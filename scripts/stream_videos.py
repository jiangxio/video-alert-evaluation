#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按顺序推流脚本 —— 把一两个视频按顺序推到 MediaMTX（RTSP），供 AIBOX 拉流检测。

完全复刻平台 six4 任务的推流方式（见 app/routes/streaming.py::_build_ffmpeg_cmd）：
  ffmpeg -re [-stream_loop N] -f concat -safe 0 -i <concat.txt> \
         -c copy -rtsp_transport tcp -f rtsp rtsp://localhost:8554/<流名>

两种推流模式（--mode 参数切换）：
  loop    （默认）：把所有视频拼成一轮，整轮循环 N 次。单个 ffmpeg 进程、单个
                   RTSP publisher，循环边界不会重建 mediamtx path、不会踢掉 AIBOX
                   的 reader。这就是 six4 用的模式。
  once           ：按顺序把每个视频各推一遍，不循环。仍是单个 ffmpeg 进程，用
                   concat demuxer 顺序拼接，推完即停。

用法示例：
  # 1) 两个视频整轮循环 20 次，推到流名 demo
  python scripts/stream_videos.py a.mp4 b.mp4 --stream demo --loop 20

  # 2) 两个视频按顺序各推一遍（不循环）
  python scripts/stream_videos.py a.mp4 b.mp4 --stream demo --mode once

  # 3) 指定 MediaMTX 地址和端口
  python scripts/stream_videos.py a.mp4 b.mp4 --stream demo --host 127.0.0.1

  # 4) 不带 -re（实时推流用 -re；若视频已是目标帧率且想尽快推可用 --no-realtime）
  python scripts/stream_videos.py a.mp4 --stream demo --no-realtime

