"""RTSP 推流路由

创建推流任务 → 启动 FFmpeg 推流到 MediaMTX → 监控状态
MediaMTX 需用户手动启动：tools/mediamtx（默认监听 :8554）
"""

import errno
import json
import os
import signal
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from flask import Blueprint, request, jsonify, render_template, current_app, has_app_context

from app.database import get_db, DATABASE_PATH

bp = Blueprint("streaming", __name__, url_prefix="/streaming")

# task_id -> Popen
_stream_processes = {}
_stream_lock = threading.Lock()

# 防止 _sync_running_status 与 _monitor_video_process 同时触发重连/切换
_reconnecting_tasks = set()
_reconnect_lock = threading.Lock()

MEDIAMTX_PORT = 8554


def _is_pid_alive(pid: int) -> bool:
    """检查指定 PID 的进程是否仍在运行（跨平台，且不会误杀进程）。

    注意：Windows 上 ``os.kill(pid, 0)`` 会调用 ``TerminateProcess`` 直接杀掉目标
    进程，用于轮询推流状态时会杀光所有 FFmpeg。这里在 Windows 上改用
    ``OpenProcess`` + ``GetExitCodeProcess`` 探测，不产生副作用；POSIX 上仍用
    ``os.kill(pid, 0)``。
    """
    if not pid:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # PROCESS_QUERY_LIMITED_INFORMATION
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            STILL_ACTIVE = 259
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _sync_running_status(db: sqlite3.Connection, app=None, allow_restart: bool = True):
    """
    扫描数据库中 status='running' 的任务，校验对应 pid 是否还活着。
    若进程已消失：
      - 错误可重试（broken pipe 等）且允许重连 → 从任务开头重新推流
      - 否则 → 标记为 failed
    应在 list_tasks() 返回前、以及应用启动时调用。
    应用启动时 allow_restart=False，因为此时没有 app context，也不适合自动恢复。
    """
    cur = db.cursor()
    cur.execute(
        "SELECT id, pid, log_path, loop_count, restart_count, max_restarts, started_at "
        "FROM stream_tasks WHERE status = 'running'"
    )
    dead_tasks = [dict(row) for row in cur.fetchall() if not _is_pid_alive(row["pid"])]
    for task in dead_tasks:
        task_id = task["id"]
        with _reconnect_lock:
            if task_id in _reconnecting_tasks:
                # 已经有监控线程在处理重连，避免重复起 FFmpeg
                continue
            _reconnecting_tasks.add(task_id)

        pid = task["pid"]
        log_path = task["log_path"] if "log_path" in task else None

        stderr_output = ""
        if log_path:
            try:
                stderr_output = Path(log_path).read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass

        retryable = _is_retryable_error(stderr_output)
        restart_count = int(task["restart_count"] or 0)
        max_restarts = int(task["max_restarts"] or 3)
        total_loops = task["loop_count"] or 1
        error_msg = stderr_output[-500:] if stderr_output else f"FFmpeg 进程 (pid={pid}) 已退出"

        if retryable and restart_count < max_restarts and allow_restart and app is not None:
            # 断流重连保留进度：从任务 started_at 算整轮偏移作为续播点
            resume_offset = _compute_resume_offset(task_id, task["started_at"])
            cur.execute(
                "UPDATE stream_tasks SET restart_count = restart_count + 1, last_error = ?, "
                "resume_video_index = 0, resume_offset = ?, resume_loop = 1, "
                "resume_at = CURRENT_TIMESTAMP WHERE id = ?",
                (error_msg, resume_offset, task_id),
            )
            db.commit()
            threading.Thread(
                target=_delayed_play_video,
                args=(task_id, total_loops, app, resume_offset),
                daemon=True,
            ).start()
        else:
            with _reconnect_lock:
                _reconnecting_tasks.discard(task_id)
            cur.execute(
                "UPDATE stream_tasks SET status = 'failed', error_message = ?, restart_count = 0, "
                "resume_video_index = 0, resume_offset = 0, resume_loop = 1, "
                "resume_at = CURRENT_TIMESTAMP, ended_at = CURRENT_TIMESTAMP WHERE id = ?",
                (error_msg, task_id),
            )
            db.commit()


def _delayed_play_video(task_id: int, total_loops: int, app=None, resume_offset: float = 0.0):
    """等待几秒后重新推流（resume_offset 保留断流进度，0=从头）；函数退出时释放重连锁"""
    try:
        time.sleep(5)
        if app is not None and not has_app_context():
            with app.app_context():
                _play_video(task_id, total_loops, resume_offset=resume_offset, app=app)
        else:
            _play_video(task_id, total_loops, resume_offset=resume_offset, app=app)
    finally:
        with _reconnect_lock:
            _reconnecting_tasks.discard(task_id)


def init_streaming_cleanup():
    """应用启动时执行一次：将数据库中已死亡的 running 任务修正为 failed（不自动恢复）"""
    try:
        conn = sqlite3.connect(str(DATABASE_PATH))
        conn.row_factory = sqlite3.Row
        _sync_running_status(conn, app=None, allow_restart=False)
        conn.close()
    except Exception:
        pass



def _get_local_ips() -> list:
    """获取所有真实网卡的 IPv4 地址（排除 loopback、docker、虚拟网卡）

    跨平台实现：Windows 走 socket.gethostbyname_ex，Linux 走 fcntl.ioctl，
    二者均失败时回退到 UDP 连接法。fcntl 是 Linux 专属模块，必须延迟导入
    并包裹在 try 内，否则在 Windows 上会直接 ModuleNotFoundError。
    """
    results = []
    try:
        import fcntl
        import struct
        import array

        SIOCGIFCONF = 0x8912
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        buf = array.array("B", b"\0" * 4096)
        ifreq = struct.pack("iL", 4096, buf.buffer_info()[0])
        res = fcntl.ioctl(s.fileno(), SIOCGIFCONF, ifreq)
        s.close()
        outbytes = struct.unpack("iL", res)[0]
        namestr = buf.tobytes()
        offset = 0
        while offset < outbytes:
            iface = namestr[offset:offset + 16].split(b"\0", 1)[0].decode()
            addr = socket.inet_ntoa(namestr[offset + 20:offset + 24])
            offset += 40
            if iface.startswith(("lo", "docker", "virbr", "br-", "veth")):
                continue
            if addr.startswith("127."):
                continue
            results.append({"iface": iface, "ip": addr})
    except Exception:
        # Windows 或无 fcntl 的平台：用 gethostbyname_ex 枚举本机地址
        try:
            hostname = socket.gethostname()
            try:
                _, _, ip_list = socket.gethostbyname_ex(hostname)
            except Exception:
                ip_list = [socket.gethostbyname(hostname)]
            for ip in ip_list:
                if ip.startswith("127."):
                    continue
                results.append({"iface": "default", "ip": ip})
        except Exception:
            pass

    if not results:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            results = [{"iface": "default", "ip": ip}]
        except Exception:
            results = [{"iface": "localhost", "ip": "127.0.0.1"}]
    return results


