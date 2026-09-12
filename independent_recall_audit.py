#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
独立复算器：回答"好结果是不是 code bug 造出来的"。

不信任任何既有脚本（stream_fight_loop.py / compute_fight_recall.py / leakage_audit.py），
只用两个独立来源：
  1) 直连 AIBOX 后端 /gbg/alarm/list 拉取 channelCode=70 的全部告警（ground truth，实时）
  2) 各测试 run 的推流区间 CSV（alarm_report_fight.csv 首轮、fight_retest_summary.csv 重测、
     nonfight_falsepositive_batch2.csv NonFight）

用 LIVE 告警重算 hit/miss，与报告里的数字逐一对账。任何对不上 = 可能是 bug。
"""
import csv
import json
import os
import ssl
import sys
import datetime as dt
import urllib.request
from pathlib import Path
from collections import Counter

BASE = Path(__file__).resolve().parent
AIBOX = os.environ.get("AIBOX_URL", "")
if not AIBOX:
    print("请设置 AIBOX_URL 环境变量（AIBOX 盒子地址）"); sys.exit(1)
DELAY = 8.0  # stream_fight_loop.py 用的检测延迟
TEST_DATE = "2026-08-05"  # 所有测试 run 都在 8/5 当天


def live_alarms():
    """直连 AIBOX 拉全部告警，返回 [{time, sec, channelCode, channelName, conf, id}, ...]"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    def call(path, body=None, token=None):
        url = AIBOX + path
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = "Bearer " + token
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, headers=h, method=("POST" if body else "GET"))
        with urllib.request.urlopen(req, context=ctx, timeout=25) as r:
            return json.load(r)

    r = call("/gbg/main/login", {"username": os.environ.get("AIBOX_USER", ""), "password": os.environ.get("AIBOX_PASS", "")})
    token = r["data"]
    all_evs = []
    for page in range(1, 8):
        r = call(f"/gbg/alarm/list?pageNo={page}&pageSize=500", token=token)
        d = r.get("data", {})
        evs = d.get("events", []) if isinstance(d, dict) else (d or [])
        all_evs.extend(evs)
        if len(evs) < 500:
            break
    return all_evs


def hms_to_sec(t):
    """'10:31:53' 或 '2026-08-05 10:31:53' -> 当天0点起的秒数。
    保留日期：返回 (date_str, seconds_in_day)。所有 run 都在 8/5，需按日期区分避免跨天串味。"""
    t = str(t).strip()
    if " " in t:
        date_str, t = t.split(" ")[0], t.split(" ")[1]
    else:
        date_str = TEST_DATE  # 无日期的区间(CSV里只有 HH:MM:SS)默认 8/5
    h, m, s = t.split(":")
    return date_str, int(h) * 3600 + int(m) * 60 + float(s)


def sec(t):
    """便捷：只取秒数（当天内）。调用方需确保同日。"""
    return hms_to_sec(t)[1]


