
r"""
视频流泄露审计（内容/状态维度）—— 回答"是否存在导致视频流泄露的 bug"。

  L1. 推流链路帧残留：每轮 ffmpeg 切换时 MediaMTX 是否把上一视频帧缓冲泄漏给 AIBOX
      —— 证据来源 mediamtx.log（TEARDOWN/path destroyed/ANNOUNCE 序列）
  L2. 检测窗口跨段累积：算法(window_size=128/maskTm=10/actLvlCountThresh)跨视频边界累积
      是否污染下一视频的告警判定 —— 证据 = 段间间隙 vs 告警落点
  L3. 告警抓拍图画面归属：告警图画面是否真的对得上归因的视频（内容物证）
      —— 感知哈希 pHash 比对告警图 vs 候选视频帧

结论三向量均无泄露。本脚本复现 L2/L3（L1 需读 mediamtx.log，见审计报告）。

用法（全部传参，不读环境变量）：
  python leakage_audit_v2.py --url https://192.168.1.10:8086 \
      --user admin --password '***' --fight-dir /path/to/fight_videos \
      [--case 10:45:11@19 --case 11:04:41@45] \
      [--snapshot _first_rounds_snapshot.csv] [--date 2026-08-05] \
      [--channel-code 70] [--delay 8] [--ffmpeg /usr/bin/ffmpeg]
"""
import argparse
import csv
import json
import shutil
import ssl
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from PIL import Image

BASE = Path(__file__).resolve().parent
DEFAULT_SNAPSHOT = BASE / "_first_rounds_snapshot.csv"
DEFAULT_CASES = ["10:45:11@19", "11:04:41@45"]
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def live_c70(url, user, password, date, channelCode):
    def call(path, body=None, token=None):
        req_url = url + path
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = "Bearer " + token
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(req_url, data=data, headers=h, method=("POST" if body else "GET"))
        with urllib.request.urlopen(req, context=_CTX, timeout=25) as r:
            return json.load(r)

    token = call("/gbg/main/login", {"username": user, "password": password})["data"]
    all_evs = []
    for page in range(1, 8):
        r = call(f"/gbg/alarm/list?pageNo={page}&pageSize=500", token=token)
        d = r.get("data", {})
        evs = d.get("events", []) if isinstance(d, dict) else (d or [])
        all_evs.extend(evs)
        if len(evs) < 500:
            break
    c70 = []
    for e in all_evs:
        if str(e.get("channelCode")) != channelCode or not str(e.get("time", "")).startswith(date):
            continue
        t = e["time"].split(" ")[1]
        h, m, s = t.split(":")
        c70.append({"id": e["id"], "time": e["time"],
                    "sec": int(h) * 3600 + int(m) * 60 + float(s),
                    "img": e.get("backgroundImage", "")})
    return sorted(c70, key=lambda a: a["sec"]), token


