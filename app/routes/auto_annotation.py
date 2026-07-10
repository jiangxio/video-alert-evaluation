"""自动化标注路由

视频抽帧 -> 多模态分析 -> 合并事件 -> 生成 Ground Truth JSON
支持后台执行、中断、进度展示、任务排队。
"""

import json
import os
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

from flask import Blueprint, request, jsonify, render_template, current_app

from app.database import get_db, DATABASE_PATH
from app.services.behavior_analysis_service import (
    analyze_frame,
    get_api_client,
    load_config as load_anno_config,
)
from app.event_types import get_event_types

bp = Blueprint("auto_annotation", __name__, url_prefix="/auto-annotation")

EVENT_TYPES = get_event_types()

# ── 内存任务状态 ────────────────────────────────────────────────────────────
_auto_anno_tasks = {}
_auto_anno_lock = threading.Lock()
_stop_requested = False
_current_task_id = None
_task_queue = []


# ── 工具函数 ────────────────────────────────────────────────────────────────

def _get_video_duration(video_path: str) -> float:
    """获取视频时长（秒）"""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=duration", "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            duration = result.stdout.strip()
            if duration:
                return float(duration)
    except Exception:
        pass
    return 0.0


def _extract_frames(video_path: str, frames_dir: Path, interval_sec: int, task_id: int) -> list[tuple[float, str]]:
    """用 FFmpeg 按间隔抽帧，返回 [(timestamp_sec, frame_path), ...]"""
    duration = _get_video_duration(video_path)
    if duration <= 0:
        return []

    frames_dir.mkdir(parents=True, exist_ok=True)
    frames = []

    for t in range(0, int(duration) + 1, interval_sec):
        out_path = frames_dir / f"frame_{t:06d}.jpg"
        try:
            subprocess.run(
                [
                    "ffmpeg", "-ss", str(t), "-i", str(video_path),
                    "-vframes", "1", "-q:v", "2", "-y",
                    "-loglevel", "error",
                    str(out_path),
                ],
                timeout=30, check=True,
            )
            if out_path.exists():
                frames.append((float(t), str(out_path)))
        except Exception:
            pass

        # 每抽10帧更新一次进度
        if len(frames) % 10 == 0:
            with _auto_anno_lock:
                if task_id in _auto_anno_tasks:
                    total = _auto_anno_tasks[task_id].get("total_frames", 1)
                    progress = min(30, int(len(frames) / total * 30)) if total > 0 else 0
                    _auto_anno_tasks[task_id]["phase_progress"] = progress

    return frames


def _merge_frame_results(
    frames: list[dict], merge_interval_sec: int, selected_types: list[str]
) -> list[dict]:
    """将逐帧检测结果按类型合并为事件区间

    frames: [{"timestamp_sec": float, "detected_event_types": [str, ...]}, ...]
    返回: [{"type": str, "start": float, "end": float}, ...]
    """
    type_timestamps = {}
    for f in frames:
        for etype in f.get("detected_event_types", []):
            if etype in selected_types and etype != "normal":
                type_timestamps.setdefault(etype, []).append(f["timestamp_sec"])

    events = []
    for etype, timestamps in type_timestamps.items():
        timestamps = sorted(set(timestamps))
        if not timestamps:
            continue
        start = timestamps[0]
        end = timestamps[0]
        for ts in timestamps[1:]:
            if ts - end <= merge_interval_sec:
                end = ts
            else:
                events.append({"type": etype, "start": start, "end": end})
                start = ts
                end = ts
        events.append({"type": etype, "start": start, "end": end})

    return sorted(events, key=lambda e: e["start"])


def _update_task_in_db(task_id: int, conn: sqlite3.Connection, **kwargs):
    """更新任务字段到数据库"""
    if not kwargs:
        return
    fields = ", ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values()) + [task_id]
    cursor = conn.cursor()
    cursor.execute(
        f"UPDATE auto_annotation_tasks SET {fields}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        values,
    )
    conn.commit()