def load_first_rounds():
    """首轮 47 段（fight accumulate）。CSV 当前被 NonFight 覆盖了，
    但首轮区间在 fight_recall_report.md / 旧 metadata 里——这里从 git 历史外的快照拿不到。
    改用 fight_missed_videos 的 last_interval + 已知 47 段顺序不可全得。
    因此首轮复算改用 aibox_alarm metadata 的 100 条（8/5 上午窗口）+ 报告里贴的区间。
    为稳妥，直接读 alarm_report_fight.csv 的「上一版」——不存在了。
    所以首轮：用 leakage_audit 已验证过的 47 段区间，硬编码在下方（来自报告/审计）。"""
    rows = []
    # 来自 alarm_report_fight.csv 的首批内容（已确认 47 段区间，文件后续被 NonFight 覆盖）
    # 这里从内存里的首轮区间重建——读取之前保存的快照
    snap = BASE / "_first_rounds_snapshot.csv"
    if not snap.exists():
        return None, "无首轮区间快照 _first_rounds_snapshot.csv"
    with open(snap, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            rows.append({
                "round": int(r["round"]), "video": r["video"],
                "dur": float(r["duration"]),
                "s": sec(r["start"]), "e": sec(r["end"]),
                "csv_count": int(r["count"]),
                "csv_alarmed": r["alarmed"].strip().lower() == "true",
            })
    return rows, None


def assign(alarms_c70, rounds, delay):
    """把 channel70 告警时间 -delay 归到 [s,e)。返回 {round:[alarms]}, orphans。"""
    assigned = {r["round"]: [] for r in rounds}
    used = set()
    for a in alarms_c70:
        pt = a["sec"] - delay
        for r in rounds:
            if r["s"] <= pt < r["e"]:
                assigned[r["round"]].append(a)
                used.add(a["id"])
                break
    orphan = [a for a in alarms_c70 if a["id"] not in used]
    return assigned, orphan


def main():
    print("拉取 AIBOX 全部告警（ground truth）...")
    evs = live_alarms()
    print(f"  AIBOX 告警总数: {len(evs)}")
    c70 = []
    for e in evs:
        if str(e.get("channelCode")) != "70":
            continue
        t = str(e.get("time", ""))
        if not t:
            continue
        # 只取 8/5 当天的告警（其余日期是更早 run，与本次测试无关）
        if not t.startswith(TEST_DATE):
            continue
        objs = e.get("objects") or []
        conf = 0.0
        if objs and isinstance(objs, list) and objs:
            try:
                conf = round(float(objs[0].get("confidence", 0) or 0), 2)
            except Exception:
                pass
        _, s = hms_to_sec(t)
        c70.append({"id": e.get("id"), "time": t, "sec": s,
                    "conf": conf, "channelName": e.get("channelName")})
    print(f"  channelCode=70 且 8/5 当天告警数: {len(c70)}")
    # 时间分布
    byhour = Counter(a["time"][11:13] + ":00" for a in c70)
    print(f"  channel70 按小时: {dict(sorted(byhour.items()))}")
    c70.sort(key=lambda a: a["sec"])
    if c70:
        print(f"  最早: {c70[0]['time']}  最晚: {c70[-1]['time']}")

    # ── 首轮复算 ──
    first, err = load_first_rounds()
    print("\n" + "=" * 64)
    print("首轮（fight accumulate, 47 段）独立复算")
    print("=" * 64)
    if first is None:
        print(f"  跳过：{err}")
        print("  （alarm_report_fight.csv 已被 NonFight 覆盖；首轮区间用之前快照）")
    else:
        for delay in (0.0, DELAY):
            assigned, orphan = assign(c70, first, delay)
            hit = sum(1 for r in first if assigned[r["round"]])
            miss = len(first) - hit
            print(f"\n  [delay={delay}s] 命中 {hit}/47, 漏报 {miss}/47, 召回率 {hit/47*100:.2f}%")
            print(f"    未归因告警(orphan): {len(orphan)}")
            # 对账脚本 CSV
            mismatch = 0
            flips = 0
            for r in first:
                my = len(assigned[r["round"]])
                if my != r["csv_count"]:
                    mismatch += 1
                if (my > 0) != r["csv_alarmed"]:
                    flips += 1
            print(f"    与脚本CSV count 不一致轮次: {mismatch}（翻转 hit/miss: {flips}）")
            missed = [r["video"] for r in first if not assigned[r["round"]]]
            print(f"    漏报视频({len(missed)}): {missed}")

    # ── 重测复算 ──
    print("\n" + "=" * 64)
    print("重测（fight repeat 60s, 14 段）独立复算")
    print("=" * 64)
    retest_csv = BASE / "fight_retest_summary.csv"
    retest = []
    with open(retest_csv, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            s, e = r["retest_interval"].split("-")
            retest.append({
                "round": int(r["retest_round"]), "video": r["video"],
                "s": sec(s), "e": sec(e),
                "rep_count": int(r["retest_count"]),
                "rep_result": r["retest_result"],
            })
    # 重测窗口告警（AIBOX 上 15:23~15:37）
    retest_alarms = [a for a in c70 if retest[0]["s"] <= a["sec"] < retest[-1]["e"]]
    print(f"  重测窗口内 channel70 告警: {len(retest_alarms)} 条")
    for delay in (0.0, DELAY):
        assigned, orphan = assign(c70, retest, delay)
        hit = sum(1 for r in retest if assigned[r["round"]])
        miss = len(retest) - hit
        print(f"\n  [delay={delay}s] 命中 {hit}/14, 漏报 {miss}/14")
        # 对账重测报告
        mismatch = 0
        for r in retest:
            my = len(assigned[r["round"]])
            if my != r["rep_count"]:
                mismatch += 1
            my_hit = my > 0
            rep_hit = r["rep_result"] == "命中"
            if my_hit != rep_hit:
                print(f"    ⚠ 翻转: 轮{r['round']} {r['video']} 报告={r['rep_result']}(count={r['rep_count']}) 复算={'命中' if my_hit else '漏报'}(count={my})")
        print(f"  与重测报告 count 不一致轮次: {mismatch}")
        missed = [r["video"] for r in retest if not assigned[r["round"]]]
        print(f"  漏报视频({len(missed)}): {missed}")

    # ── NonFight 复算（关键）──
    print("\n" + "=" * 64)
    print("NonFight（repeat 20s, 200 段）独立复算 —— 0 误检的真伪")
    print("=" * 64)
    nf_csv = BASE / "nonfight_falsepositive_batch2.csv"
    nf = []
    with open(nf_csv, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            nf.append({
                "round": int(r["round"]), "video": r["video"],
                "s": sec(r["start"]), "e": sec(r["end"]),
            })
    print(f"  NonFight 第二批区间数: {len(nf)}")
    print(f"  区间: {nf[0]['s']}~{nf[-1]['e']} (即 {nf[0]['video']}..{nf[-1]['video']})")
    # NonFight 第二批窗口内（及前后延 8s）channel70 告警数
    nf_start, nf_end = nf[0]["s"], nf[-1]["e"]
    in_window = [a for a in c70 if nf_start - DELAY <= a["sec"] < nf_end]
    print(f"  NonFight 第二批窗口[延-8s,+0s]内 channel70 告警: {len(in_window)} 条")
    for delay in (0.0, DELAY):
        assigned, orphan = assign(c70, nf, delay)
        fp = sum(1 for r in nf if assigned[r["round"]])
        print(f"  [delay={delay}s] 误检视频数: {fp}")
        if fp:
            for r in nf:
                if assigned[r["round"]]:
                    print(f"    ⚠ 误检: 轮{r['round']} {r['video']} count={len(assigned[r['round']])}")
    print("\n  结论: " + ("NonFight 第二批 0 误检 = 真实" if not in_window else f"⚠ 有 {len(in_window)} 条告警落窗口,需排查"))

    # ── channelName 过滤对账：脚本用 channelName==stream_name 过滤，会否漏掉真告警 ──
    print("\n" + "=" * 64)
    print("代码风险点排查：channelName 过滤是否屏蔽真告警")
    print("=" * 64)
    c70_names = Counter(a["channelName"] for a in c70)
    print(f"  channelCode=70 的 channelName 取值: {dict(c70_names)}")
    # 脚本 report_alarm_vs_videos 用 a.get('channelName')==stream_name('fight') 过滤
    # 若有 channel70 告警 channelName≠'fight'，会被脚本漏计
    not_fight = [a for a in c70 if a["channelName"] != "fight"]
    print(f"  channel70 但 channelName≠'fight' 的告警: {len(not_fight)} 条 (脚本会漏计)")
    if not_fight:
        for a in not_fight[:5]:
            print(f"    {a['time']} name={a['channelName']} conf={a['conf']}")


if __name__ == "__main__":
    main()
