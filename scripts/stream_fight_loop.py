#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按"拼接推流"方式把 Fight/NonFight 视频循环推到 MediaMTX（RTSP）。

每轮循环（共 10 轮）：
  1. 取下一个 5s 视频片段。
  2. 若与当前累积视频【同类】(同在 Fight 或同在 NonFight) → 继续累积拼接（累积视频变长）。
     若【不同类】→ 丢弃旧累积，从最新这个视频重新开始拼接。
  3. 累积视频不足 10s 时，补同类的下一个片段到 ≥10s。
  4. 把拼接后的视频作为流，用 ffmpeg -re 实时推一次到 rtsp://localhost:8554/<流名>。
推完 10 轮结束。

分辨率不强制统一来源，但拼接需统一画布：每片段按比例缩放 + 居中黑边(pad)到 640×360，
不拉伸变形（640×360 是 six4 原分辨率，AIBOX 已验证可拉）。

用法：
  python scripts/stream_fight_loop.py
  python scripts/stream_fight_loop.py --stream fight --rounds 10 --host localhost
  python scripts/stream_fight_loop.py --val-dir "F:/桌面/val/val" --stream fight

依赖：ffmpeg + ffprobe 在 PATH 中。前置：MediaMTX 已监听 :8554。
"""

import argparse
import json
import os
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

MEDIAMTX_DEFAULT_PORT = 8554
CANVAS_W, CANVAS_H = 640, 360          # 统一画布（缩放+黑边，不拉伸）
CLIP_DURATION = 5.0                     # 每个片段 5s
MIN_DURATION = 10.0                     # 累积视频最少 10s
SEGMENTS_PER_GROUP = 3                  # 每 N 个同类为一组，组间切换 Fight/NonFight

# AIBOX 默认连接参数（从环境变量读取；真值放 .env，勿入库）
AIBOX_DEFAULT_URL = os.environ.get("AIBOX_URL", "")
AIBOX_DEFAULT_USER = os.environ.get("AIBOX_USER", "")
AIBOX_DEFAULT_PASS = os.environ.get("AIBOX_PASS", "")

# 人员行为检测算法默认参数（来自 AIBOX 算法字典 personAction 的 algConfig.condition 默认值）。
# 其中以下参数按需从默认值调整：
#   maskTm(报警屏蔽时间) 300s → 10s：配合 --repeat 4（5s×4=20s）使用，段内首次告警后屏蔽 10s，
#     间隔短便于段内多次触发；
#   actLvlCountThresh(分级模型识别多少次打架后上报事件，范围 1~100) 100 → 1：
#     识别 1 次即上报，最大化告警灵敏度，避免漏报。
# 其余参数保持默认不变。
PERSON_ACTION_DEFAULT_CONDITION = json.dumps({
    "confidence": 0.3, "IFALGSDK__ACT__conf": 0.5, "IFALGSDK__CLS__conf": 0.7,
    "actLvlCountThresh": 1, "mergeRectCntMax": 4, "rectWidthMin": 256, "rectHeightMin": 256,
    "total_count_thresh": 2, "alert_count_thresh": 2, "total_count_thresh_1": 1,
    "alert_count_thresh_1": 4, "window_size": 128, "action_fight": 1, "action_run": 0,
    "action_falldown": 0, "maskTm": 10, "whRate": 0.06, "enableSkip": False,
    "p3_count": 1, "p3_rate": 0.5, "total_confidence": 120, "child_thresh": 0.7,
    "child_rate": 0.25, "quality_thresh": 0.21, "quality_rate": 0.5,
}, ensure_ascii=False)

# 检测上报延迟估计(秒)：告警产生时间通常比画面实际发生时间晚若干秒(拉流+检测+累积+上报)。
# 统计时把告警时间往前推这个量再匹配到对应视频轮次，减少滞后告警误归下一视频。
DETECT_DELAY = 8.0


def die(msg: str, code: int = 1):
    print(f"[错误] {msg}", file=sys.stderr)
    sys.exit(code)


# ── AIBOX 算法任务 API（登录/查询/创建/启动/停止/删除）─────────────────────────

class AiboxClient:
    """AIBOX Web API 的轻量封装（仅用标准库 urllib），负责给通道挂/启/停人员行为算法任务。

    API（均需 Authorization: Bearer <token>，内网自签证书故禁用校验）：
      登录  POST /gbg/main/login            {username,password} -> data=token
      列表  GET  /gbg/intellif/list?...     -> data.tasks[]
      创建  POST /gbg/intellif/create       -> 创建并默认运行，data.task.uuid
      启动  POST /gbg/intellif/start        {uuid}
      停止  POST /gbg/intellif/stop         {uuid}
      删除  POST /gbg/intellif/delete       {uuids:[...]}
    """

    def __init__(self, base_url: str, username: str, password: str):
        self.base = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.token: str | None = None
        self._ctx = ssl.create_default_context()
        self._ctx.check_hostname = False
        self._ctx.verify_mode = ssl.CERT_NONE

    def _call(self, method: str, path: str, body=None, with_auth=True):
        url = self.base + path
        headers = {"Content-Type": "application/json"}
        if with_auth and self.token:
            headers["Authorization"] = "Bearer " + self.token
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, context=self._ctx, timeout=20) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            body_txt = ""
            try:
                body_txt = e.read().decode()[:200]
            except Exception:
                pass
            return {"code": e.code, "success": False, "message": f"HTTP {e.code}: {body_txt}"}
        except Exception as e:
            return {"code": -1, "success": False, "message": str(e)}

    def login(self) -> bool:
        r = self._call("POST", "/gbg/main/login",
                       {"username": self.username, "password": self.password}, with_auth=False)
        if r.get("success") and r.get("data"):
            self.token = r["data"]
            return True
        print(f"[AIBOX] 登录失败：{r.get('message')}", file=sys.stderr)
        return False

    def list_tasks(self) -> list:
        r = self._call("GET", "/gbg/intellif/list?pageNo=1&pageSize=100&total=0&page=1&size=100")
        if r.get("success"):
            return r.get("data", {}).get("tasks", []) or []
        return []

    def find_task(self, channel_code: str) -> dict | None:
        """按通道编码找已存在的算法任务"""
        for t in self.list_tasks():
            if str(t.get("channelCodes")) == str(channel_code):
                return t
        return None

    def create_task(self, channel_code: str, channel_name: str) -> str | None:
        """创建人员行为检测算法任务（默认参数），返回任务 uuid。创建后默认即运行。"""
        body = {
            "code": str(channel_code),
            "name": channel_name,
            "channelCodes": str(channel_code),
            "channelNames": channel_name,
            "channelRoi": [{
                "channelCode": str(channel_code), "channelName": channel_name,
                "reverseSelect": 0, "width": 1920, "height": 1080,
                "roiCombo": [], "snaperRoi": [], "exclusionRoi": [],
            }],
            "algType": "personAction",
            "taskType": "continues",
            "attribute": "CV",
            "target": [],
            "condition": PERSON_ACTION_DEFAULT_CONDITION,
            "snaperRoi": [], "exclusionRoi": [],
            "timeSet": "{}", "alarmSoundId": None, "alarmNum": None,
        }
        r = self._call("POST", "/gbg/intellif/create", body)
        if r.get("success"):
            return r.get("data", {}).get("task", {}).get("uuid")
        print(f"[AIBOX] 创建算法任务失败：{r.get('message')}", file=sys.stderr)
        return None

    def start_task(self, uuid: str) -> bool:
        r = self._call("POST", "/gbg/intellif/start", {"uuid": uuid})
        return bool(r.get("success"))

    def stop_task(self, uuid: str) -> bool:
        r = self._call("POST", "/gbg/intellif/stop", {"uuid": uuid})
        return bool(r.get("success"))

    def delete_task(self, uuid: str) -> bool:
        r = self._call("POST", "/gbg/intellif/delete", {"uuids": [uuid]})
        return bool(r.get("success"))


def ensure_algo_task(client: AiboxClient, channel_code: str, channel_name: str, delete_on_exit: bool):
    """确保通道上有运行中的人员行为算法任务。

    返回 (uuid, owned) —— owned=True 表示这次新建的（退出时会删除），
    owned=False 表示复用已有任务（退出时只停止不删除）。
    """
    task = client.find_task(channel_code)
    if task:
        uuid = task.get("uuid")
        if str(task.get("status")) != "1":      # 0=已停止，1=运行中
            if client.start_task(uuid):
                print(f"[AIBOX] 复用已有算法任务并启动：{uuid}")
            else:
                print(f"[AIBOX] 警告：启动已有任务失败，继续推流")
        else:
            print(f"[AIBOX] 复用已在运行的算法任务：{uuid}")
        return uuid, False

    uuid = client.create_task(channel_code, channel_name)
    if uuid:
        # create 接口默认即创建并运行；保险起见再确认启动
        client.start_task(uuid)
        print(f"[AIBOX] 已创建并启动人员行为算法任务：{uuid}（默认参数）")
        # owned 只表示"本次是否新建"，与是否删除解耦：
        # 新建的任务退出时至少要停止（除非显式 --delete-algo-on-exit 才删除），
        # 避免每次运行都在 AIBOX 上残留一个运行中的算法任务。
        return uuid, True
    print("[AIBOX] 警告：算法任务创建失败，将仅推流不挂算法")
    return None, False


def which_ffmpeg() -> str:
    ff = shutil.which("ffmpeg")
    if not ff:
        die("未找到 ffmpeg，请安装并加入 PATH。")
    return ff


def probe_duration(video_path: str) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0 and r.stdout.strip():
            return float(r.stdout.strip())
    except Exception:
        pass
    return 0.0


def fmt_dur(s: float) -> str:
    s = int(round(s or 0))
    m, s = divmod(s, 60)
    return f"{m}m{s:02d}s"


def list_videos(val_dir: Path, category: str) -> list[Path]:
    """按文件名排序返回某类别下的所有视频。"""
    d = val_dir / category
    if not d.exists():
        die(f"类别目录不存在：{d}")
    exts = {".avi", ".mp4", ".mov", ".mkv", ".flv"}
    return sorted([p for p in d.iterdir() if p.suffix.lower() in exts])


def build_concat_filter(n: int) -> str:
    """
    把 n 个输入拼接成一个，统一画布 640×360（缩放+居中黑边，保持比例不拉伸）。
    用 scale+pad+setsar 再 concat。
    """
    scaled = []
    for i in range(n):
        # 缩放到画布内（contain），再 pad 到画布大小，置 sar=1 去除比例差异
        scaled.append(
            f"[{i}:v]scale={CANVAS_W}:{CANVAS_H}:force_original_aspect_ratio=decrease,"
            f"pad={CANVAS_W}:{CANVAS_H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[v{i}]"
        )
    inputs = "".join(f"[v{i}]" for i in range(n))
    return ";".join(scaled) + f";{inputs}concat=n={n}:v=1:a=0[vout]"


def concat_segments(ffmpeg: str, segments: list[Path], out_path: Path) -> tuple[bool, str]:
    """用 concat filter 把多个片段拼成一个 mp4（统一画布）。返回 (成功, 信息)。"""
    if len(segments) == 1:
        # 单段也要统一画布（便于后续统一推流）
        cmd = [
            ffmpeg, "-y", "-i", str(segments[0]),
            "-vf", (f"scale={CANVAS_W}:{CANVAS_H}:force_original_aspect_ratio=decrease,"
                    f"pad={CANVAS_W}:{CANVAS_H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"),
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-r", "30",
            "-an", str(out_path),
        ]
    else:
        cmd = [ffmpeg, "-y"]
        for s in segments:
            cmd += ["-i", str(s)]
        cmd += [
            "-filter_complex", build_concat_filter(len(segments)),
            "-map", "[vout]",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-r", "30",
            "-an", str(out_path),
        ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return False, "拼接超时(>300s)"
    if r.returncode != 0:
        return False, (r.stderr or "")[-800:]
    return True, ""


def repeat_video(ffmpeg: str, src: Path, times: int, out_path: Path) -> tuple[bool, str]:
    """把单个视频自我复制 times 次拼成一条长视频（统一画布 640×360）。返回 (成功, 信息)。

    用 -stream_loop 复制输入后再 scale+pad 统一画布重编码，避免逐个 concat filter 输入过多。
    -stream_loop N 表示输入循环 N 次（N=times-1，总共播 times 遍）。
    """
    loop_n = max(0, times - 1)
    cmd = [ffmpeg, "-y"]
    if loop_n > 0:
        cmd += ["-stream_loop", str(loop_n)]
    cmd += [
        "-i", str(src),
        "-vf", (f"scale={CANVAS_W}:{CANVAS_H}:force_original_aspect_ratio=decrease,"
                f"pad={CANVAS_W}:{CANVAS_H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"),
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-r", "30",
        "-an", str(out_path),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return False, "复制延长超时(>300s)"
    if r.returncode != 0:
        return False, (r.stderr or "")[-800:]
    return True, ""


def push_stream(ffmpeg: str, video_path: Path, rtsp_url: str, log_path: Path) -> int:
    """用 ffmpeg -re 实时推流。返回 ffmpeg 退出码。"""
    cmd = [
        ffmpeg, "-re", "-i", str(video_path),
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
        "-pix_fmt", "yuv420p", "-g", "30", "-an",
        "-rtsp_transport", "tcp", "-f", "rtsp", rtsp_url,
    ]
    print(f"    [推流] {video_path.name} → {rtsp_url}")
    popen_kwargs = dict()
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    with open(log_path, "a", encoding="utf-8") as log_fp:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=log_fp, **popen_kwargs)
        try:
            return proc.wait()
        except KeyboardInterrupt:
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
            print("\n[中断] 已停止推流。")
            sys.exit(130)


# ── 告警-视频对应统计 ─────────────────────────────────────────────────────

def fetch_alarms(aibox: "AiboxClient") -> list:
    """拉取告警列表，返回原始 event dict 列表。"""
    r = aibox._call("GET", "/gbg/alarm/list?pageNo=1&pageSize=200")
    if r.get("success"):
        d = r.get("data", {})
        return d.get("events", []) if isinstance(d, dict) else (d or [])
    return []


def parse_alarm_time(t: str):
    """解析 'YYYY-MM-DD HH:MM:SS' 为 epoch 秒。失败返回 None。"""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            import datetime as _dt
            return _dt.datetime.strptime(t, fmt).timestamp()
        except Exception:
            continue
    return None


def report_alarm_vs_videos(rounds_log: list, alarms: list, stream_name: str):
    """把告警按时间匹配到各轮视频，输出统计表 + 写 csv。

    rounds_log: [{"round":int,"src":Path,"duration":float,"start":float,"end":float}, ...]
    alarms:     AIBOX 原始告警列表
    """
    import csv
    import datetime as _dt

    # 只统计本流的告警
    my_alarms = [a for a in alarms if a.get("channelName") == stream_name]
    # 告警时间 → epoch，再往前推检测延迟
    a_pts = []
    for a in my_alarms:
        ts = parse_alarm_time(str(a.get("time", "")))
        if ts is None:
            continue
        conf = 0.0
        objs = a.get("objects") or []
        if objs:
            conf = round(float(objs[0].get("confidence", 0) or 0), 2)
        a_pts.append((ts - DETECT_DELAY, ts, a.get("time"), conf))

    # 把每条告警归到其(去延迟后)时间所在的轮次
    rows = []
    for rd in rounds_log:
        s, e = rd["start"], rd["end"]
        hit = [p for p in a_pts if s <= p[0] < e]
        rows.append({
            "round": rd["round"],
            "video": rd["src"].name,
            "duration": rd["duration"],
            "alarmed": len(hit) > 0,
            "count": len(hit),
            "conf": ", ".join(str(p[3]) for p in hit) if hit else "",
            "start": _dt.datetime.fromtimestamp(s).strftime("%H:%M:%S"),
            "end": _dt.datetime.fromtimestamp(e).strftime("%H:%M:%S"),
        })

    reported = [r for r in rows if r["alarmed"]]
    missed = [r for r in rows if not r["alarmed"]]

    print("\n" + "=" * 64)
    print("告警-视频对应统计")
    print("=" * 64)
    print(f"本流告警总数: {len(my_alarms)}    视频轮数: {len(rows)}")
    print(f"报了告警: {len(reported)} 个视频    没报: {len(missed)} 个视频")
    print("-" * 64)
    print(f"{'轮':>3}  {'视频':<28} {'时长':>5}  {'告警':>4}  {'置信度':<14} {'区间'}")
    for r in rows:
        flag = "✓" if r["alarmed"] else "✗"
        print(f"{r['round']:>3}  {r['video']:<28} {r['duration']:>4.0f}s {flag} {r['count']:>3}  {r['conf']:<14} {r['start']}-{r['end']}")

    # 写 csv
    work = Path(tempfile.gettempdir()) / "stream_fight_loop"
    csv_path = work / f"alarm_report_{stream_name}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["round", "video", "duration", "alarmed", "count", "conf", "start", "end"])
        w.writeheader()
        w.writerows(rows)
    print("-" * 64)
    print(f"统计明细已写入: {csv_path}")
    print("=" * 64)


def main():
    ap = argparse.ArgumentParser(
        description="Fight/NonFight 拼接循环推流：每轮拼入下一段，同类累积、异类重置，推流 10 轮。",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__,
    )
    ap.add_argument("--val-dir", default=r"F:\桌面\val\val",
                    help="数据集根目录（含 Fight/、NonFight/ 子目录）")
    ap.add_argument("--categories", default="Fight",
                    help="参与拼接的类别，逗号分隔（默认只 Fight；如 'Fight,NonFight' 会交替取）")
    ap.add_argument("-s", "--stream", default="fight",
                    help="流名称（默认 fight）")
    ap.add_argument("--host", default="localhost", help="MediaMTX 地址（默认 localhost）")
    ap.add_argument("--port", type=int, default=MEDIAMTX_DEFAULT_PORT, help="RTSP 端口")
    ap.add_argument("--rounds", type=int, default=10, help="循环轮数（默认 10）")
    ap.add_argument("--offset", type=int, default=0,
                    help="repeat 模式下跳过前 N 个视频（默认 0）；用于分批测试同一目录的不同区间")
    ap.add_argument("--mode", choices=["accumulate", "repeat"], default="accumulate",
                    help="accumulate=同类累积拼接（默认）；repeat=单视频自我复制延长后逐个推流")
    ap.add_argument("--repeat", type=int, default=12,
                    help="repeat 模式下每个视频复制次数（默认 12，5s×12=60s）")
    ap.add_argument("--keep", action="store_true", help="保留拼接中间视频（默认推完即删）")
    ap.add_argument("--videos", default="",
                    help="仅推流指定视频文件（repeat 模式专用，逗号分隔的文件名或绝对路径）；"
                         "不传则推 --val-dir 下 --categories 的全部视频")
    # AIBOX 算法任务相关
    ap.add_argument("--algo", dest="algo", action="store_true", default=True,
                    help="推流时给通道挂人员行为算法任务（默认开启，用默认参数）")
    ap.add_argument("--no-algo", dest="algo", action="store_false",
                    help="不挂算法任务，仅推流")
    ap.add_argument("--channel-code", default="70",
                    help="AIBOX 通道编码（默认 70，对应 fight 通道；需事先在 AIBOX 通道管理里建好该通道）")
    ap.add_argument("--aibox-url", default=AIBOX_DEFAULT_URL, help="AIBOX 地址")
    ap.add_argument("--aibox-user", default=AIBOX_DEFAULT_USER, help="AIBOX 账号")
    ap.add_argument("--aibox-pass", default=AIBOX_DEFAULT_PASS, help="AIBOX 密码")
    ap.add_argument("--delete-algo-on-exit", action="store_true",
                    help="退出时删除本次新建的算法任务（默认：新建的停止、复用的不动）")
    args = ap.parse_args()

    if not all(c.isalnum() or c in "-_" for c in args.stream):
        die("流名称只能含字母、数字、连字符和下划线。")

    val_dir = Path(args.val_dir)
    ffmpeg = which_ffmpeg()

    # 预读所选类别的视频列表（若传了 --videos 则跳过目录扫描）
    selected_videos = [s.strip() for s in args.videos.split(",") if s.strip()] if args.videos else []
    selected_cats = [c.strip() for c in args.categories.split(",") if c.strip()]
    cats = {}
    if selected_videos:
        # --videos：把每个文件名解析成 Path，支持纯文件名（在 --val-dir/<category>/ 下找）或绝对路径
        search_dirs = [val_dir / c for c in (selected_cats or ["Fight", "NonFight"])]
        for sv in selected_videos:
            p = Path(sv)
            if not p.exists():
                # 纯文件名：在候选目录里找
                for d in search_dirs:
                    cand = d / sv
                    if cand.exists():
                        p = cand
                        break
            if not p.exists():
                die(f"--videos 指定的视频不存在：{sv}")
            cats.setdefault(selected_cats[0] if selected_cats else "Fight", []).append(p)
        for c, lst in cats.items():
            print(f"[数据] --videos 指定 {c}: {len(lst)} 个视频")
    else:
        for c in selected_cats:
            cats[c] = list_videos(val_dir, c)
            print(f"[数据] {c}: {len(cats[c])} 个视频")
            if not cats[c]:
                die(f"{c} 目录无视频。")
    # 预读时长（这里都是 5s，但用 ffprobe 实测更稳）
    dur_cache = {}
    def dur_of(p: Path) -> float:
        if p not in dur_cache:
            dur_cache[p] = probe_duration(str(p)) or CLIP_DURATION
        return dur_cache[p]

    # 工作目录
    work = Path(tempfile.gettempdir()) / "stream_fight_loop"
    work.mkdir(parents=True, exist_ok=True)
    log_path = work / f"{args.stream}.log"
    log_path.write_text("", encoding="utf-8")

    rtsp_url = f"rtsp://{args.host}:{args.port}/{args.stream}"

    # 交替取片段的指针：每 SEGMENTS_PER_GROUP 个同类为一组，组间在所选类别间切换
    cat_order = list(cats.keys())
    cur_cat = cat_order[0]
    taken_in_group = 0
    idx = {c: 0 for c in cat_order}

    def next_segment() -> tuple[Path, str]:
        """取下一个片段，返回 (path, category)。组间在所选类别间切换。"""
        nonlocal cur_cat, taken_in_group
        lst = cats[cur_cat]
        p = lst[idx[cur_cat] % len(lst)]
        idx[cur_cat] += 1
        taken_in_group += 1
        cat = cur_cat
        if taken_in_group >= SEGMENTS_PER_GROUP and len(cat_order) > 1:
            ci = cat_order.index(cur_cat)
            cur_cat = cat_order[(ci + 1) % len(cat_order)]
            taken_in_group = 0
        return p, cat

    accumulated: list[tuple[Path, str]] = []   # [(path, category), ...]
    acc_duration = 0.0

    print("=" * 64)
    print(f"流名称    : {args.stream}")
    print(f"RTSP 地址 : {rtsp_url}")
    print(f"模式      : {args.mode}" + (f"（每个视频复制 {args.repeat} 次）" if args.mode == "repeat" else ""))
    print(f"轮数      : {args.rounds}")
    print(f"画布      : {CANVAS_W}x{CANVAS_H}（缩放+黑边，不拉伸）")
    if args.mode == "accumulate":
        print(f"最少时长  : {fmt_dur(MIN_DURATION)}（每段 {fmt_dur(CLIP_DURATION)}）")
        print(f"取片策略  : 类别 {list(cats.keys())}，每 {SEGMENTS_PER_GROUP} 个同类一组"
              + ("" if len(cats) > 1 else "（单一类别，持续累积）"))
    else:
        print(f"每段时长  : {fmt_dur(CLIP_DURATION)} × {args.repeat} = {fmt_dur(CLIP_DURATION * args.repeat)}")
    print("=" * 64)

    # ── AIBOX 算法任务：推流前给通道挂人员行为算法（默认参数）──────────────────
    algo_uuid = None
    algo_owned = False
    aibox = None
    if args.algo:
        aibox = AiboxClient(args.aibox_url, args.aibox_user, args.aibox_pass)
        if aibox.login():
            print(f"[AIBOX] 登录成功，准备为通道 {args.channel_code}({args.stream}) 挂人员行为算法任务")
            algo_uuid, algo_owned = ensure_algo_task(
                aibox, args.channel_code, args.stream, args.delete_algo_on_exit,
            )
        else:
            print("[AIBOX] 登录失败，跳过算法任务，仅推流。")
            aibox = None

    try:
      rounds_log = []   # 记录每轮视频起止时间，供告警统计
      if args.mode == "repeat":
        # ── repeat 模式：把每个视频自我复制延长后逐个推流 ──────────────────────
        all_videos = []
        for c in cat_order:
            all_videos.extend(cats[c])
        # offset：跳过前 N 个，用于分批测试
        pool = all_videos[args.offset:]
        total = min(args.rounds, len(pool))
        for rnd in range(1, total + 1):
            src = pool[rnd - 1]
            d = dur_of(src) or CLIP_DURATION
            extended_dur = d * args.repeat
            print(f"\n[第 {rnd}/{total} 轮] repeat：{src.name} ×{args.repeat} = {fmt_dur(extended_dur)}")
            merged = work / f"round_{rnd:02d}.mp4"
            ok, err = repeat_video(ffmpeg, src, args.repeat, merged)
            if not ok:
                print(f"    [复制延长失败] {err[:300]}")
                continue
            t0 = time.time()
            rc = push_stream(ffmpeg, merged, rtsp_url, log_path)
            t1 = time.time()
            if rc == 0:
                print(f"    [完成] 第 {rnd} 轮推流结束")
            else:
                print(f"    [警告] 推流退出码 {rc}（详见 {log_path}）")
            rounds_log.append({
                "round": rnd, "src": src, "duration": extended_dur,
                "start": t0, "end": t1,
            })
            if not args.keep and merged.exists():
                try:
                    merged.unlink()
                except Exception:
                    pass
        print(f"\n[全部结束] repeat 模式 {total} 轮推流完成。日志：{log_path}")
      else:
      # ── accumulate 模式：同类累积拼接、异类重置 ──────────────────────────────
        for rnd in range(1, args.rounds + 1):
          seg, cat = next_segment()
          if not accumulated:
            # 首轮或刚重置：累积为空，从这段开始
            accumulated = [(seg, cat)]
            acc_duration = dur_of(seg)
            action = f"开始(从 {cat} 起步)"
          elif cat == accumulated[-1][1]:
            # 同类 → 继续累积
            accumulated.append((seg, cat))
            acc_duration += dur_of(seg)
            action = f"同类累积 → {cat}，累计 {len(accumulated)} 段"
          else:
            # 不同类 → 丢弃旧累积，从最新这段重新开始
            old_cat = accumulated[-1][1]
            accumulated = [(seg, cat)]
            acc_duration = dur_of(seg)
            action = f"换类({old_cat}→{cat})，丢弃旧累积重新开始"

          # 补足最少 10s（同类）
          guard = 0
          while acc_duration < MIN_DURATION and guard < 10:
            seg2, cat2 = next_segment()
            # 补的若不同类，跳过本轮补足（保持类别一致），换下个
            if cat2 != accumulated[-1][1]:
                # 放回去：简单做法是不放回，直接取下一个同类——这里直接跳过此段继续取
                continue
            accumulated.append((seg2, cat2))
            acc_duration += dur_of(seg2)
            guard += 1

          seg_paths = [p for p, _ in accumulated]
          seg_cats = [c for _, c in accumulated]
          print(f"\n[第 {rnd}/{args.rounds} 轮] {action}")
          print(f"    片段({len(seg_paths)}): " + " + ".join(p.name for p in seg_paths))
          print(f"    类别: {seg_cats[0]}  时长: {fmt_dur(acc_duration)}")

          # 拼接
          merged = work / f"round_{rnd:02d}.mp4"
          ok, err = concat_segments(ffmpeg, seg_paths, merged)
          if not ok:
            print(f"    [拼接失败] {err[:300]}")
            continue
          # 推流
          t0 = time.time()
          rc = push_stream(ffmpeg, merged, rtsp_url, log_path)
          t1 = time.time()
          if rc == 0:
            print(f"    [完成] 第 {rnd} 轮推流结束")
          else:
            print(f"    [警告] 推流退出码 {rc}（详见 {log_path}）")
          rounds_log.append({
              "round": rnd, "src": seg_paths[0], "duration": acc_duration,
              "start": t0, "end": t1,
          })

          if not args.keep and merged.exists():
            try:
                merged.unlink()
            except Exception:
                pass

        print(f"\n[全部结束] {args.rounds} 轮推流完成。日志：{log_path}")
    finally:
      # ── 告警-视频对应统计：从 AIBOX 拉告警，按时间匹配到各轮视频 ──────────
      if rounds_log and aibox is not None:
          # 重新登录(token 可能过期)
          if not aibox.token or not aibox.login():
              print("[统计] 重新登录 AIBOX 失败，跳过告警统计。")
          else:
              try:
                  alarms = fetch_alarms(aibox)
                  report_alarm_vs_videos(rounds_log, alarms, args.stream)
              except Exception as e:
                  print(f"[统计] 生成告警统计失败：{e}")

      # ── 清理算法任务：本次新建的停止/删除，复用的保持不动 ──────────────────
      if aibox is not None and algo_uuid:
          if algo_owned:
              if args.delete_algo_on_exit:
                  if aibox.delete_task(algo_uuid):
                      print(f"[AIBOX] 已删除本次新建的算法任务：{algo_uuid}")
              else:
                  if aibox.stop_task(algo_uuid):
                      print(f"[AIBOX] 已停止本次新建的算法任务：{algo_uuid}")
          # 复用的任务（algo_owned=False）保持原状，不动它


if __name__ == "__main__":
    main()