# ── 后台工作函数 ────────────────────────────────────────────────────────────

def _do_auto_annotation(
    task_id: int,
    video_db_id: int,
    video_path: str,
    video_id_str: str,
    video_filename: str,
    frame_interval: int,
    merge_interval: int,
    selected_types: list[str],
    project_root: str,
    api_config: dict,
):
    """后台执行自动化标注"""
    global _current_task_id, _stop_requested

    conn = sqlite3.connect(str(DATABASE_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row

    try:
        # 确保内存状态中有该任务
        with _auto_anno_lock:
            if task_id not in _auto_anno_tasks:
                _auto_anno_tasks[task_id] = {
                    "task_id": task_id,
                    "video_db_id": video_db_id,
                    "video_id": video_id_str,
                    "status": "processing",
                    "phase": "queued",
                    "phase_progress": 0,
                    "analyzed_frames": 0,
                    "total_frames": 0,
                }

        # Phase: extracting
        _update_task_in_db(task_id, conn, current_phase="extracting", phase_progress=0)
        with _auto_anno_lock:
            _auto_anno_tasks[task_id]["phase"] = "extracting"
            _auto_anno_tasks[task_id]["phase_progress"] = 0

        frames_dir = Path(project_root) / "auto_annotation_frames" / str(task_id)
        frames = _extract_frames(video_path, frames_dir, frame_interval, task_id)
        total = len(frames)

        _update_task_in_db(task_id, conn, total_frames=total, analyzed_frames=0, phase_progress=30)
        with _auto_anno_lock:
            _auto_anno_tasks[task_id]["total_frames"] = total
            _auto_anno_tasks[task_id]["phase_progress"] = 30

        # Phase: analyzing
        _update_task_in_db(task_id, conn, current_phase="analyzing", phase_progress=30)
        with _auto_anno_lock:
            _auto_anno_tasks[task_id]["phase"] = "analyzing"
            _auto_anno_tasks[task_id]["phase_progress"] = 30

        client = get_api_client(api_config)
        model_name = api_config.get("model", "Qwen3-VL-8B-Instruct")

        request_interval = api_config.get("request_interval_sec", 1)
        max_retries = 3
        max_total_errors = 5
        total_errors = 0

        for i, (ts, frame_path) in enumerate(frames):
            # 检查中断请求
            with _auto_anno_lock:
                if _stop_requested and _current_task_id == task_id:
                    _update_task_in_db(task_id, conn, status="cancelled", current_phase="cancelled")
                    _auto_anno_tasks[task_id]["status"] = "cancelled"
                    _auto_anno_tasks[task_id]["phase"] = "cancelled"
                    return

            labels = ["normal"]
            frame_failed = False
            for attempt in range(max_retries):
                try:
                    labels = analyze_frame(client, model_name, frame_path, selected_types)
                    break
                except Exception as e:
                    err_str = str(e)
                    is_rate_limit = "429" in err_str or "频率" in err_str or "rate limit" in err_str.lower()
                    if is_rate_limit and attempt < max_retries - 1:
                        sleep_sec = (attempt + 1) * 2 + request_interval
                        print(f"[AutoAnnotation] 429 at {ts}s, retry in {sleep_sec}s...")
                        time.sleep(sleep_sec)
                    else:
                        print(f"[AutoAnnotation] Frame analysis error at {ts}s: {e}")
                        frame_failed = True
                        break

            if frame_failed:
                total_errors += 1
                if total_errors >= max_total_errors:
                    error_msg = f"连续 {max_total_errors} 帧分析失败，任务已停止"
                    _update_task_in_db(task_id, conn, status="failed", error_message=error_msg)
                    with _auto_anno_lock:
                        _auto_anno_tasks[task_id]["status"] = "failed"
                        _auto_anno_tasks[task_id]["error"] = error_msg
                    return

            # 帧间限流间隔
            if i < len(frames) - 1:
                time.sleep(request_interval)

            # 过滤掉 normal，只保留用户勾选的事件类型
            detected = [l for l in labels if l in selected_types and l != "normal"]

            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO auto_annotation_frames
                (task_id, timestamp_sec, frame_path, detected_event_types)
                VALUES (?, ?, ?, ?)
                """,
                (task_id, ts, frame_path, json.dumps(detected, ensure_ascii=False)),
            )
            conn.commit()

            # 每5帧更新进度
            if (i + 1) % 5 == 0 or i == total - 1:
                analyzed = i + 1
                progress = 30 + int(analyzed / total * 50) if total > 0 else 80
                _update_task_in_db(task_id, conn, analyzed_frames=analyzed, phase_progress=progress)
                with _auto_anno_lock:
                    _auto_anno_tasks[task_id]["analyzed_frames"] = analyzed
                    _auto_anno_tasks[task_id]["phase_progress"] = progress

        # Phase: merging
        _update_task_in_db(task_id, conn, current_phase="merging", phase_progress=80)
        with _auto_anno_lock:
            _auto_anno_tasks[task_id]["phase"] = "merging"
            _auto_anno_tasks[task_id]["phase_progress"] = 80

        cursor = conn.cursor()
        cursor.execute(
            "SELECT timestamp_sec, detected_event_types FROM auto_annotation_frames WHERE task_id = ? ORDER BY timestamp_sec",
            (task_id,),
        )
        frame_rows = [
            {
                "timestamp_sec": r["timestamp_sec"],
                "detected_event_types": json.loads(r["detected_event_types"] or "[]"),
            }
            for r in cursor.fetchall()
        ]
        events = _merge_frame_results(frame_rows, merge_interval, selected_types)

        # Phase: saving
        _update_task_in_db(task_id, conn, current_phase="saving", phase_progress=90)
        with _auto_anno_lock:
            _auto_anno_tasks[task_id]["phase"] = "saving"
            _auto_anno_tasks[task_id]["phase_progress"] = 90

        gt_data = {
            "file": video_filename,
            "id": video_id_str,
            "events": events,
        }
        gt_dir = Path(project_root) / "ground_truth"
        gt_dir.mkdir(parents=True, exist_ok=True)
        gt_path = gt_dir / f"{video_id_str}.json"
        with open(gt_path, "w", encoding="utf-8") as f:
            json.dump(gt_data, f, ensure_ascii=False, indent=2)

        _update_task_in_db(
            task_id, conn,
            status="done",
            current_phase="done",
            phase_progress=100,
            result_json_path=str(gt_path),
        )
        with _auto_anno_lock:
            _auto_anno_tasks[task_id]["status"] = "done"
            _auto_anno_tasks[task_id]["phase"] = "done"
            _auto_anno_tasks[task_id]["phase_progress"] = 100
            _auto_anno_tasks[task_id]["result_json_path"] = str(gt_path)

    except Exception as e:
        _update_task_in_db(task_id, conn, status="failed", error_message=str(e))
        with _auto_anno_lock:
            _auto_anno_tasks[task_id]["status"] = "failed"
            _auto_anno_tasks[task_id]["error"] = str(e)
    finally:
        conn.close()
        # 处理队列
        _process_queue(project_root)


def _process_queue(project_root: str):
    """检查队列并启动下一个任务"""
    global _current_task_id, _stop_requested

    with _auto_anno_lock:
        _current_task_id = None
        _stop_requested = False
        if not _task_queue:
            return
        next_task_id = _task_queue.pop(0)

    # 读取任务详情并启动
    conn = sqlite3.connect(str(DATABASE_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, video_db_id, video_id, status, frame_interval_sec, merge_interval_sec, event_types, total_frames, analyzed_frames, current_phase, phase_progress, result_json_path, error_message, created_at, updated_at FROM auto_annotation_tasks WHERE id = ?", (next_task_id,))
        task = cursor.fetchone()
        if not task:
            return

        cursor.execute("SELECT id, filename, original_path, video_id, file_size, duration, created_at, updated_at, video_id_confirmed FROM videos WHERE id = ?", (task["video_db_id"],))
        video = cursor.fetchone()
        if not video:
            _update_task_in_db(next_task_id, conn, status="failed", error_message="视频不存在")
            return

        cursor.execute(
            "SELECT id, original_video_id, filename, output_path, file_size, created_at, thumbnail_path, resolution, duration, ocr_check_status FROM watermarked_videos WHERE original_video_id = ? ORDER BY created_at DESC LIMIT 1",
            (task["video_db_id"],),
        )
        wm = cursor.fetchone()
        if not wm:
            _update_task_in_db(next_task_id, conn, status="failed", error_message="无水印视频")
            return

        _update_task_in_db(next_task_id, conn, status="processing", current_phase="queued")

        api_config = load_anno_config()

        with _auto_anno_lock:
            _current_task_id = next_task_id
            _auto_anno_tasks[next_task_id] = {
                "task_id": next_task_id,
                "video_db_id": task["video_db_id"],
                "video_id": task["video_id"],
                "status": "processing",
                "phase": "queued",
                "phase_progress": 0,
                "analyzed_frames": 0,
                "total_frames": task["total_frames"],
            }

        thread = threading.Thread(
            target=_do_auto_annotation,
            args=(
                next_task_id,
                task["video_db_id"],
                wm["output_path"],
                task["video_id"],
                video["filename"],
                task["frame_interval_sec"],
                task["merge_interval_sec"],
                json.loads(task["event_types"]),
                project_root,
                api_config,
            ),
            daemon=True,
        )
        thread.start()
    finally:
        conn.close()


# ── 页面路由 ────────────────────────────────────────────────────────────────

@bp.route("/")
def select_page():
    """视频选择页：展示无事件的视频"""
    return render_template("auto_annotation_select.html")


@bp.route("/config/<int:video_db_id>")
def config_page(video_db_id):
    """参数配置页"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id, filename, original_path, video_id, file_size, duration, created_at, updated_at, video_id_confirmed FROM videos WHERE id = ?", (video_db_id,))
    video = cursor.fetchone()
    if not video:
        return "视频不存在", 404

    cursor.execute(
        "SELECT id, original_video_id, filename, output_path, file_size, created_at, thumbnail_path, resolution, duration, ocr_check_status FROM watermarked_videos WHERE original_video_id = ? ORDER BY created_at DESC LIMIT 1",
        (video_db_id,),
    )
    wm = cursor.fetchone()
    if not wm:
        return "尚未生成水印视频", 400

    return render_template(
        "auto_annotation_config.html",
        video=dict(video),
        watermarked=dict(wm),
        event_types=EVENT_TYPES,
    )


# ── API 路由 ────────────────────────────────────────────────────────────────

@bp.route("/api/videos-without-events", methods=["GET"])
def list_videos_without_events():
    """查询无事件的视频列表"""
    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        SELECT v.*, wv.id as wm_id, wv.thumbnail_path, wv.duration
        FROM videos v
        LEFT JOIN watermarked_videos wv ON wv.original_video_id = v.id
        LEFT JOIN events e ON e.video_db_id = v.id
        WHERE wv.id IS NOT NULL
        GROUP BY v.id
        HAVING COUNT(e.id) = 0
        ORDER BY v.created_at DESC
    """)
    videos = []
    for row in cursor.fetchall():
        videos.append({
            "id": row["wm_id"],
            "video_db_id": row["id"],
            "filename": row["filename"],
            "video_id": row["video_id"],
            "duration": row["duration"],
            "thumbnail_path": row["thumbnail_path"],
        })
    return jsonify(videos)


@bp.route("/api/start", methods=["POST"])
def start_task():
    """启动自动化标注任务"""
    global _current_task_id, _stop_requested

    data = request.get_json() or {}
    video_db_id = data.get("video_db_id")
    frame_interval = data.get("frame_interval_sec", 1)
    merge_interval = data.get("merge_interval_sec", 5)
    selected_types = data.get("event_types", [])
    api_key = data.get("api_key", "")
    base_url = data.get("base_url", "")
    model = data.get("model", "")
    request_interval_sec = data.get("request_interval_sec")

    if not video_db_id:
        return jsonify({"error": "未选择视频"}), 400
    if frame_interval < 1:
        return jsonify({"error": "抽帧间隔至少为1秒"}), 400
    if merge_interval < 0:
        return jsonify({"error": "合并间隔不能为负数"}), 400
    if not selected_types:
        return jsonify({"error": "至少选择一个事件类型"}), 400

    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id, filename, original_path, video_id, file_size, duration, created_at, updated_at, video_id_confirmed FROM videos WHERE id = ?", (video_db_id,))
    video = cursor.fetchone()
    if not video:
        return jsonify({"error": "视频不存在"}), 404

    cursor.execute(
        "SELECT id, original_video_id, filename, output_path, file_size, created_at, thumbnail_path, resolution, duration, ocr_check_status FROM watermarked_videos WHERE original_video_id = ? ORDER BY created_at DESC LIMIT 1",
        (video_db_id,),
    )
    wm = cursor.fetchone()
    if not wm:
        return jsonify({"error": "尚未生成水印视频"}), 400

    # 保存 API 配置（如果提供了）
    api_config = load_anno_config()
    if api_key:
        api_config["api_key"] = api_key
    if base_url:
        api_config["base_url"] = base_url
    if model:
        api_config["model"] = model
    if request_interval_sec is not None:
        api_config["request_interval_sec"] = request_interval_sec
    if api_key or base_url or model or request_interval_sec is not None:
        from app.services.behavior_analysis_service import save_config
        save_config(api_config)

    # 创建任务记录
    cursor.execute(
        """
        INSERT INTO auto_annotation_tasks
        (video_db_id, video_id, status, frame_interval_sec, merge_interval_sec, event_types)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            video_db_id,
            video["video_id"],
            "queued",
            frame_interval,
            merge_interval,
            json.dumps(selected_types, ensure_ascii=False),
        ),
    )
    db.commit()
    task_id = cursor.lastrowid

    with _auto_anno_lock:
        _auto_anno_tasks[task_id] = {
            "task_id": task_id,
            "video_db_id": video_db_id,
            "video_id": video["video_id"],
            "status": "queued",
            "phase": "queued",
            "phase_progress": 0,
            "analyzed_frames": 0,
            "total_frames": 0,
        }

    # 判断是立即执行还是入队
    with _auto_anno_lock:
        if _current_task_id is not None:
            _task_queue.append(task_id)
            return jsonify({"success": True, "task_id": task_id, "queued": True})
        _current_task_id = task_id
        _stop_requested = False

    # 立即启动
    _update_task_in_db(task_id, db, status="processing")
    _auto_anno_tasks[task_id]["status"] = "processing"

    project_root = current_app.config["PROJECT_ROOT"]
    thread = threading.Thread(
        target=_do_auto_annotation,
        args=(
            task_id,
            video_db_id,
            wm["output_path"],
            video["video_id"],
            video["filename"],
            frame_interval,
            merge_interval,
            selected_types,
            project_root,
            api_config,
        ),
        daemon=True,
    )
    thread.start()

    return jsonify({"success": True, "task_id": task_id, "queued": False})


