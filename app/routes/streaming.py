"""RTSP 推流路由

创建推流任务 → 启动 FFmpeg 推流到 MediaMTX → 监控状态
MediaMTX 需用户手动启动：tools/mediamtx（默认监听 :8554）
"""

import errno
import json
import os
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from flask import Blueprint, request, jsonify, render_template, current_app

from app.database import get_db, DATABASE_PATH

bp = Blueprint("streaming", __name__, url_prefix="/streaming")

# task_id -> Popen
_stream_processes = {}
_stream_lock = threading.Lock()

MEDIAMTX_PORT = 8554


def _is_process_alive(pid: int) -> bool:
    """检查指定 PID 的进程是否仍在运行"""
    try:
        os.kill(pid, 0)
    except OSError as e:
        if e.errno == errno.ESRCH:
            return False
    return True


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


# ── 工具函数 ────────────────────────────────────────────────────────────────

def _load_algorithm_names(config_path: str) -> dict:
    """从 alert_types.json 读取 event_type -> 中文名映射"""
    names = {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(maxsplit=1)
                if len(parts) == 2:
                    names[parts[1]] = parts[1]
    except Exception:
        pass
    return names


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
    videos: [{"wm_id": int, "path": str, "video_id": str, "filename": str}, ...]
    """
    conn = sqlite3.connect(str(DATABASE_PATH))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()

        if source_type == "single":
            cur.execute(
                "SELECT wv.id, wv.output_path, wv.duration, v.video_id, v.filename, v.id as video_db_id "
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
                f"SELECT wv.id, wv.output_path, wv.duration, v.video_id, v.filename, v.id as video_db_id "
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


def _build_ffmpeg_cmd(video_paths: list[str], stream_name: str, loop_count: int) -> tuple[list[str], str | None]:
    """
    构建 FFmpeg 推流命令。
    单视频单次：直接 -i；多视频或多次循环：concat playlist。
    返回 (cmd, playlist_path_or_None)
    """
    rtsp_url = f"rtsp://localhost:{MEDIAMTX_PORT}/{stream_name}"

    if len(video_paths) == 1 and loop_count == 1:
        cmd = [
            "ffmpeg", "-re", "-i", video_paths[0],
            "-c", "copy", "-f", "rtsp", rtsp_url,
        ]
        return cmd, None

    # 生成 concat playlist
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    )
    for _ in range(loop_count):
        for path in video_paths:
            tmp.write(f"file '{path}'\n")
    tmp.close()

    cmd = [
        "ffmpeg", "-re", "-f", "concat", "-safe", "0",
        "-i", tmp.name,
        "-c", "copy", "-f", "rtsp", rtsp_url,
    ]
    return cmd, tmp.name


def _monitor_process(task_id: int, process: subprocess.Popen, playlist_path: str | None):
    """后台线程：等待 FFmpeg 进程结束，更新任务状态"""
    process.wait()
    returncode = process.returncode

    if playlist_path and os.path.exists(playlist_path):
        try:
            os.unlink(playlist_path)
        except Exception:
            pass

    with _stream_lock:
        _stream_processes.pop(task_id, None)

    conn = sqlite3.connect(str(DATABASE_PATH))
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT status FROM stream_tasks WHERE id = ?", (task_id,)
        )
        row = cur.fetchone()
        if row and row[0] == "running":
            if returncode == 0:
                new_status = "done"
                error_msg = None
            else:
                stderr_output = ""
                try:
                    stderr_output = process.stderr.read() if process.stderr else ""
                except Exception:
                    pass
                new_status = "failed"
                error_msg = stderr_output[:500] if stderr_output else f"FFmpeg 退出码 {returncode}"
            cur.execute(
                "UPDATE stream_tasks SET status = ?, error_message = ?, ended_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_status, error_msg, task_id),
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
    cur.execute(
        "SELECT id, name, source_type, source_id, stream_name, loop_count, status, "
        "total_duration, suggested_algorithms, error_message, pid, created_at, started_at, ended_at "
        "FROM stream_tasks ORDER BY created_at DESC"
    )
    rows = [dict(r) for r in cur.fetchall()]
    local_ips = _get_local_ips()
    now_ts = time.time()

    # 校验 running 状态的任务：进程若已消失则自动修正状态
    for r in rows:
        if r.get("status") == "running" and r.get("pid"):
            if not _is_process_alive(r["pid"]):
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

        # 运行时长和预计结束时间（仅 running 状态有意义）
        elapsed = None
        estimated_end_ts = None
        if r.get("status") == "running" and r.get("started_at"):
            try:
                from datetime import datetime, timezone, timedelta
                import email.utils
                # Flask/sqlite3 PARSE_DECLTYPES 可能返回 RFC 2822 格式
                started_raw = r["started_at"]
                if isinstance(started_raw, str) and "," in started_raw:
                    # "Fri, 29 May 2026 09:00:04 GMT"
                    parsed = email.utils.parsedate_to_datetime(started_raw)
                    started = parsed.astimezone(timezone.utc)
                elif isinstance(started_raw, str):
                    # "2026-05-29 09:00:04"
                    started = datetime.strptime(started_raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                else:
                    # datetime 对象（PARSE_DECLTYPES 解析结果）
                    started = started_raw.replace(tzinfo=timezone.utc)
                elapsed = now_ts - started.timestamp()
                if r.get("total_duration"):
                    remaining = max(0, r["total_duration"] - elapsed)
                    estimated_end_ts = now_ts + remaining
            except Exception:
                pass
        r["elapsed_seconds"] = elapsed
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


@bp.route("/api/tasks/<int:task_id>/start", methods=["POST"])
def start_task(task_id):
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT id, source_type, source_id, stream_name, loop_count, status "
        "FROM stream_tasks WHERE id = ?",
        (task_id,),
    )
    task = cur.fetchone()
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    if task["status"] == "running":
        return jsonify({"error": "任务已在运行中"}), 400
    if task["status"] not in ("created", "done", "failed", "stopped"):
        return jsonify({"error": f"任务状态 {task['status']} 不可启动"}), 400

    videos, err = _resolve_watermarked_videos(task["source_type"], task["source_id"])
    if err:
        return jsonify({"error": err}), 400

    video_paths = [v["output_path"] for v in videos]
    for p in video_paths:
        if not Path(p).exists():
            return jsonify({"error": f"视频文件不存在：{p}"}), 400

    cmd, playlist_path = _build_ffmpeg_cmd(video_paths, task["stream_name"], task["loop_count"])

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        return jsonify({"error": "ffmpeg 未安装或不在 PATH 中"}), 500

    # 等待 2.5 秒判断进程是否存活
    time.sleep(2.5)
    returncode = process.poll()

    if returncode is not None:
        # 进程已退出，读取错误信息
        stderr_output = ""
        try:
            stderr_output = process.stderr.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        if playlist_path and os.path.exists(playlist_path):
            try:
                os.unlink(playlist_path)
            except Exception:
                pass
        error_msg = stderr_output[-500:] if stderr_output else f"FFmpeg 退出码 {returncode}"
        cur.execute(
            "UPDATE stream_tasks SET status = 'failed', error_message = ?, ended_at = CURRENT_TIMESTAMP WHERE id = ?",
            (error_msg, task_id),
        )
        db.commit()
        return jsonify({"error": "推流启动失败，请确认 MediaMTX 已运行", "detail": error_msg}), 500

    # 进程存活，记录 PID
    with _stream_lock:
        _stream_processes[task_id] = process

    cur.execute(
        "UPDATE stream_tasks SET status = 'running', pid = ?, started_at = CURRENT_TIMESTAMP, "
        "error_message = NULL, ended_at = NULL WHERE id = ?",
        (process.pid, task_id),
    )
    db.commit()

    # 后台监控进程结束
    threading.Thread(
        target=_monitor_process,
        args=(task_id, process, playlist_path),
        daemon=True,
    ).start()

    rtsp_urls = [
        {"iface": entry["iface"], "url": f"rtsp://{entry['ip']}:{MEDIAMTX_PORT}/{task['stream_name']}"}
        for entry in _get_local_ips()
    ]
    return jsonify({"status": "running", "pid": process.pid, "rtsp_urls": rtsp_urls})


@bp.route("/api/tasks/<int:task_id>/stop", methods=["POST"])
def stop_task(task_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id, status FROM stream_tasks WHERE id = ?", (task_id,))
    task = cur.fetchone()
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    if task["status"] != "running":
        return jsonify({"error": "任务未在运行中"}), 400

    with _stream_lock:
        process = _stream_processes.pop(task_id, None)

    if process:
        try:
            process.terminate()
        except Exception:
            pass

    cur.execute(
        "UPDATE stream_tasks SET status = 'stopped', ended_at = CURRENT_TIMESTAMP WHERE id = ?",
        (task_id,),
    )
    db.commit()
    return jsonify({"status": "stopped"})


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
