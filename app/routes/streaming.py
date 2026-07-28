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
    """检查指定 PID 的进程是否仍在运行"""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _sync_running_status(db: sqlite3.Connection, app=None, allow_restart: bool = True):
    """
    扫描数据库中 status='running' 的任务，校验对应 pid 是否还活着。
    若进程已消失：
      - 错误可重试（broken pipe 等）且允许重连 → 触发自动重连
      - 否则 → 标记为 failed
    应在 list_tasks() 返回前、以及应用启动时调用。
    应用启动时 allow_restart=False，因为此时没有 app context，也不适合自动恢复。
    """
    cur = db.cursor()
    cur.execute(
        "SELECT id, pid, log_path, loop_count, current_video_index, current_loop, restart_count, max_restarts "
        "FROM stream_tasks WHERE status = 'running'"
    )
    dead_tasks = [dict(row) for row in cur.fetchall() if not _is_pid_alive(row["pid"])]
    for task in dead_tasks:
        task_id = task["id"]
        with _reconnect_lock:
            if task_id in _reconnecting_tasks:
                # 已经有监控线程在处理重连/切换，避免重复起 FFmpeg
                continue
            _reconnecting_tasks.add(task_id)

        pid = task["pid"]
        log_path = task.get("log_path")

        stderr_output = ""
        if log_path:
            try:
                stderr_output = Path(log_path).read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass

        retryable = _is_retryable_error(stderr_output)
        restart_count = int(task["restart_count"] or 0)
        max_restarts = int(task["max_restarts"] or 3)
        video_index = task["current_video_index"] if task["current_video_index"] is not None else 0
        current_loop = task["current_loop"] if task["current_loop"] is not None else 1
        total_loops = task["loop_count"] or 1
        error_msg = stderr_output[-500:] if stderr_output else f"FFmpeg 进程 (pid={pid}) 已退出"

        if retryable and restart_count < max_restarts and allow_restart and app is not None:
            cur.execute(
                "UPDATE stream_tasks SET restart_count = restart_count + 1, last_error = ?, "
                "resume_video_index = ?, resume_offset = ?, resume_loop = ?, resume_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (error_msg, video_index, 0.0, current_loop, task_id),
            )
            db.commit()
            threading.Thread(
                target=_delayed_play_video,
                args=(task_id, video_index, current_loop, total_loops, app),
                daemon=True,
            ).start()
        else:
            with _reconnect_lock:
                _reconnecting_tasks.discard(task_id)
            cur.execute(
                "UPDATE stream_tasks SET status = 'failed', error_message = ?, restart_count = 0, "
                "resume_video_index = ?, resume_offset = ?, resume_loop = ?, resume_at = CURRENT_TIMESTAMP, "
                "ended_at = CURRENT_TIMESTAMP WHERE id = ?",
                (error_msg, video_index, 0.0, current_loop, task_id),
            )
            db.commit()
            cur.execute(
                "UPDATE stream_tasks SET status = 'failed', error_message = ?, restart_count = 0, "
                "resume_video_index = ?, resume_offset = ?, resume_loop = ?, resume_at = CURRENT_TIMESTAMP, "
                "ended_at = CURRENT_TIMESTAMP WHERE id = ?",
                (error_msg, video_index, 0.0, current_loop, task_id),
            )
            db.commit()


def _delayed_play_video(task_id: int, video_index: int, current_loop: int, total_loops: int, app=None):
    """等待几秒后重试播放指定视频；函数退出时释放重连锁"""
    try:
        time.sleep(5)
        if app is not None and not has_app_context():
            with app.app_context():
                _play_video(task_id, video_index, current_loop, total_loops, app=app)
        else:
            _play_video(task_id, video_index, current_loop, total_loops, app=app)
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
    """获取所有真实网卡的 IPv4 地址（排除 loopback、docker、虚拟网卡）"""
    import fcntl
    import struct
    import array

    SIOCGIFCONF = 0x8912
    results = []
    try:
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
) -> float:
    """
    根据当前所在视频/轮次以及该视频的开始时间，计算任务总已播放秒数。
    逐视频推流模式下，started_at 记录的是“当前视频”的开始时间，因此需要把
    前面已经完整播放的视频/轮次累加进来。
    """
    if not video_list:
        return 0.0
    round_duration = sum(v.get("duration") or 0 for v in video_list)
    if round_duration <= 0:
        return 0.0

    loop = current_loop or 1
    idx = current_video_index or 0
    idx = max(0, min(idx, len(video_list) - 1))

    prior_seconds = (loop - 1) * round_duration + sum(
        (video_list[i].get("duration") or 0) for i in range(idx)
    )

    current_video_duration = video_list[idx].get("duration") or 0
    current_played = 0.0
    if video_started_at:
        started = _parse_started_at(video_started_at)
        if started:
            ref = ref_ts if ref_ts is not None else time.time()
            current_played = ref - started.timestamp()
            current_played = max(0.0, min(current_played, current_video_duration))

    return prior_seconds + current_played


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