def _get_local_ip() -> str:
    """兼容旧调用，返回第一个 IP"""
    ips = _get_local_ips()
    return ips[0]["ip"] if ips else "127.0.0.1"


def _parse_started_at(started_raw) -> datetime | None:
    """将数据库中的 started_at 解析为带 UTC 时区的 datetime 对象"""
    if not started_raw:
        return None
    try:
        if isinstance(started_raw, str) and "," in started_raw:
            # "Fri, 29 May 2026 09:00:04 GMT"
            return parsedate_to_datetime(started_raw).astimezone(timezone.utc)
        elif isinstance(started_raw, str):
            # "2026-05-29 09:00:04"
            return datetime.strptime(started_raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        else:
            # datetime 对象（PARSE_DECLTYPES 解析结果）
            return started_raw.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _calc_progress(elapsed: float, videos: list[dict], loop_count: int) -> dict:
    """
    根据已播放秒数计算当前播放位置。
    videos: [{"filename": str, "video_id": str, "duration": float}, ...]
    返回：{
        current_loop, total_loops, loop_progress_seconds, loop_progress_percent,
        current_video_index, current_video_offset, current_video_percent,
        overall_percent, is_finished
    }
    """
    if not videos:
        return {
            "current_loop": 0, "total_loops": loop_count, "loop_progress_seconds": 0,
            "loop_progress_percent": 0, "current_video_index": 0,
            "current_video_offset": 0, "current_video_percent": 0,
            "overall_percent": 0, "is_finished": False,
        }

    round_duration = sum(v.get("duration") or 0 for v in videos)
    if round_duration <= 0:
        return {
            "current_loop": 0, "total_loops": loop_count, "loop_progress_seconds": 0,
            "loop_progress_percent": 0, "current_video_index": 0,
            "current_video_offset": 0, "current_video_percent": 0,
            "overall_percent": 0, "is_finished": False,
        }

    total_duration = round_duration * loop_count
    elapsed = max(0, min(elapsed, total_duration))
    is_finished = elapsed >= total_duration

    current_loop = min(int(elapsed // round_duration) + 1, loop_count)
    loop_progress_seconds = elapsed % round_duration
    loop_progress_percent = round((loop_progress_seconds / round_duration) * 100, 1)
    overall_percent = round((elapsed / total_duration) * 100, 1) if total_duration > 0 else 0

    # 找到当前视频
    current_video_index = 0
    current_video_offset = loop_progress_seconds
    accumulated = 0
    for idx, v in enumerate(videos):
        dur = v.get("duration") or 0
        if accumulated + dur > loop_progress_seconds or idx == len(videos) - 1:
            current_video_index = idx
            current_video_offset = loop_progress_seconds - accumulated
            break
        accumulated += dur

    current_video = videos[current_video_index]
    current_video_duration = current_video.get("duration") or 0
    current_video_percent = round((current_video_offset / current_video_duration) * 100, 1) if current_video_duration > 0 else 0

    return {
        "current_loop": current_loop,
        "total_loops": loop_count,
        "loop_progress_seconds": round(loop_progress_seconds, 1),
        "loop_progress_percent": loop_progress_percent,
        "current_video_index": current_video_index,
        "current_video_offset": round(current_video_offset, 1),
        "current_video_percent": current_video_percent,
        "overall_percent": overall_percent,
        "is_finished": is_finished,
    }


def _calc_elapsed_seconds(
    video_list: list[dict],
    current_loop: int | None,
    current_video_index: int | None,
    video_started_at,
    ref_ts: float | None = None,
    total_duration: float | None = None,
) -> float:
    """
    计算任务总已播放秒数。

    单进程推流模式下，started_at 是整个任务的开始时间（全程不再重置），
    因此 elapsed = now - started_at，并用 total_duration 封顶。
    保留 current_loop/current_video_index 参数仅为向后兼容，不再参与计算。
    """
    if not video_list:
        return 0.0
    round_duration = sum(v.get("duration") or 0 for v in video_list)
    if round_duration <= 0:
        return 0.0

    if not video_started_at:
        return 0.0

    started = _parse_started_at(video_started_at)
    if not started:
        return 0.0

    ref = ref_ts if ref_ts is not None else time.time()
    elapsed = ref - started.timestamp()
    elapsed = max(0.0, elapsed)

    # 用 total_duration 封顶（任务总时长 = 单轮时长 × 循环数）
    cap = total_duration if total_duration and total_duration > 0 else round_duration
    return min(elapsed, cap)


# ── 工具函数 ────────────────────────────────────────────────────────────────

def _load_algorithm_names(config_path: str) -> dict:
    """返回 event_type -> 中文名映射（从中央注册表读取）"""
    from app.event_types import get_type_names

    return get_type_names()


def _get_video_duration(video_path: str) -> float | None:
    """用 ffprobe 获取视频时长（秒），失败返回 None"""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            val = result.stdout.strip()
            if val:
                return float(val)
    except Exception:
        pass
    return None


def _ensure_duration(wm_id: int, video_path: str) -> float | None:
    """确保 watermarked_videos.duration 有值，缺失时用 ffprobe 补全"""
    conn = sqlite3.connect(str(DATABASE_PATH))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute("SELECT duration FROM watermarked_videos WHERE id = ?", (wm_id,))
        row = cur.fetchone()
        if row and row["duration"]:
            return float(row["duration"])
        duration = _get_video_duration(video_path)
        if duration is not None:
            cur.execute(
                "UPDATE watermarked_videos SET duration = ? WHERE id = ?",
                (duration, wm_id),
            )
            conn.commit()
        return duration
    finally:
        conn.close()


def _probe_codecs(video_path: str) -> tuple[str | None, str | None]:
    """用 ffprobe 探测视频/音频编码，返回 (video_codec, audio_codec)。

    无音频流时 acodec=None；探测失败（ffprobe 不在/超时/非视频文件）返回 (None, None)。
    纯探测函数，便于单测（monkeypatch streaming.subprocess.run 喂 canned 输出）。
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "stream=codec_name,codec_type",
                "-of", "csv=p=0",
                str(video_path),
            ],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return None, None
        vcodec = acodec = None
        for line in result.stdout.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 2:
                continue
            cname, ctype = parts[0], parts[1]
            if ctype == "video":
                vcodec = cname
            elif ctype == "audio":
                acodec = cname
        return vcodec, acodec
    except Exception:
        return None, None


def _probe_codec_compatible(video_path: str) -> bool:
    """探测源编码是否对 RTSP/MediaMTX 友好（可直接 -c copy）。

    判定：视频编码为 h264/hevc/h265 且（无音频 或 音频为 aac）→ True（可 copy）；
    其余 → False（需转码兜底）。探测失败按 True 处理（保留旧的 copy 默认行为，
    若 copy 失败由断流重连兜底），避免 ffprobe 不可用时一律走昂贵的转码。
    """
    vcodec, acodec = _probe_codecs(video_path)
    if vcodec is None:
        return True
    video_ok = vcodec in ("h264", "hevc", "h265")
    audio_ok = acodec is None or acodec == "aac"
    return video_ok and audio_ok


def _get_suggested_algorithms(video_db_ids: list[int], config_path: str) -> list[str]:
    """根据视频 DB ID 列表，从 events 表汇总所有事件类型"""
    if not video_db_ids:
        return []
    conn = sqlite3.connect(str(DATABASE_PATH))
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" * len(video_db_ids))
        cur = conn.cursor()
        cur.execute(
            f"SELECT DISTINCT event_type FROM events WHERE video_db_id IN ({placeholders})",
            video_db_ids,
        )
        return [r["event_type"] for r in cur.fetchall()]
    finally:
        conn.close()


def _resolve_watermarked_videos(source_type: str, source_id: int) -> tuple[list[dict], str | None]:
    """
    根据来源类型解析出打水印视频列表。
    返回 (videos, error_message)
    videos: [
        {
            "wm_id": int,
            "path": str,
            "video_id": str,      # 原视频 video_id
            "filename": str,      # 原视频文件名
            "wm_filename": str,   # 打水印后视频文件名
            "video_db_id": int
        }, ...
    ]
    """
    conn = sqlite3.connect(str(DATABASE_PATH))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()

        if source_type == "single":
            cur.execute(
                "SELECT wv.id, wv.output_path, wv.duration, v.video_id, v.filename, "
                "wv.filename as wm_filename, v.id as video_db_id "
                "FROM watermarked_videos wv JOIN videos v ON wv.original_video_id = v.id "
                "WHERE wv.id = ?",
                (source_id,),
            )
            row = cur.fetchone()
            if not row:
                return [], "打水印视频不存在"
            return [dict(row)], None

        elif source_type == "set":
            cur.execute(
                "SELECT id, name, video_ids FROM eval_video_sets WHERE id = ?",
                (source_id,),
            )
            vset = cur.fetchone()
            if not vset:
                return [], "视频集不存在"
            try:
                video_db_ids = json.loads(vset["video_ids"] or "[]")
            except Exception:
                return [], "视频集数据格式错误"
            if not video_db_ids:
                return [], "视频集为空"

            placeholders = ",".join("?" * len(video_db_ids))
            cur.execute(
                f"SELECT wv.id, wv.output_path, wv.duration, v.video_id, v.filename, "
                f"wv.filename as wm_filename, v.id as video_db_id "
                f"FROM watermarked_videos wv JOIN videos v ON wv.original_video_id = v.id "
                f"WHERE v.id IN ({placeholders})",
                video_db_ids,
            )
            rows = cur.fetchall()
            found_ids = {r["video_db_id"] for r in rows}
            missing = [vid for vid in video_db_ids if vid not in found_ids]
            if missing:
                cur.execute(
                    f"SELECT filename FROM videos WHERE id IN ({','.join('?'*len(missing))})",
                    missing,
                )
                names = [r["filename"] for r in cur.fetchall()]
                return [], f"以下视频尚未打水印，请先处理：{', '.join(names)}"
            return [dict(r) for r in rows], None

        return [], "未知来源类型"
    finally:
        conn.close()


def _get_concat_list_path(task_id: int | None) -> Path:
    """获取任务 concat 列表文件路径（单进程推流用）"""
    base = Path(__file__).resolve().parent.parent.parent / "tmp" / "stream_concat"
    return base / f"task_{task_id}.txt"


def _get_resume_video_path(task_id: int | None) -> Path:
    """获取任务续播临时视频文件路径"""
    base = Path(__file__).resolve().parent.parent.parent / "tmp" / "stream_resume"
    return base / f"task_{task_id}.mp4"


def _cleanup_resume_file(task_id: int | None):
    """清理任务的续播临时视频文件"""
    if not task_id:
        return
    for p in (_get_resume_video_path(task_id), _get_concat_list_path(task_id)):
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass


def _build_ffmpeg_cmd(
    video_paths: list[str],
    stream_name: str,
    total_loops: int,
    task_id: int | None = None,
    resume_offset: float = 0,
    transcode: bool = False,
) -> list[str]:
    """
    构建单进程 FFmpeg 推流命令：用 concat demuxer 把整轮视频拼成一个输入，
    再用 -stream_loop 循环整轮，全程只开一个 ffmpeg、一个 RTSP publisher，
    避免每轮循环重建 mediamtx path 导致 reader 被踢。

    video_paths: 一轮内的视频文件路径列表（按播放顺序）。
    total_loops: 总循环次数。
    resume_offset: 续播偏移（秒），从该偏移处开始推流（作用于拼接后的整轮输入）。
    transcode: 源编码不兼容时走转码兜底（-c:v libx264 -preset veryfast -c:a aac），
        默认 False 走 -c copy。
    """
    rtsp_url = f"rtsp://localhost:{MEDIAMTX_PORT}/{stream_name}"
    resume_offset = float(resume_offset or 0)

    # 生成 concat 列表文件
    concat_path = _get_concat_list_path(task_id)
    concat_path.parent.mkdir(parents=True, exist_ok=True)
    # concat demuxer 要求路径用正斜杠或转义，且文件需存在
    lines = []
    for vp in video_paths:
        # Windows 路径反斜杠在 concat 文件里需转义为正斜杠
        safe = vp.replace("\\", "/")
        lines.append(f"file '{safe}'")
    concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    input_path = str(concat_path)

    # 续播：从指定偏移截取整轮输入后再循环推流
    if resume_offset > 0:
        resume_path = _get_resume_video_path(task_id)
        resume_path.parent.mkdir(parents=True, exist_ok=True)
        if resume_path.exists():
            try:
                resume_path.unlink()
            except Exception:
                pass

        result = subprocess.run(
            [
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-ss", str(resume_offset),
                "-i", concat_path,
                *(["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac"]
                  if transcode else ["-c", "copy"]),
                "-avoid_negative_ts", "make_zero",
                str(resume_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"截取续播视频失败：{concat_path} @ {resume_offset}s")
        input_path = str(resume_path)
        input_is_concat = False
    else:
        input_is_concat = True

    # -stream_loop N：输入循环 N 次（N=0 表示不循环即播 1 次）
    loop_count = max(0, total_loops - 1)
    cmd = ["ffmpeg", "-re"]
    if loop_count > 0:
        cmd += ["-stream_loop", str(loop_count)]
    if input_is_concat:
        # concat demuxer 需显式声明格式；-safe 0 允许任意路径
        cmd += ["-f", "concat", "-safe", "0"]
    if transcode:
        # 源编码不兼容 RTSP/MediaMTX：转码兜底，-maxrate/-bufsize 稳定码率
        out_codec = ["-c:v", "libx264", "-preset", "veryfast",
                     "-maxrate", "2M", "-bufsize", "4M", "-c:a", "aac"]
    else:
        out_codec = ["-c", "copy"]
    cmd += ["-i", input_path, *out_codec, "-rtsp_transport", "tcp", "-f", "rtsp", rtsp_url]
    return cmd


def _is_retryable_error(stderr_output: str) -> bool:
    """判断 FFmpeg 错误是否属于可自动重试的网络/连接问题"""
    if not stderr_output:
        return False
    retryable_keywords = [
        "broken pipe",
        "connection reset",
        "connection refused",
        "network is unreachable",
        "no route to host",
        "connection timed out",
        "error writing trailer",
        "error muxing a packet",
        "task finished with error code: -32",
    ]
    lower = stderr_output.lower()
    return any(k in lower for k in retryable_keywords)


def _compute_resume_position(task_id: int, started_at) -> tuple[int | None, float | None, int | None, float | None]:
    """根据任务开始时间计算当前播放位置，用于断流续播。

    返回 (current_video_index, current_video_offset, current_loop, round_offset)。
    round_offset 是整轮输入内的偏移秒数（= loop_progress_seconds），正是单进程
    _build_ffmpeg_cmd 的 resume_offset 所需。任务未开始/已结束/无法计算时全 None。
    """
    if not started_at:
        return None, None, None, None
    started = _parse_started_at(started_at)
    if not started:
        return None, None, None, None

    conn = sqlite3.connect(str(DATABASE_PATH))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT source_type, source_id, loop_count FROM stream_tasks WHERE id = ?",
            (task_id,),
        )
        task = cur.fetchone()
        if not task:
            return None, None, None, None

        elapsed = time.time() - started.timestamp()
        videos, err = _resolve_watermarked_videos(task["source_type"], task["source_id"])
        if err:
            return None, None, None, None

        video_list = []
        for v in videos:
            dur = _ensure_duration(v["id"], v["output_path"])
            video_list.append({"duration": dur or 0})

        progress = _calc_progress(elapsed, video_list, task["loop_count"] or 1)
        if progress["is_finished"]:
            return None, None, None, None
        return (progress["current_video_index"], progress["current_video_offset"],
                progress["current_loop"], progress["loop_progress_seconds"])
    finally:
        conn.close()


def _compute_resume_offset(task_id: int, started_at) -> float:
    """整轮输入内的续播偏移秒数（断流重连保留进度用）。无法计算返 0.0。"""
    *_unused, round_offset = _compute_resume_position(task_id, started_at)
    return float(round_offset or 0.0)


def _set_task_failed(task_id: int, error_msg: str):
    """将任务标记为 failed 并记录错误"""
    try:
        conn = sqlite3.connect(str(DATABASE_PATH))
        cur = conn.cursor()
        cur.execute(
            "UPDATE stream_tasks SET status = 'failed', error_message = ?, restart_count = 0, "
            "ended_at = CURRENT_TIMESTAMP WHERE id = ?",
            (error_msg[:500], task_id),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def _play_video(
    task_id: int,
    total_loops: int,
    resume_offset: float = 0,
    app=None,
) -> tuple[bool, dict]:
    """
    启动整个推流任务的单个 FFmpeg 进程：用 concat + -stream_loop 把所有视频、
    所有循环一次性推完。全程只有一个 RTSP publisher，mediamtx path 不会因
    循环边界而重建，reader 不会被踢。

    返回 (success, result)：
      - success=True 时 result 包含 status, pid, rtsp_urls
      - success=False 时 result 包含 error
    """
    # 后台线程调用时需要 app context 才能使用 get_db / current_app
    if app is None and has_app_context():
        app = current_app._get_current_object()
    ctx = None
    if app is not None and not has_app_context():
        ctx = app.app_context()
        ctx.push()
    try:
        db = get_db()
        cur = db.cursor()
        cur.execute(
            "SELECT id, source_type, source_id, stream_name, loop_count, status, transcode "
            "FROM stream_tasks WHERE id = ?",
            (task_id,),
        )
        task = cur.fetchone()
        if not task or task["status"] != "running":
            return False, {"error": "任务状态已改变"}

        videos, err = _resolve_watermarked_videos(task["source_type"], task["source_id"])
        if err:
            _set_task_failed(task_id, err)
            return False, {"error": err}

        video_count = len(videos)
        if video_count == 0:
            _set_task_failed(task_id, "视频列表为空")
            return False, {"error": "视频列表为空"}

        # 校验所有视频文件存在
        video_paths = []
        for v in videos:
            vp = v["output_path"]
            if not Path(vp).exists():
                err = f"视频文件不存在：{vp}"
                _set_task_failed(task_id, err)
                return False, {"error": err}
            video_paths.append(vp)

        try:
            cmd = _build_ffmpeg_cmd(
                video_paths, task["stream_name"], total_loops,
                task_id=task_id, resume_offset=resume_offset,
                transcode=bool(task["transcode"] or 0),
            )
        except RuntimeError as e:
            _set_task_failed(task_id, str(e))
            return False, {"error": str(e)}

        log_dir = Path(current_app.root_path).parent / "logs" / "stream"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"task_{task_id}.log"

        try:
            log_fp = open(log_path, "w", encoding="utf-8")
            # Windows: 子进程需脱离父进程控制台进程组，否则 Ctrl+C / 控制台事件
            # 会同时发给 run.py 和 ffmpeg，导致 web 服务随 ffmpeg 一起被信号杀掉。
            # CREATE_NEW_PROCESS_GROUP 让 ffmpeg 独立，CREATE_NO_WINDOW 避免弹窗。
            popen_kwargs = dict(stdout=subprocess.DEVNULL, stderr=log_fp)
            if os.name == "nt":
                popen_kwargs["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                )
            process = subprocess.Popen(cmd, **popen_kwargs)
        except FileNotFoundError:
            log_fp.close()
            err = "ffmpeg 未安装或不在 PATH 中"
            _set_task_failed(task_id, err)
            return False, {"error": err}

        # 单进程模式：started_at 表示整个任务的开始时间，全程不再重置
        with _stream_lock:
            _stream_processes[task_id] = (process, log_fp)

        cur.execute(
            "UPDATE stream_tasks SET status = 'running', pid = ?, log_path = ?, "
            "current_video_index = 0, current_loop = 1, "
            "resume_video_index = NULL, resume_offset = NULL, resume_loop = NULL, resume_at = NULL, "
            "started_at = CURRENT_TIMESTAMP, error_message = NULL, ended_at = NULL WHERE id = ?",
            (process.pid, str(log_path), task_id),
        )
        db.commit()

        time.sleep(2.5)
        returncode = process.poll()

        if returncode is not None:
            try:
                log_fp.close()
            except Exception:
                pass
            with _stream_lock:
                _stream_processes.pop(task_id, None)
            stderr_output = ""
            try:
                stderr_output = log_path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
            error_msg = stderr_output[-500:] if stderr_output else f"FFmpeg 退出码 {returncode}"
            _set_task_failed(task_id, error_msg)
            return False, {"error": error_msg}

        threading.Thread(
            target=_monitor_video_process,
            args=(task_id, process, log_fp, log_path, total_loops, app),
            daemon=True,
        ).start()

        rtsp_urls = [
            {"iface": entry["iface"], "url": f"rtsp://{entry['ip']}:{MEDIAMTX_PORT}/{task['stream_name']}"}
            for entry in _get_local_ips()
        ]
        return True, {"status": "running", "pid": process.pid, "rtsp_urls": rtsp_urls}
    finally:
        if ctx is not None:
            ctx.pop()


def _monitor_video_process(
    task_id: int,
    process: subprocess.Popen,
    log_fp,
    log_path: Path,
    total_loops: int,
    app=None,
):
    """后台线程：单进程推流结束后，决定任务完成、重试还是失败。

    单进程模式下 ffmpeg 推完所有循环才会退出，因此 returncode==0 即代表
    整个任务正常完成，标记 done；无需再递归播放下一个视频。
    """
    try:
        process.wait()
    except Exception:
        pass
    returncode = process.returncode

    try:
        log_fp.close()
    except Exception:
        pass

    with _stream_lock:
        _stream_processes.pop(task_id, None)

    stderr_output = ""
    try:
        stderr_output = log_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass

    conn = sqlite3.connect(str(DATABASE_PATH))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT status, restart_count, max_restarts, started_at FROM stream_tasks WHERE id = ?",
            (task_id,),
        )
        row = cur.fetchone()
        if not row or row["status"] != "running":
            return

        if returncode == 0:
            # 整个任务所有循环正常推完
            cur.execute(
                "UPDATE stream_tasks SET status = 'done', error_message = NULL, restart_count = 0, "
                "resume_video_index = NULL, resume_offset = NULL, resume_loop = NULL, resume_at = NULL, "
                "ended_at = CURRENT_TIMESTAMP WHERE id = ?",
                (task_id,),
            )
            conn.commit()
            _cleanup_resume_file(task_id)
            return

        error_msg = stderr_output[-500:] if stderr_output else f"FFmpeg 退出码 {returncode}"
        retryable = _is_retryable_error(stderr_output)
        restart_count = int(row["restart_count"] or 0)
        max_restarts = int(row["max_restarts"] or 3)

        if retryable and restart_count < max_restarts:
            # 可重试错误：从断流处续播（保留整轮进度）
            with _reconnect_lock:
                _reconnecting_tasks.add(task_id)
            try:
                resume_offset = _compute_resume_offset(task_id, row["started_at"])
                cur.execute(
                    "UPDATE stream_tasks SET restart_count = restart_count + 1, last_error = ?, "
                    "resume_video_index = 0, resume_offset = ?, resume_loop = 1, "
                    "resume_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (error_msg, resume_offset, task_id),
                )
                conn.commit()

                time.sleep(5)
                if app is not None and not has_app_context():
                    with app.app_context():
                        _play_video(task_id, total_loops, resume_offset=resume_offset, app=app)
                else:
                    _play_video(task_id, total_loops, resume_offset=resume_offset, app=app)
            finally:
                with _reconnect_lock:
                    _reconnecting_tasks.discard(task_id)
            return

        # 不可重试或重试次数用尽
        cur.execute(
            "UPDATE stream_tasks SET status = 'failed', error_message = ?, restart_count = 0, "
            "resume_video_index = 0, resume_offset = 0, resume_loop = 1, "
            "resume_at = CURRENT_TIMESTAMP, ended_at = CURRENT_TIMESTAMP WHERE id = ?",
            (error_msg, task_id),
        )
        conn.commit()
    except Exception:
        # DB 不可用（应用关闭 / 测试 teardown 删库）→ daemon 监控线程不崩，静默退出
        return
    finally:
        conn.close()


# ── 页面路由 ────────────────────────────────────────────────────────────────

@bp.route("/")
def streaming_page():
    return render_template("streaming.html")


# ── API 路由 ────────────────────────────────────────────────────────────────

@bp.route("/api/videos")
def list_streamable_videos():
    """获取所有已打水印的视频"""
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT wv.id, wv.filename, wv.duration, wv.output_path, v.video_id "
        "FROM watermarked_videos wv JOIN videos v ON wv.original_video_id = v.id "
        "ORDER BY wv.created_at DESC"
    )
    rows = [dict(r) for r in cur.fetchall()]
    return jsonify(rows)


@bp.route("/api/video-sets")
def list_video_sets():
    """获取所有评测视频集"""
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT id, name, notes, video_ids, created_at FROM eval_video_sets ORDER BY created_at DESC"
    )
    result = []
    for row in cur.fetchall():
        d = dict(row)
        try:
            ids = json.loads(d.get("video_ids") or "[]")
        except Exception:
            ids = []
        d["video_count"] = len(ids)
        d.pop("video_ids", None)
        result.append(d)
    return jsonify(result)


@bp.route("/api/tasks", methods=["GET"])
def list_tasks():
    db = get_db()
    cur = db.cursor()
    # 先同步 running 状态：把已退出的假死任务修正为 failed 或可重试时自动恢复
    app = current_app._get_current_object()
    _sync_running_status(db, app=app)

    cur.execute(
        "SELECT id, name, source_type, source_id, stream_name, loop_count, status, "
        "total_duration, suggested_algorithms, error_message, pid, log_path, "
        "resume_video_index, resume_offset, resume_loop, resume_at, "
        "restart_count, max_restarts, last_error, "
        "current_video_index, current_loop, "
        "created_at, started_at, ended_at "
        "FROM stream_tasks ORDER BY created_at DESC"
    )
    rows = [dict(r) for r in cur.fetchall()]
    local_ips = _get_local_ips()
    now_ts = time.time()

    # 校验 running 状态的任务：进程若已消失则自动修正状态
    for r in rows:
        if r.get("status") == "running" and r.get("pid"):
            # 正在重连的任务交由 _delayed_play_video 线程处理（其 DB pid 仍为旧值），
            # 这里不能据旧 pid 把它误标 done，否则会破坏自动重连（单请求内必现竞态）。
            with _reconnect_lock:
                if r["id"] in _reconnecting_tasks:
                    continue
            if not _is_pid_alive(r["pid"]):
                cur.execute(
                    "UPDATE stream_tasks SET status = 'done', ended_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (r["id"],),
                )
                db.commit()
                r["status"] = "done"
                r["ended_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())

    for r in rows:
        if r.get("suggested_algorithms"):
            try:
                r["suggested_algorithms"] = json.loads(r["suggested_algorithms"])
            except Exception:
                r["suggested_algorithms"] = []
        r["rtsp_urls"] = [
            {"iface": entry["iface"], "url": f"rtsp://{entry['ip']}:{MEDIAMTX_PORT}/{r['stream_name']}"}
            for entry in local_ips
        ]

        # 运行时长和预计结束时间（单进程模式下 started_at 为任务开始时间）
        elapsed = None
        estimated_end_ts = None
        total_duration = r.get("total_duration") or 0
        if r.get("status") in ("running", "done", "stopped", "failed"):
            videos, _ = _resolve_watermarked_videos(r["source_type"], r["source_id"])
            video_list = [
                {"duration": (_ensure_duration(v["id"], v["output_path"]) or 0)}
                for v in videos
            ]
            ref_ts = None
            if r.get("status") in ("done", "stopped", "failed") and r.get("ended_at"):
                ended = _parse_started_at(r["ended_at"])
                if ended:
                    ref_ts = ended.timestamp()
            elapsed = _calc_elapsed_seconds(
                video_list,
                r.get("current_loop"),
                r.get("current_video_index"),
                r.get("started_at"),
                ref_ts=ref_ts,
                total_duration=total_duration,
            )
            elapsed = max(0, min(elapsed, total_duration))
            if r.get("status") == "running":
                remaining = max(0, total_duration - elapsed)
                estimated_end_ts = now_ts + remaining
        r["elapsed_seconds"] = round(elapsed, 1) if elapsed is not None else None
        r["estimated_end_ts"] = estimated_end_ts

    return jsonify(rows)


@bp.route("/api/tasks", methods=["POST"])
def create_task():
    data = request.get_json() or {}
    source_type = data.get("source_type", "").strip()
    source_id = data.get("source_id")
    stream_name = data.get("stream_name", "").strip()
    loop_count = int(data.get("loop_count") or 1)
    name = data.get("name", "").strip()

    if source_type not in ("single", "set"):
        return jsonify({"error": "来源类型无效"}), 400
    if not source_id:
        return jsonify({"error": "请选择视频或视频集"}), 400
    if not stream_name:
        return jsonify({"error": "流名称不能为空"}), 400
    if not all(c.isalnum() or c in "-_" for c in stream_name):
        return jsonify({"error": "流名称只能包含字母、数字、连字符和下划线"}), 400
    if loop_count < 1:
        loop_count = 1
    if loop_count > 100:
        loop_count = 100

    # 解析视频列表，检查是否都已打水印
    videos, err = _resolve_watermarked_videos(source_type, int(source_id))
    if err:
        return jsonify({"error": err}), 400

    # 补全时长
    total_duration = 0.0
    for v in videos:
        dur = _ensure_duration(v["id"], v["output_path"])
        if dur:
            total_duration += dur

    # 算法建议
    video_db_ids = [v["video_db_id"] for v in videos]
    config_path = current_app.config.get("ALERT_TYPES_CONFIG", "config/alert_types.json")
    suggested = _get_suggested_algorithms(video_db_ids, config_path)

    db = get_db()
    cur = db.cursor()
    cur.execute(
        "INSERT INTO stream_tasks (name, source_type, source_id, stream_name, loop_count, "
        "status, total_duration, suggested_algorithms) VALUES (?, ?, ?, ?, ?, 'created', ?, ?)",
        (
            name or f"推流-{stream_name}",
            source_type,
            int(source_id),
            stream_name,
            loop_count,
            total_duration * loop_count if total_duration else None,
            json.dumps(suggested, ensure_ascii=False),
        ),
    )
    db.commit()
    task_id = cur.lastrowid

    rtsp_url = f"rtsp://{_get_local_ip()}:{MEDIAMTX_PORT}/{stream_name}"

    return jsonify({
        "id": task_id,
        "rtsp_url": rtsp_url,
        "total_duration": total_duration * loop_count if total_duration else None,
        "suggested_algorithms": suggested,
    }), 201


def _start_task_internal(task_id: int, use_resume: bool, is_restart: bool = False) -> tuple[bool, dict]:
    """
    启动推流任务的内部逻辑。
    单进程推流：用 concat + -stream_loop 一次性推完所有视频和循环。
    返回 (success, result)：
      - success=True 时 result 包含 status, pid, rtsp_urls
      - success=False 时 result 包含 error, status_code
    """
    app = current_app._get_current_object()
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT id, source_type, source_id, stream_name, loop_count, status, "
        "resume_offset, resume_loop "
        "FROM stream_tasks WHERE id = ?",
        (task_id,),
    )
    task = cur.fetchone()
    if not task:
        return False, {"error": "任务不存在", "status_code": 404}
    if not is_restart and task["status"] == "running":
        return False, {"error": "任务已在运行中", "status_code": 400}
    if not is_restart and task["status"] not in ("created", "done", "failed", "stopped"):
        return False, {"error": f"任务状态 {task['status']} 不可启动", "status_code": 400}

    videos, err = _resolve_watermarked_videos(task["source_type"], task["source_id"])
    if err:
        return False, {"error": err, "status_code": 400}

    for v in videos:
        if not Path(v["output_path"]).exists():
            return False, {"error": f"视频文件不存在：{v['output_path']}", "status_code": 400}

    # 探测源编码是否需要转码兜底（concat 单进程下任一视频不兼容则整轮转码）
    needs_transcode = not all(_probe_codec_compatible(v["output_path"]) for v in videos)

    # 并发上限：转码任务按 2 倍占并发配额（STREAM_MAX_CONCURRENT，默认 2）
    max_concurrent = int(os.environ.get("STREAM_MAX_CONCURRENT", "2"))
    cur.execute(
        "SELECT COALESCE(SUM(CASE WHEN transcode=1 THEN 2 ELSE 1 END), 0) AS used "
        "FROM stream_tasks WHERE status='running'"
    )
    used = cur.fetchone()["used"]
    cost = 2 if needs_transcode else 1
    if used + cost > max_concurrent:
        return False, {"error": "超出并发推流上限，请先停止其他任务", "status_code": 409}

    total_loops = task["loop_count"] or 1
    resume_offset = 0.0

    if use_resume:
        # 单进程续播：从整轮输入的指定偏移开始推流
        resume_offset = float(task["resume_offset"] or 0)

    # 设置任务为 running，由 _play_video 启动单进程 ffmpeg（同时落 transcode 标记）
    cur.execute(
        "UPDATE stream_tasks SET status = 'running', pid = NULL, error_message = NULL, "
        "ended_at = NULL, transcode = ? WHERE id = ?",
        (1 if needs_transcode else 0, task_id),
    )
    db.commit()

    success, result = _play_video(task_id, total_loops, resume_offset=resume_offset, app=app)
    if not success:
        return False, {"error": result.get("error", "启动失败"), "status_code": 500}
    return True, result


@bp.route("/api/tasks/<int:task_id>/start", methods=["POST"])
def start_task(task_id):
    data = request.get_json() or {}
    use_resume = data.get("resume", False)

    success, result = _start_task_internal(task_id, use_resume)
    if not success:
        return jsonify({"error": result["error"], "detail": result.get("detail")}), result["status_code"]

    return jsonify({"status": result["status"], "pid": result.get("pid"), "rtsp_urls": result.get("rtsp_urls")})


@bp.route("/api/tasks/<int:task_id>/stop", methods=["POST"])
def stop_task(task_id):
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT id, status, pid, source_type, source_id, loop_count, started_at, "
        "current_video_index, current_loop "
        "FROM stream_tasks WHERE id = ?",
        (task_id,),
    )
    task = cur.fetchone()
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    if task["status"] != "running":
        return jsonify({"error": "任务未在运行中"}), 400

    # 使用当前播放位置作为续播点
    resume_index = task["current_video_index"] if task["current_video_index"] is not None else 0
    resume_loop = task["current_loop"] if task["current_loop"] is not None else 1
    resume_offset = 0.0

    with _stream_lock:
        entry = _stream_processes.pop(task_id, None)

    killed = False
    if entry:
        process, log_fp = entry
        try:
            process.terminate()
            killed = True
        except Exception:
            pass
        try:
            log_fp.close()
        except Exception:
            pass
    else:
        # 应用重启后内存字典为空，尝试通过 pid 直接终止
        pid = task["pid"]
        if pid and _is_pid_alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                killed = True
            except Exception:
                pass

    new_status = "stopped" if killed else "failed"
    cur.execute(
        "UPDATE stream_tasks SET status = ?, ended_at = CURRENT_TIMESTAMP, "
        "resume_video_index = ?, resume_offset = ?, resume_loop = ?, resume_at = CURRENT_TIMESTAMP "
        "WHERE id = ?",
        (new_status, resume_index, resume_offset, resume_loop, task_id),
    )
    db.commit()
    return jsonify({"status": new_status})


@bp.route("/api/tasks/<int:task_id>/logs", methods=["GET"])
def get_task_logs(task_id):
    """获取推流任务的 FFmpeg 日志内容"""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT log_path FROM stream_tasks WHERE id = ?", (task_id,))
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "任务不存在"}), 404

    log_path = row["log_path"]
    if not log_path or not Path(log_path).exists():
        return jsonify({"content": "", "lines": 0}), 200

    try:
        content = Path(log_path).read_text(encoding="utf-8", errors="replace")
        # 限制返回 100KB，避免前端卡死
        max_chars = 100 * 1024
        if len(content) > max_chars:
            content = "... (日志过长，仅显示最后部分) ...\n" + content[-max_chars:]
        lines = content.count("\n")
        return jsonify({"content": content, "lines": lines}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/tasks/<int:task_id>/progress", methods=["GET"])
def get_task_progress(task_id):
    """获取推流任务的播放进度（视频列表、当前播放到第几个、第几轮等）"""
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT id, name, source_type, source_id, stream_name, loop_count, status, "
        "total_duration, started_at, ended_at, current_video_index, current_loop FROM stream_tasks WHERE id = ?",
        (task_id,),
    )
    task = cur.fetchone()
    if not task:
        return jsonify({"error": "任务不存在"}), 404

    videos, err = _resolve_watermarked_videos(task["source_type"], task["source_id"])
    if err:
        return jsonify({"error": err}), 400

    video_list = []
    for v in videos:
        dur = _ensure_duration(v["id"], v["output_path"])
        video_list.append({
            "filename": v["wm_filename"],
            "video_id": v["video_id"],
            "duration": dur or 0,
        })

    loop_count = task["loop_count"] or 1
    total_duration = task["total_duration"] or 0
    status = task["status"]

    # 计算已播放秒数（单进程模式下 started_at 是任务开始时间）
    ref_ts = None
    if status in ("done", "stopped", "failed") and task["ended_at"]:
        ended = _parse_started_at(task["ended_at"])
        if ended:
            ref_ts = ended.timestamp()
    elapsed = _calc_elapsed_seconds(
        video_list,
        task["current_loop"],
        task["current_video_index"],
        task["started_at"],
        ref_ts=ref_ts,
        total_duration=total_duration,
    )
    elapsed = max(0, min(elapsed, total_duration))
    progress = _calc_progress(elapsed, video_list, loop_count)

    return jsonify({
        "task_id": task_id,
        "name": task["name"],
        "stream_name": task["stream_name"],
        "status": status,
        "loop_count": loop_count,
        "total_duration": total_duration,
        "elapsed_seconds": round(elapsed, 1),
        "videos": video_list,
        "progress": progress,
    })


@bp.route("/api/tasks/<int:task_id>", methods=["PATCH"])
def update_task(task_id):
    """编辑任务参数（仅非运行中状态可编辑）"""
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT id, source_type, source_id, stream_name, loop_count, status, name "
        "FROM stream_tasks WHERE id = ?",
        (task_id,),
    )
    task = cur.fetchone()
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    if task["status"] == "running":
        return jsonify({"error": "任务运行中，无法编辑"}), 400

    data = request.get_json() or {}
    source_type = data.get("source_type", "").strip()
    source_id = data.get("source_id")
    stream_name = data.get("stream_name", "").strip()
    loop_count = int(data.get("loop_count") or 1)
    name = data.get("name", "").strip()

    if source_type not in ("single", "set"):
        return jsonify({"error": "来源类型无效"}), 400
    if not source_id:
        return jsonify({"error": "请选择视频或视频集"}), 400
    if not stream_name:
        return jsonify({"error": "流名称不能为空"}), 400
    if not all(c.isalnum() or c in "-_" for c in stream_name):
        return jsonify({"error": "流名称只能包含字母、数字、连字符和下划线"}), 400
    if loop_count < 1:
        loop_count = 1
    if loop_count > 100:
        loop_count = 100

    # 解析视频列表，检查是否都已打水印
    videos, err = _resolve_watermarked_videos(source_type, int(source_id))
    if err:
        return jsonify({"error": err}), 400

    # 补全时长
    total_duration = 0.0
    for v in videos:
        dur = _ensure_duration(v["id"], v["output_path"])
        if dur:
            total_duration += dur

    # 算法建议
    video_db_ids = [v["video_db_id"] for v in videos]
    config_path = current_app.config.get("ALERT_TYPES_CONFIG", "config/alert_types.json")
    suggested = _get_suggested_algorithms(video_db_ids, config_path)

    # 清除旧的错误信息，重置状态为 created
    cur.execute(
        "UPDATE stream_tasks SET name = ?, source_type = ?, source_id = ?, "
        "stream_name = ?, loop_count = ?, total_duration = ?, "
        "suggested_algorithms = ?, status = 'created', error_message = NULL, "
        "ended_at = NULL WHERE id = ?",
        (
            name or f"推流-{stream_name}",
            source_type,
            int(source_id),
            stream_name,
            loop_count,
            total_duration * loop_count if total_duration else None,
            json.dumps(suggested, ensure_ascii=False),
            task_id,
        ),
    )
    db.commit()
    return jsonify({"id": task_id, "status": "created"})


@bp.route("/api/tasks/<int:task_id>", methods=["DELETE"])
def delete_task(task_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id, status FROM stream_tasks WHERE id = ?", (task_id,))
    task = cur.fetchone()
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    if task["status"] == "running":
        return jsonify({"error": "请先停止任务再删除"}), 400

    cur.execute("DELETE FROM stream_tasks WHERE id = ?", (task_id,))
    db.commit()

    # 删除任务时清理续播临时文件
    _cleanup_resume_file(task_id)

    return jsonify({"ok": True})


@bp.route("/api/preview", methods=["POST"])
def preview_task():
    """根据来源类型和 ID 返回预览信息（RTSP 地址、时长、算法建议）"""
    data = request.get_json() or {}
    source_type = data.get("source_type", "").strip()
    source_id = data.get("source_id")
    stream_name = data.get("stream_name", "").strip()
    loop_count = int(data.get("loop_count") or 1)

    if not source_type or not source_id:
        return jsonify({"error": "参数不完整"}), 400

    videos, err = _resolve_watermarked_videos(source_type, int(source_id))
    if err:
        return jsonify({"error": err}), 400

    total_duration = 0.0
    for v in videos:
        dur = _ensure_duration(v["id"], v["output_path"])
        if dur:
            total_duration += dur

    video_db_ids = [v["video_db_id"] for v in videos]
    config_path = current_app.config.get("ALERT_TYPES_CONFIG", "config/alert_types.json")
    suggested = _get_suggested_algorithms(video_db_ids, config_path)

    rtsp_urls = [
        {"iface": entry["iface"], "url": f"rtsp://{entry['ip']}:{MEDIAMTX_PORT}/{stream_name}"}
        for entry in _get_local_ips()
    ] if stream_name else []

    return jsonify({
        "rtsp_urls": rtsp_urls,
        "total_duration": total_duration * loop_count if total_duration else None,
        "suggested_algorithms": suggested,
        "video_count": len(videos),
    })