@bp.route("/api/stop", methods=["POST"])
def stop_task():
    """中断当前任务"""
    with _auto_anno_lock:
        if _current_task_id is None:
            return jsonify({"error": "当前没有运行中的任务"}), 400
        _stop_requested = True
        task_id = _current_task_id

    return jsonify({"success": True, "task_id": task_id})


@bp.route("/api/clear-intermediate", methods=["POST"])
def clear_intermediate():
    """清空中间数据（帧图片 + 帧记录）"""
    data = request.get_json() or {}
    task_id = data.get("task_id")
    if not task_id:
        return jsonify({"error": "缺少 task_id"}), 400

    project_root = current_app.config["PROJECT_ROOT"]
    frames_dir = Path(project_root) / "auto_annotation_frames" / str(task_id)
    if frames_dir.exists():
        for f in frames_dir.iterdir():
            f.unlink(missing_ok=True)
        try:
            frames_dir.rmdir()
        except Exception:
            pass

    db = get_db()
    cursor = db.cursor()
    cursor.execute("DELETE FROM auto_annotation_frames WHERE task_id = ?", (task_id,))
    db.commit()

    return jsonify({"success": True})


@bp.route("/api/status", methods=["GET"])
def get_status():
    """获取当前任务状态和排队信息"""
    with _auto_anno_lock:
        if _current_task_id is None:
            last_task = None
            if _auto_anno_tasks:
                latest_tid = max(_auto_anno_tasks.keys(), key=lambda k: _auto_anno_tasks[k].get("updated_at", 0))
                t = _auto_anno_tasks.get(latest_tid, {})
                last_task = {
                    "task_id": latest_tid,
                    "video_db_id": t.get("video_db_id"),
                    "video_id": t.get("video_id"),
                    "status": t.get("status"),
                    "phase": t.get("phase"),
                    "phase_progress": t.get("phase_progress", 0),
                    "analyzed_frames": t.get("analyzed_frames", 0),
                    "total_frames": t.get("total_frames", 0),
                }
            return jsonify({
                "has_running_task": False,
                "current_task": None,
                "last_task": last_task,
                "queue_count": len(_task_queue),
                "queue": [],
            })

        task = _auto_anno_tasks.get(_current_task_id, {})
        queue_info = []
        for tid in _task_queue:
            t = _auto_anno_tasks.get(tid, {})
            queue_info.append({
                "task_id": tid,
                "video_id": t.get("video_id", ""),
            })

        return jsonify({
            "has_running_task": True,
            "current_task": {
                "task_id": _current_task_id,
                "video_db_id": task.get("video_db_id"),
                "video_id": task.get("video_id"),
                "status": task.get("status"),
                "phase": task.get("phase"),
                "phase_progress": task.get("phase_progress", 0),
                "analyzed_frames": task.get("analyzed_frames", 0),
                "total_frames": task.get("total_frames", 0),
            },
            "queue_count": len(_task_queue),
            "queue": queue_info,
        })


