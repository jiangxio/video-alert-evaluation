#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按视频ID合并同源片段 → 重复N遍 → 逐条推流到 MediaMTX（RTSP）。

Fight 数据集下同一来源被切成多段，命名 ``{视频ID}_{段号}.avi``，如
``0Ow4cotKOuw_0..4``、``RTHTRS_697..720``、``nuf-d5GugL0_0..10``。
本脚本把同视频ID的片段按段号顺序拼回一条完整源视频，每条源视频自我重复
``--repeat`` 次（默认 2 = 重复1遍），再逐条用 ffmpeg -re 实时推流，
供 AIBOX 拉流检测。

底层函数（拼接/重复/推流/算法任务/告警统计）全部复用 stream_fight_loop.py，
本脚本只新增「按视频ID分组」+「合并→重复→推流」主循环。

用法：
  python scripts/stream_merged_sources.py
  python scripts/stream_merged_sources.py --rounds 2
  python scripts/stream_merged_sources.py --repeat 3 --no-algo --keep

依赖：ffmpeg + ffprobe 在 PATH 中。前置：MediaMTX 已监听 :8554。
"""

import argparse
import re
import sys
import tempfile
import time
from pathlib import Path

# 复用 stream_fight_loop.py 的底层函数（模块级无副作用，导入安全）
from stream_fight_loop import (
    AIBOX_DEFAULT_URL, AIBOX_DEFAULT_USER, AIBOX_DEFAULT_PASS,
    MEDIAMTX_DEFAULT_PORT, CLIP_DURATION,
    AiboxClient, ensure_algo_task,
    list_videos, concat_segments, repeat_video, push_stream,
    probe_duration, fmt_dur, which_ffmpeg, die,
    fetch_alarms, report_alarm_vs_videos,
)

# 文件名 → 段号 的正则：末尾 ``_数字`` 即段号，前面是视频ID前缀。
# 贪婪 .* 确保前缀里含 ``_`` 时（如 ``nuf-d5GugL0_10``）也能正确切分。
_SEGMENT_RE = re.compile(r"^(.*)_(\d+)$")


def group_by_source(videos: list[Path]) -> list[tuple[str, list[Path]]]:
    """按视频ID前缀分组，返回 [(prefix, [segment, ...]), ...]。

    - 文件名 stem 形如 ``{prefix}_{segno}``，去掉末尾 ``_<数字>`` 得到前缀。
    - 无段号后缀的文件（如纯 ``xxx.avi``）单独成组，前缀=stem，段号视为 0。
    - 组内按段号**数字**排序（避免 ``_10`` 排在 ``_2`` 前面）。
    - 组间按前缀字典序排序，作为推流顺序。
    """
    groups: dict[str, list[tuple[int, Path]]] = {}
    for p in videos:
        stem = p.stem
        m = _SEGMENT_RE.match(stem)
        if m:
            prefix, segno = m.group(1), int(m.group(2))
        else:
            prefix, segno = stem, 0
        groups.setdefault(prefix, []).append((segno, p))

    result = []
    for prefix in sorted(groups.keys()):
        segs = sorted(groups[prefix], key=lambda x: x[0])
        result.append((prefix, [p for _, p in segs]))
    return result


def main():
    ap = argparse.ArgumentParser(
        description="按视频ID合并同源片段 → 重复N遍 → 逐条推流到 MediaMTX（RTSP）。",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__,
    )
    ap.add_argument("--val-dir", default=r"F:\桌面\val\val",
                    help="数据集根目录（含 Fight/、NonFight/ 子目录）")
    ap.add_argument("--category", default="Fight",
                    help="类别子目录（默认 Fight）")
    ap.add_argument("-s", "--stream", default="fight", help="流名称（默认 fight）")
    ap.add_argument("--host", default="localhost", help="MediaMTX 地址（默认 localhost）")
    ap.add_argument("--port", type=int, default=MEDIAMTX_DEFAULT_PORT, help="RTSP 端口")
    ap.add_argument("--repeat", type=int, default=2,
                    help="每条源视频总播放次数（默认 2=重复1遍；1=不重复）")
    ap.add_argument("--rounds", type=int, default=0,
                    help="推流条数（源视频数），0=全部（默认 0）")
    ap.add_argument("--keep", action="store_true", help="保留合并/重复中间视频（默认推完即删）")
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

    if args.repeat < 1:
        die("--repeat 必须 >= 1")
    if not all(c.isalnum() or c in "-_" for c in args.stream):
        die("流名称只能含字母、数字、连字符和下划线。")

    val_dir = Path(args.val_dir)
    ffmpeg = which_ffmpeg()

    # 1. 列出类别下所有片段并按视频ID分组
    all_videos = list_videos(val_dir, args.category)
    print(f"[数据] {args.category}: {len(all_videos)} 个片段")
    groups = group_by_source(all_videos)
    print(f"[数据] 按视频ID合并为 {len(groups)} 条源视频")

    # --rounds 截断（0=全部）
    if args.rounds > 0:
        groups = groups[:args.rounds]
        print(f"[数据] --rounds={args.rounds}，取前 {len(groups)} 条推流")

    # 预读时长（用于展示，5s 片段用 ffprobe 实测更稳）
    dur_cache: dict[Path, float] = {}
    def dur_of(p: Path) -> float:
        if p not in dur_cache:
            dur_cache[p] = probe_duration(str(p)) or CLIP_DURATION
        return dur_cache[p]

    # 工作目录
    work = Path(tempfile.gettempdir()) / "stream_merged_sources"
    work.mkdir(parents=True, exist_ok=True)
    log_path = work / f"{args.stream}.log"
    log_path.write_text("", encoding="utf-8")

    rtsp_url = f"rtsp://{args.host}:{args.port}/{args.stream}"

    print("=" * 64)
    print(f"流名称    : {args.stream}")
    print(f"RTSP 地址 : {rtsp_url}")
    print(f"类别      : {args.category}")
    print(f"源视频数  : {len(groups)}")
    print(f"每条重复  : {args.repeat} 次" + ("（重复1遍）" if args.repeat == 2 else ""))
    print(f"画布      : 640x360（缩放+黑边，不拉伸）")
    print("=" * 64)
    # 预览分组
    for i, (prefix, segs) in enumerate(groups, 1):
        total = sum(dur_of(p) for p in segs)
        names = "+".join(p.name for p in segs)
        print(f"  [{i:>2}/{len(groups)}] {prefix:<16} ({len(segs)}段, {fmt_dur(total)}) {names}")

    # 2. 推流前给通道挂人员行为算法任务（默认参数）
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
      rounds_log = []
      for rnd, (prefix, segs) in enumerate(groups, 1):
        # 2a. 合并同源片段 → source_{prefix}.mp4
        merged = work / f"source_{rnd:02d}_{prefix}.mp4"
        print(f"\n[第 {rnd}/{len(groups)} 条] 合并 {prefix}（{len(segs)} 段）→ {merged.name}")
        ok, err = concat_segments(ffmpeg, segs, merged)
        if not ok:
            print(f"    [合并失败] {err[:300]}")
            continue
        src_dur = probe_duration(str(merged)) or sum(dur_of(p) for p in segs)

        # 2b. 重复 --repeat 次（默认 2 = 重复1遍）
        if args.repeat > 1:
            repeated = work / f"source_{rnd:02d}_{prefix}_x{args.repeat}.mp4"
            print(f"    重复 {args.repeat} 次 → {repeated.name}（{fmt_dur(src_dur * args.repeat)}）")
            ok, err = repeat_video(ffmpeg, merged, args.repeat, repeated)
            if not ok:
                print(f"    [重复失败] {err[:300]}")
                continue
            push_src = repeated
            push_dur = src_dur * args.repeat
        else:
            push_src = merged
            push_dur = src_dur

        # 2c. 推流
        t0 = time.time()
        rc = push_stream(ffmpeg, push_src, rtsp_url, log_path)
        t1 = time.time()
        if rc == 0:
            print(f"    [完成] 第 {rnd} 条推流结束")
        else:
            print(f"    [警告] 推流退出码 {rc}（详见 {log_path}）")
        rounds_log.append({
            "round": rnd, "src": segs[0], "duration": push_dur,
            "start": t0, "end": t1,
        })

        # 2d. 清理中间文件
        if not args.keep:
            for tmp in (merged, push_src if args.repeat > 1 else None):
                if tmp and tmp.exists():
                    try:
                        tmp.unlink()
                    except Exception:
                        pass

      print(f"\n[全部结束] {len(groups)} 条源视频推流完成。日志：{log_path}")
    finally:
      # 3. 告警-视频对应统计：从 AIBOX 拉告警，按时间匹配到各轮源视频
      if rounds_log and aibox is not None:
          if not aibox.token or not aibox.login():
              print("[统计] 重新登录 AIBOX 失败，跳过告警统计。")
          else:
              try:
                  alarms = fetch_alarms(aibox)
                  report_alarm_vs_videos(rounds_log, alarms, args.stream)
              except Exception as e:
                  print(f"[统计] 生成告警统计失败：{e}")

      # 4. 清理算法任务：本次新建的停止/删除，复用的保持不动
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