依赖：ffmpeg + ffprobe 必须在 PATH 中（与平台一致）。
前置：MediaMTX 需已启动并监听 :8554（平台页面提示 ./tools/mediamtx）。
"""

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


MEDIAMTX_DEFAULT_PORT = 8554


def die(msg: str, code: int = 1):
    print(f"[错误] {msg}", file=sys.stderr)
    sys.exit(code)


def find_ffmpeg() -> str:
    ff = shutil.which("ffmpeg")
    if not ff:
        die("未找到 ffmpeg，请先安装并加入 PATH。")
    return ff


def find_ffprobe() -> str | None:
    return shutil.which("ffprobe")


def probe_duration(ffprobe: str, video_path: str) -> float | None:
    """用 ffprobe 取视频时长（秒），失败返回 None。"""
    try:
        r = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0 and r.stdout.strip():
            return float(r.stdout.strip())
    except Exception:
        pass
    return None


def fmt_duration(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return "?"
    s = int(round(seconds))
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def build_concat_list(video_paths: list[str], list_path: Path):
    """写 concat demuxer 列表文件。Windows 反斜杠需转成正斜杠。"""
    lines = []
    for vp in video_paths:
        safe = str(Path(vp).resolve()).replace("\\", "/")
        lines.append(f"file '{safe}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_ffmpeg_cmd(
    ffmpeg: str,
    concat_list: Path,
    rtsp_url: str,
    mode: str,
    loop_count: int,
    realtime: bool,
) -> list[str]:
    """
    构造 ffmpeg 推流命令。

    loop 模式：-stream_loop N 把整轮输入循环 N 次（N=loop_count-1，总播放 loop_count 遍）。
    once 模式：不循环，concat 列表里的视频按顺序各推一遍。
    """
    cmd = [ffmpeg]
    if realtime:
        cmd += ["-re"]                       # 按原速读，实时推流必备
    if mode == "loop":
        n = max(0, loop_count - 1)           # -stream_loop N = 总共播放 N+1 遍
        if n > 0:
            cmd += ["-stream_loop", str(n)]
    cmd += ["-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-c", "copy", "-rtsp_transport", "tcp", "-f", "rtsp", rtsp_url]
    return cmd


def main():
    ap = argparse.ArgumentParser(
        description="按顺序把视频推流到 MediaMTX（RTSP），复刻平台 six4 的推流方式。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("videos", nargs="+", help="要推流的视频文件路径（按播放顺序）")
    ap.add_argument("-s", "--stream", required=True,
                    help="流名称（只能含字母、数字、连字符、下划线），如 six4、demo")
    ap.add_argument("--host", default="localhost",
                    help="MediaMTX 地址（默认 localhost；要让别的机器拉流可显式传本机 IP）")
    ap.add_argument("--port", type=int, default=MEDIAMTX_DEFAULT_PORT,
                    help=f"MediaMTX RTSP 端口（默认 {MEDIAMTX_DEFAULT_PORT}）")
    ap.add_argument("--mode", choices=["loop", "once"], default="loop",
                    help="loop=整轮循环 N 次（默认，同 six4）；once=按顺序各推一遍不循环")
    ap.add_argument("-l", "--loop", type=int, default=20,
                    help="loop 模式下的整轮循环次数（默认 20）")
    ap.add_argument("--no-realtime", dest="realtime", action="store_false",
                    help="不加 -re（默认加 -re 实时推流；调试时可去掉以最快速度推）")
    ap.add_argument("--keep-list", action="store_true",
                    help="保留 concat 列表临时文件（默认推完即删）")
    args = ap.parse_args()

    # 校验流名称
    if not args.stream or not all(c.isalnum() or c in "-_" for c in args.stream):
        die("流名称只能包含字母、数字、连字符和下划线。")

    # 校验视频文件
    videos = []
    for vp in args.videos:
        p = Path(vp)
        if not p.exists():
            die(f"视频文件不存在：{vp}")
        videos.append(str(p.resolve()))
    if len(videos) > 2:
        print(f"[警告] 传入 {len(videos)} 个视频，本脚本面向一两个视频；将全部按顺序推流。")

    ffmpeg = find_ffmpeg()
    ffprobe = find_ffprobe()

    # 取时长（仅用于展示预计时长）
    durations = []
    if ffprobe:
        for vp in videos:
            durations.append(probe_duration(ffprobe, vp))

    rtsp_url = f"rtsp://{args.host}:{args.port}/{args.stream}"

    # 写 concat 列表（临时文件，推完删除）
    list_dir = Path(tempfile.gettempdir()) / "stream_videos"
    list_dir.mkdir(parents=True, exist_ok=True)
    concat_list = list_dir / f"{args.stream}_{os.getpid()}.txt"
    build_concat_list(videos, concat_list)

    # 预计总时长
    round_dur = sum(d for d in durations if d) if durations else None
    if args.mode == "loop":
        total = round_dur * args.loop if round_dur else None
        mode_desc = f"整轮循环 {args.loop} 次"
    else:
        total = round_dur
        mode_desc = "顺序各推一遍（不循环）"

    print("=" * 64)
    print(f"流名称    : {args.stream}")
    print(f"RTSP 地址 : {rtsp_url}")
    print(f"模式      : {args.mode}（{mode_desc}）")
    print(f"视频列表  :")
    for i, vp in enumerate(videos):
        d = durations[i] if i < len(durations) else None
        print(f"  [{i + 1}] {Path(vp).name}  ({fmt_duration(d)})")
    print(f"预计总时长: {fmt_duration(total)}")
    print(f"实时推流  : {'是 (-re)' if args.realtime else '否'}")
    print("=" * 64)

    cmd = build_ffmpeg_cmd(
        ffmpeg, concat_list, rtsp_url, args.mode, args.loop, args.realtime,
    )
    print("[命令]", " ".join(f'"{a}"' if " " in a else a for a in cmd))
    print("[推流中...] 按 Ctrl+C 提前停止。\n")

    # Windows: CREATE_NEW_PROCESS_GROUP 让 ffmpeg 独立，Ctrl+C 不会连带杀掉本脚本
    # 之外的进程；CREATE_NO_WINDOW 避免弹黑窗。
    popen_kwargs = dict()
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )

    log_path = list_dir / f"{args.stream}_{os.getpid()}.log"
    proc = None
    try:
        with open(log_path, "w", encoding="utf-8") as log_fp:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=log_fp, **popen_kwargs)
            print(f"[ffmpeg pid={proc.pid}] 日志：{log_path}")
            # 实时把 ffmpeg stderr 的进度行回显（ffmpeg 进度走 stderr，已重定向到文件）
            # 这里简单 wait；想看实时进度可 tail 日志文件。
            rc = proc.wait()
            print()
            if rc == 0:
                print(f"[完成] 推流正常结束（退出码 0）。")
            elif rc < 0:
                print(f"[停止] ffmpeg 被信号终止（{-rc}）。")
            else:
                print(f"[失败] ffmpeg 退出码 {rc}，详见日志：{log_path}")
                sys.exit(rc)
    except KeyboardInterrupt:
        print("\n[中断] 收到 Ctrl+C，正在停止 ffmpeg ...")
        if proc is not None and proc.poll() is None:
            try:
                if os.name == "nt":
                    proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                print("[停止] 已终止 ffmpeg。")
            except Exception as e:
                print(f"[停止] 终止 ffmpeg 时出错：{e}")
        sys.exit(130)
    finally:
        if not args.keep_list:
            try:
                concat_list.unlink()
            except Exception:
                pass


if __name__ == "__main__":
    main()