def _build_ffmpeg_cmd(
    video_path: str,
    stream_name: str,
    resume_offset: float = 0,
    task_id: int | None = None,
) -> list[str]:
    """
    构建单个视频的 FFmpeg 推流命令。
    支持 resume_offset：从指定偏移处截取后推流。
    返回 FFmpeg 命令列表。
    """
    rtsp_url = f"rtsp://localhost:{MEDIAMTX_PORT}/{stream_name}"
    resume_offset = float(resume_offset or 0)

    input_path = video_path
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
                "ffmpeg", "-y", "-ss", str(resume_offset),
                "-i", video_path,
                "-c", "copy", "-avoid_negative_ts", "make_zero",
                str(resume_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"截取续播视频失败：{video_path} @ {resume_offset}s")
        input_path = str(resume_path)

    return [
        "ffmpeg", "-re", "-i", input_path,
        "-c", "copy", "-rtsp_transport", "tcp", "-f", "rtsp", rtsp_url,
    ]


def _get_resume_video_path(task_id: int | None) -> Path:
    """获取任务续播临时视频文件路径"""
    base = Path(__file__).resolve().parent.parent.parent / "tmp" / "stream_resume"
    return base / f"task_{task_id}.mp4"


def _cleanup_resume_file(task_id: int | None):
    """清理任务的续播临时视频文件"""
    if not task_id:
        return
    path = _get_resume_video_path(task_id)
    if path.exists():
        try:
            path.unlink()
        except Exception:
            pass


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