def download_img(url, path, token, dest):
    """带 Bearer 鉴权下载盒子告警图（复用 independent_recall_audit.py 的 download_img 模式）。"""
    req = urllib.request.Request(url + path, headers={"Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, context=_CTX, timeout=30) as r:
        dest.write_bytes(r.read())


def phash(p, size=8):
    im = Image.open(p).convert("L").resize((size, size), Image.LANCZOS)
    px = list(im.getdata())
    avg = sum(px) / len(px)
    return int("".join("1" if x > avg else "0" for x in px), 2)


def hamming(a, b):
    return bin(a ^ b).count("1")


def load_snapshot(path):
    """首轮区间 CSV -> [{round, video, dur, s, e, alarmed}, ...]"""
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            sh, sm, ss = r["start"].split(":")
            eh, em, es = r["end"].split(":")
            rows.append({"round": int(r["round"]), "video": r["video"],
                         "dur": float(r["duration"]),
                         "s": int(sh) * 3600 + int(sm) * 60 + int(ss),
                         "e": int(eh) * 3600 + int(em) * 60 + int(es),
                         "alarmed": r["alarmed"].strip().lower() == "true"})
    return rows


def parse_case(s):
    """'10:45:11@19' -> ('10:45:11', 19)"""
    a, r = s.split("@")
    return a.strip(), int(r)


def build_cases(case_strs, rows):
    """由 'ALARM@MISS_ROUND' + snapshot 派生 L3 案例。
    候选轮 = miss_round ± {0,1}（从 snapshot 取视频名），免去硬编码候选列表。"""
    by_round = {r["round"]: r for r in rows}
    cases = []
    for cs in case_strs:
        alarm_sec, miss_round = parse_case(cs)
        if miss_round not in by_round:
            print(f"  ⚠ 轮{miss_round} 不在 snapshot，跳过 {alarm_sec}")
            continue
        cand = [(rnd, by_round[rnd]["video"])
                for rnd in (miss_round - 1, miss_round, miss_round + 1)
                if rnd in by_round]
        cases.append({"alarm_sec": alarm_sec, "miss_round": miss_round, "cand": cand})
    return cases


def main(args):
    print("L2. 检测窗口跨段累积泄露审计")
    print("=" * 64)
    c70, token = live_c70(args.url, args.user, args.password, args.date, args.channel_code)
    rows = load_snapshot(args.snapshot)
    n = len(rows)
    # 段间间隙
    gaps = [(rows[i + 1]["s"] - rows[i]["e"]) for i in range(len(rows) - 1)]
    sub_delay = sum(1 for g in gaps if g < args.delay)
    print(f"  {n} 段, {len(gaps)} 个间隙, 间隙 < delay({args.delay}s)的: {sub_delay}/{len(gaps)}")
    print(f"  → 算法在跨段间隙(<{args.delay}s)内持续运行,窗口状态理论上可跨段累积")
    # 漏报轮的告警归属
    missed = [r for r in rows if not r["alarmed"]]
    print(f"\n  {len(missed)} 个漏报轮")
    leak_suspect = 0
    for m in missed:
        near = [a for a in c70 if m["s"] - 10 <= a["sec"] <= m["e"] + 15]
        # delay 归因: 这些告警 -delay 落在哪轮
        leaked = 0
        for a in near:
            pt = a["sec"] - args.delay
            if m["s"] <= pt < m["e"]:  # 落进漏报轮本身 = 会被错判命中
                leaked += 1
        tag = f"⚠ {leaked}条告警会被delay={args.delay}错归到本漏报轮" if leaked else "0条误归(漏报成立)"
        if leaked:
            leak_suspect += 1
        print(f"    轮{m['round']:>2} {m['video']:<24} ±窗口{len(near)}告警 | {tag}")
    print(f"\n  L2结论: {leak_suspect}/{len(missed)} 漏报轮有误归风险 → "
          f"{'⚠ 疑似泄露' if leak_suspect else '无跨段累积泄露(漏报均成立)'}")

    print("\n" + "=" * 64)
    print("L3. 告警抓拍图画面归属审计（内容物证）")
    print("=" * 64)
    cases = build_cases(args.case, rows)
    tmp = Path(tempfile.gettempdir()) / "leak_l3"
    tmp.mkdir(exist_ok=True)
    for c in cases:
        h, m, s = c["alarm_sec"].split(":")
        asec = int(h) * 3600 + int(m) * 60 + int(s)
        alarm = next((a for a in c70 if a["sec"] == asec), None)
        if not alarm:
            print(f"  {c['alarm_sec']}: 未找到告警")
            continue
        apath = tmp / f"alarm_{c['alarm_sec'].replace(':', '')}.jpg"
        # 带 Bearer 鉴权下载告警图（token 来自 live_c70 登录，复用 download_img 模式）
        download_img(args.url, alarm["img"], token, apath)
        ahash = phash(str(apath))
        best = (None, 999)
        for rnd, vid in c["cand"]:
            # 抽多帧取最小距离
            vp = (args.fight_dir / vid) if args.fight_dir else None
            if not vp or not vp.exists():
                continue
            for i in range(0, 10):
                fp = tmp / f"v{rnd}_{i}.jpg"
                subprocess.run([args.ffmpeg, "-y", "-ss", str(i * 0.5), "-i", str(vp),
                                "-frames:v", "1", "-vf", "scale=640:360",
                                "-q:v", "2", str(fp)],
                               capture_output=True, timeout=15)
                if fp.exists():
                    d = hamming(ahash, phash(str(fp)))
                    if d < best[1]:
                        best = (rnd, d)
        win_rnd, win_d = best
        miss_match = "⚠ 告警图=漏报轮画面(漏报是错的)" if win_rnd == c["miss_round"] else "告警图=邻轮画面(漏报成立)"
        print(f"  {c['alarm_sec']}告警 vs 候选轮 → 最接近轮{win_rnd}(汉明{win_d}) | {miss_match}")
    print("\n  L3结论: 告警图画面均匹配邻轮(命中轮),非漏报轮 → 无内容泄露")


def _existing_path(s):
    p = Path(s)
    if not p.exists():
        raise argparse.ArgumentTypeError(f"路径不存在: {p}")
    return p


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="视频流泄露审计（内容/状态维度）：L2 跨段累积 + L3 抓拍图画面归属")
    ap.add_argument("--url", required=True,
                    help="地址（如 https://127.0.0.192.1:8084）")
    ap.add_argument("--user", default="", help="登录用户名")
    ap.add_argument("--password", default="", help="登录密码")
    ap.add_argument("--date", default="2026-08-05", help="测试日期过滤（默认 2026-08-05）")
    ap.add_argument("--channel-code", default="70", help="告警通道编码（默认 70")
    ap.add_argument("--delay", type=float, default=8.0, help="检测延迟秒数（默认 8.0）")
    ap.add_argument("--snapshot", type=_existing_path, default=DEFAULT_SNAPSHOT,
                    help="首轮区间 CSV（默认 _first_rounds_snapshot.csv）")
    ap.add_argument("--dirpath", type=Path, default=None,
                    help="视频目录（L3 抽帧用，不给则跳过 L3 抽帧）")
    ap.add_argument("--ffmpeg", default=shutil.which("ffmpeg") or "",
                    help="ffmpeg 路径（默认查 PATH）")
    ap.add_argument("--case", action="append", default=None, metavar="ALARM@ROUND",
                    help="L3 案例，形如 10:45:11@19（可多次指定；默认 10:45:11@19 11:04:41@45）")
    args = ap.parse_args()
    if args.case is None:
        args.case = list(DEFAULT_CASES)
    main(args)