@bp.route("/api/tasks", methods=["GET"])
def list_tasks():
    """历史任务列表"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        """
        SELECT t.*, v.filename as video_filename
        FROM auto_annotation_tasks t
        JOIN videos v ON v.id = t.video_db_id
        ORDER BY t.created_at DESC
        LIMIT 50
        """
    )
    tasks = [dict(r) for r in cursor.fetchall()]
    return jsonify(tasks)


@bp.route("/api/json/<int:task_id>", methods=["GET"])
def get_task_json(task_id):
    """读取任务生成的 JSON 文件内容"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id, video_db_id, video_id, status, frame_interval_sec, merge_interval_sec, event_types, total_frames, analyzed_frames, current_phase, phase_progress, result_json_path, error_message, created_at, updated_at FROM auto_annotation_tasks WHERE id = ?", (task_id,))
    task = cursor.fetchone()
    if not task:
        return jsonify({"error": "任务不存在"}), 404

    json_path = task["result_json_path"]
    if not json_path or not Path(json_path).exists():
        return jsonify({"error": "JSON 文件不存在"}), 404

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/tasks/video/<int:video_db_id>", methods=["GET"])
def list_tasks_by_video(video_db_id):
    """获取指定视频的所有已完成自动标注任务"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        """
        SELECT t.*, v.filename as video_filename
        FROM auto_annotation_tasks t
        JOIN videos v ON v.id = t.video_db_id
        WHERE t.video_db_id = ? AND t.status = 'done' AND t.result_json_path IS NOT NULL
        ORDER BY t.created_at DESC
        """,
        (video_db_id,),
    )
    tasks = [dict(r) for r in cursor.fetchall()]
    return jsonify(tasks)


@bp.route("/api/tasks/<int:task_id>", methods=["DELETE"])
def delete_task(task_id):
    """删除任务及中间数据"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id, video_db_id, video_id, status, frame_interval_sec, merge_interval_sec, event_types, total_frames, analyzed_frames, current_phase, phase_progress, result_json_path, error_message, created_at, updated_at FROM auto_annotation_tasks WHERE id = ?", (task_id,))
    task = cursor.fetchone()
    if not task:
        return jsonify({"error": "任务不存在"}), 404

    # 删除帧文件
    project_root = current_app.config["PROJECT_ROOT"]
    frames_dir = Path(project_root) / "auto_annotation_frames" / str(task_id)
    if frames_dir.exists():
        for f in frames_dir.iterdir():
            f.unlink(missing_ok=True)
        try:
            frames_dir.rmdir()
        except Exception:
            pass

    # 删除数据库记录
    cursor.execute("DELETE FROM auto_annotation_frames WHERE task_id = ?", (task_id,))
    cursor.execute("DELETE FROM auto_annotation_tasks WHERE id = ?", (task_id,))
    db.commit()

    return jsonify({"success": True})