def _compute_resume_position(task_id: int, started_at) -> tuple[int | None, float | None, int | None]:
    """根据任务开始时间计算当前播放位置，用于续播"""
    if not started_at:
        return None, None, None
    started = _parse_started_at(started_at)
    if not started:
        return None, None, None

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
            return None, None, None

        elapsed = time.time() - started.timestamp()
        videos, err = _resolve_watermarked_videos(task["source_type"], task["source_id"])
        if err:
            return None, None, None

        video_list = []
        for v in videos:
            dur = _ensure_duration(v["id"], v["output_path"])
            video_list.append({"duration": dur or 0})

        progress = _calc_progress(elapsed, video_list, task["loop_count"] or 1)
        if progress["is_finished"]:
            return None, None, None
        return progress["current_video_index"], progress["current_video_offset"], progress["current_loop"]
    finally:
        conn.close()


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
    video_index: int,
    current_loop: int,
    total_loops: int,
    resume_offset: float = 0,
    app=None,
) -> tuple[bool, dict]:
    """
    播放队列中的指定视频。处理视频索引越界、循环结束、启动失败等情况。
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
            "SELECT id, source_type, source_id, stream_name, loop_count, status "
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

        # 处理视频索引越界：进入下一轮
        while video_index >= video_count:
            video_index -= video_count
            current_loop += 1

        # 超过总循环次数，任务完成
        if current_loop > total_loops:
            cur.execute(
                "UPDATE stream_tasks SET status = 'done', error_message = NULL, restart_count = 0, "
                "resume_video_index = NULL, resume_offset = NULL, resume_loop = NULL, resume_at = NULL, "
                "ended_at = CURRENT_TIMESTAMP WHERE id = ?",
                (task_id,),
            )
            db.commit()
            return True, {"status": "done"}

        video = videos[video_index]
        video_path = video["output_path"]
        if not Path(video_path).exists():
            err = f"视频文件不存在：{video_path}"
            _set_task_failed(task_id, err)
            return False, {"error": err}

        try:
            cmd = _build_ffmpeg_cmd(video_path, task["stream_name"], resume_offset=resume_offset, task_id=task_id)
        except RuntimeError as e:
            _set_task_failed(task_id, str(e))
            return False, {"error": str(e)}

        log_dir = Path(current_app.root_path).parent / "logs" / "stream"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"task_{task_id}.log"

        try:
            log_fp = open(log_path, "w", encoding="utf-8")
            process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=log_fp)
        except FileNotFoundError:
            log_fp.close()
            err = "ffmpeg 未安装或不在 PATH 中"
            _set_task_failed(task_id, err)
            return False, {"error": err}

        # 立即记录启动时间和位置（started_at 表示当前视频的实际开始时间）
        with _stream_lock:
            _stream_processes[task_id] = (process, log_fp)

        cur.execute(
            "UPDATE stream_tasks SET status = 'running', pid = ?, log_path = ?, "
            "current_video_index = ?, current_loop = ?, "
            "resume_video_index = NULL, resume_offset = NULL, resume_loop = NULL, resume_at = NULL, "
            "started_at = CURRENT_TIMESTAMP, error_message = NULL, ended_at = NULL WHERE id = ?",
            (process.pid, str(log_path), video_index, current_loop, task_id),
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
            args=(task_id, process, log_fp, log_path, video_index, current_loop, total_loops, app),
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
    video_index: int,
    current_loop: int,
    total_loops: int,
    app=None,
):
    """后台线程：单个视频推流结束后，决定播放下一个、重试当前视频还是失败"""
    try:
        process.wait()
    except Exception:
        pass
    returncode = process.returncode

    try:
        log_fp.close()
    except Exception:
        pass

    _cleanup_resume_file(task_id)

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
            "SELECT status, restart_count, max_restarts FROM stream_tasks WHERE id = ?",
            (task_id,),
        )
        row = cur.fetchone()
        if not row or row["status"] != "running":
            return

        def _play_with_ctx(**kwargs):
            if app is not None and not has_app_context():
                with app.app_context():
                    _play_video(task_id, app=app, **kwargs)
            else:
                _play_video(task_id, app=app, **kwargs)

        if returncode == 0:
            # 当前视频正常结束，播放下一个
            with _reconnect_lock:
                _reconnecting_tasks.add(task_id)
            try:
                _play_with_ctx(video_index=video_index + 1, current_loop=current_loop, total_loops=total_loops)
            finally:
                with _reconnect_lock:
                    _reconnecting_tasks.discard(task_id)
            return

        error_msg = stderr_output[-500:] if stderr_output else f"FFmpeg 退出码 {returncode}"
        retryable = _is_retryable_error(stderr_output)
        restart_count = int(row["restart_count"] or 0)
        max_restarts = int(row["max_restarts"] or 3)

        if retryable and restart_count < max_restarts:
            # 记录失败位置并从当前视频开头重试
            with _reconnect_lock:
                _reconnecting_tasks.add(task_id)
            try:
                cur.execute(
                    "UPDATE stream_tasks SET restart_count = restart_count + 1, last_error = ?, "
                    "resume_video_index = ?, resume_offset = ?, resume_loop = ?, resume_at = CURRENT_TIMESTAMP "
                    "WHERE id = ?",
                    (error_msg, video_index, 0.0, current_loop, task_id),
                )
                conn.commit()

                time.sleep(5)
                _play_with_ctx(video_index=video_index, current_loop=current_loop, total_loops=total_loops)
            finally:
                with _reconnect_lock:
                    _reconnecting_tasks.discard(task_id)
            return

        # 不可重试或重试次数用尽，保留失败位置
        cur.execute(
            "UPDATE stream_tasks SET status = 'failed', error_message = ?, restart_count = 0, "
            "resume_video_index = ?, resume_offset = ?, resume_loop = ?, resume_at = CURRENT_TIMESTAMP, "
            "ended_at = CURRENT_TIMESTAMP WHERE id = ?",
            (error_msg, video_index, 0.0, current_loop, task_id),
        )
        conn.commit()
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

        # 运行时长和预计结束时间（逐视频模式下用 current_video_index/current_loop 累加）
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
    逐个视频推流：根据 resume 数据确定起始视频，然后调用 _play_video。
    返回 (success, result)：
      - success=True 时 result 包含 status, pid, rtsp_urls
      - success=False 时 result 包含 error, status_code
    """
    app = current_app._get_current_object()
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT id, source_type, source_id, stream_name, loop_count, status, "
        "resume_video_index, resume_offset, resume_loop "
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

    total_loops = task["loop_count"] or 1
    video_index = 0
    current_loop = 1
    resume_offset = 0.0

    if use_resume:
        has_resume = (
            task["resume_video_index"] is not None
            and task["resume_loop"] is not None
        )
        if has_resume:
            video_index = int(task["resume_video_index"])
            current_loop = int(task["resume_loop"])
            resume_offset = float(task["resume_offset"] or 0)

    # 设置任务为 running，由 _play_video 管理具体视频
    cur.execute(
        "UPDATE stream_tasks SET status = 'running', pid = NULL, error_message = NULL, "
        "ended_at = NULL WHERE id = ?",
        (task_id,),
    )
    db.commit()

    success, result = _play_video(task_id, video_index, current_loop, total_loops, resume_offset=resume_offset, app=app)
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

    # 计算已播放秒数（逐视频模式下 started_at 是当前视频的开始时间）
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