@bp.route("/api/convert-to-events/<int:task_id>", methods=["POST"])
def convert_to_events(task_id):
    """将自动标注生成的 JSON 转成 DB events"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id, video_db_id, video_id, status, frame_interval_sec, merge_interval_sec, event_types, total_frames, analyzed_frames, current_phase, phase_progress, result_json_path, error_message, created_at, updated_at FROM auto_annotation_tasks WHERE id = ?", (task_id,))
    task = cursor.fetchone()
    if not task:
        return jsonify({"error": "任务不存在"}), 404

    if task["status"] != "done":
        return jsonify({"error": "任务尚未完成"}), 400

    if not task["result_json_path"] or not Path(task["result_json_path"]).exists():
        return jsonify({"error": "结果 JSON 不存在"}), 400

    with open(task["result_json_path"], "r", encoding="utf-8") as f:
        gt_data = json.load(f)

    events = gt_data.get("events", [])
    video_db_id = task["video_db_id"]

    # 批量插入所有事件，然后统一串行后台生成 GT 帧
    # 避免每个事件都启动独立线程导致并发 FFmpeg 卡死系统
    inserted_events = []
    for event in events:
        cursor.execute(
            """
            INSERT INTO events (video_db_id, event_type, start_seconds, end_seconds, gt_frames_status)
            VALUES (?, ?, ?, ?, 'pending')
            """,
            (video_db_id, event["type"], event["start"], event["end"]),
        )
        event_id = cursor.lastrowid
        db.commit()
        inserted_events.append((event_id, event["type"], event["start"], event["end"]))

    # 启动一个后台线程串行生成 GT 帧
    project_root = current_app.config["PROJECT_ROOT"]
    thread = threading.Thread(
        target=_batch_capture_gt_frames,
        args=(video_db_id, inserted_events, project_root),
        daemon=True,
    )
    thread.start()

    # 重新生成 JSON（保持同步）
    from app.routes.videos import generate_ground_truth_json
    generate_ground_truth_json(video_db_id)

    return jsonify({"success": True, "event_count": len(events)})


def _batch_capture_gt_frames(video_db_id, events, project_root):
    """串行生成多个事件的 GT 帧，避免并发 FFmpeg 导致系统卡死"""
    for event_id, event_type, start, end in events:
        try:
            _capture_gt_frames_async(video_db_id, event_id, event_type, start, end, project_root)
        except Exception as e:
            # 单个事件失败不影响后续事件
            import sqlite3
            conn = sqlite3.connect(str(DATABASE_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE events SET gt_frames_status = ? WHERE id = ?",
                ("failed", event_id),
            )
            conn.commit()
            conn.close()


# ── 导入现有 GT 帧捕获函数（避免循环导入）─────────────────────────────────────

from app.routes.videos import _capture_gt_frames_async
