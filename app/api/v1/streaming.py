"""/api/v1/streaming 资源族端点（推流任务 CRUD + start/stop + logs/progress/preview）。

原位重写 app/routes/streaming.py 的 11 个 JSON 端点为 /api/v1/streaming/*，统一信封 +
5 位错误码（FF=09 stream-tasks，见 docs/rest-api-error-codes.md）。

streaming 是高风险模块：start 用 subprocess.Popen 起 ffmpeg + _monitor_video_process
后台 daemon 线程；list_tasks 调 _sync_running_status 可能起 _delayed_play_video 重连
线程；模块级 _stream_processes/_stream_lock/_reconnecting_tasks/_reconnect_lock 状态。

故**原位重写 handler 但函数级复用旧逻辑**（不重写 _play_video/_build_ffmpeg_cmd/
_monitor_video_process/_sync_running_status 等高风险函数）：
- start 直接调旧 _start_task_internal(task_id, use_resume)（其内 _play_video 起子进程+
  监控线程，原样复用），按其返回的 status_code/error 映射到 5 位码 + 400→409 修正。
- stop 复用 _stream_processes/_stream_lock/_is_pid_alive/_cleanup_resume_file 原语
  （同步 terminate，不起线程/子进程，非高风险），原样保留 os.kill 兜底分支。
- 查询/CRUD 复用 _resolve_watermarked_videos/_ensure_duration/_get_suggested_algorithms/
  _calc_progress/_calc_elapsed_seconds/_parse_started_at/_get_local_ips/_sync_running_status。

语义修正（新端点专属，旧不动）：DELETE→204（对齐 alerts/videos/algorithms）；
运行态冲突 400→409（30900 start 已运行 / 30901 PATCH 运行中 / 30902 DELETE 运行中 /
30904 stop 非运行 / 30903 状态不可启动，5 位码 H 位对齐 http_status）。
旧端点保留并自动加弃用 header（deprecation.py 的 /streaming/api/ → /api/v1/streaming）。
"""
import json
import os
import signal
import time
from pathlib import Path

from flask import Blueprint, request, current_app

from app.database import get_db
from app.routes import streaming as _legacy
from .responses import ok, created, paginated, no_content, ApiError

bp = Blueprint("api_v1_streaming", __name__, url_prefix="/api/v1")

MEDIAMTX_PORT = _legacy.MEDIAMTX_PORT


def _parse_pagination():
    """?page & ?page_size，page≥1，page_size 1..100，默认 20。"""
    try:
        page = max(1, int(request.args.get("page", "1")))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(request.args.get("page_size", "20"))
    except (TypeError, ValueError):
        page_size = 20
    return page, max(1, min(page_size, 100))


def _slice_page(rows, page, page_size):
    total = len(rows)
    start = (page - 1) * page_size
    return rows[start:start + page_size], total


def _validate_task_fields(source_type, source_id, stream_name):
    """create/update/preview 共用的字段校验。返回 (source_id:int, stream_name) 或 raise。"""
    if source_type not in ("single", "set"):
        raise ApiError(10900, "来源类型无效", 400)
    if not source_id:
        raise ApiError(10901, "请选择视频或视频集", 400)
    if not stream_name:
        raise ApiError(10902, "流名称不能为空", 400)
    if not all(c.isalnum() or c in "-_" for c in stream_name):
        raise ApiError(10903, "流名称只能包含字母、数字、连字符和下划线", 400)
    return int(source_id), stream_name


def _resolve_or_raise(source_type, source_id):
    """解析打水印视频列表；失败 raise 10900。"""
    videos, err = _legacy._resolve_watermarked_videos(source_type, source_id)
    if err:
        raise ApiError(10900, err, 400)
    return videos


def _compute_total_duration(videos):
    """累加各视频时长（经 _ensure_duration，DB 已有时长则不调 ffprobe）。"""
    total = 0.0
    for v in videos:
        dur = _legacy._ensure_duration(v["id"], v["output_path"])
        if dur:
            total += dur
    return total


def _suggested_algorithms(videos):
    """根据视频 DB ID 汇总事件类型（算法建议）。"""
    video_db_ids = [v["video_db_id"] for v in videos]
    config_path = current_app.config.get("ALERT_TYPES_CONFIG", "config/alert_types.json")
    return _legacy._get_suggested_algorithms(video_db_ids, config_path)


# ── 辅助资源：可推流视频 / 视频集 ───────────────────────────────────────────────

@bp.route("/streaming/videos", methods=["GET"])
def list_streamable_videos():
    """所有已打水印视频（分页）。"""
    page, page_size = _parse_pagination()
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT wv.id, wv.filename, wv.duration, wv.output_path, v.video_id "
        "FROM watermarked_videos wv JOIN videos v ON wv.original_video_id = v.id "
        "ORDER BY wv.created_at DESC"
    )
    rows = [dict(r) for r in cur.fetchall()]
    page_rows, total = _slice_page(rows, page, page_size)
    return paginated(page_rows, total, page, page_size)


@bp.route("/streaming/video-sets", methods=["GET"])
def list_video_sets():
    """所有评测视频集（分页），用 video_count 替代 video_ids。"""
    page, page_size = _parse_pagination()
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
    page_rows, total = _slice_page(result, page, page_size)
    return paginated(page_rows, total, page, page_size)


# ── 推流任务 ────────────────────────────────────────────────────────────────────

@bp.route("/streaming/tasks", methods=["GET"])
def list_tasks():
    """推流任务列表（分页）。先 _sync_running_status 同步假死任务（可能起重连线程，
    复用不改），再每行算 rtsp_urls/elapsed_seconds/estimated_end_ts。对齐旧 list_tasks。"""
    page, page_size = _parse_pagination()
    db = get_db()
    cur = db.cursor()
    app = current_app._get_current_object()
    _legacy._sync_running_status(db, app=app)

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
    local_ips = _legacy._get_local_ips()
    now_ts = time.time()

    # running 任务：进程消失则改 done（正在重连的交由 _delayed_play_video 线程处理，
    # 其 DB pid 仍旧值，不能据旧 pid 误标 done，否则破坏自动重连）
    for r in rows:
        if r.get("status") == "running" and r.get("pid"):
            with _legacy._reconnect_lock:
                if r["id"] in _legacy._reconnecting_tasks:
                    continue
            if not _legacy._is_pid_alive(r["pid"]):
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
            {"iface": e["iface"], "url": f"rtsp://{e['ip']}:{MEDIAMTX_PORT}/{r['stream_name']}"}
            for e in local_ips
        ]
        elapsed = None
        estimated_end_ts = None
        total_duration = r.get("total_duration") or 0
        if r.get("status") in ("running", "done", "stopped", "failed"):
            videos, _ = _legacy._resolve_watermarked_videos(r["source_type"], r["source_id"])
            video_list = [
                {"duration": (_legacy._ensure_duration(v["id"], v["output_path"]) or 0)}
                for v in videos
            ]
            ref_ts = None
            if r.get("status") in ("done", "stopped", "failed") and r.get("ended_at"):
                ended = _legacy._parse_started_at(r["ended_at"])
                if ended:
                    ref_ts = ended.timestamp()
            elapsed = _legacy._calc_elapsed_seconds(
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

    page_rows, total = _slice_page(rows, page, page_size)
    return paginated(page_rows, total, page, page_size)


@bp.route("/streaming/tasks", methods=["POST"])
def create_task():
    """创建推流任务（不启动）。body: source_type/source_id/stream_name/loop_count?/name?"""
    data = request.get_json() or {}
    source_type = (data.get("source_type") or "").strip()
    source_id = data.get("source_id")
    stream_name = (data.get("stream_name") or "").strip()
    loop_count = int(data.get("loop_count") or 1)
    name = (data.get("name") or "").strip()

    source_id, stream_name = _validate_task_fields(source_type, source_id, stream_name)
    if loop_count < 1:
        loop_count = 1
    if loop_count > 100:
        loop_count = 100

    videos = _resolve_or_raise(source_type, source_id)
    total_duration = _compute_total_duration(videos)
    suggested = _suggested_algorithms(videos)

    db = get_db()
    cur = db.cursor()
    cur.execute(
        "INSERT INTO stream_tasks (name, source_type, source_id, stream_name, loop_count, "
        "status, total_duration, suggested_algorithms) VALUES (?, ?, ?, ?, ?, 'created', ?, ?)",
        (
            name or f"推流-{stream_name}",
            source_type,
            source_id,
            stream_name,
            loop_count,
            total_duration * loop_count if total_duration else None,
            json.dumps(suggested, ensure_ascii=False),
        ),
    )
    db.commit()
    task_id = cur.lastrowid

    rtsp_url = f"rtsp://{_legacy._get_local_ip()}:{MEDIAMTX_PORT}/{stream_name}"
    return created(
        {
            "id": task_id,
            "rtsp_url": rtsp_url,
            "total_duration": total_duration * loop_count if total_duration else None,
            "suggested_algorithms": suggested,
        },
        location=f"/api/v1/streaming/tasks/{task_id}",
    )


@bp.route("/streaming/tasks/<int:task_id>:start", methods=["POST"])
def start_task(task_id):
    """启动推流（起 ffmpeg 子进程 + 监控线程，由旧 _start_task_internal 处理，不重写）。
    body: {resume?: bool}。运行态冲突 400→409（30900/30903）。"""
    data = request.get_json() or {}
    use_resume = data.get("resume", False)

    success, result = _legacy._start_task_internal(task_id, use_resume)
    if not success:
        status_code = result.get("status_code", 500)
        error = result.get("error", "启动失败")
        if status_code == 404:
            raise ApiError(20900, error, 404)
        if status_code == 409:
            # 超出并发推流上限（_start_task_internal 探测+计数后返回 409）
            raise ApiError(30905, error, 409)
        if status_code == 400:
            # _start_task_internal 的 400 按消息分流到 409 修正或 400 参数码
            if error == "任务已在运行中":
                raise ApiError(30900, error, 409)
            if error.endswith("不可启动"):
                raise ApiError(30903, error, 409)
            if error.startswith("视频文件不存在"):
                raise ApiError(10905, error, 400)
            raise ApiError(10900, error, 400)
        raise ApiError(40900, error, 500)
    return ok({
        "status": result["status"],
        "pid": result.get("pid"),
        "rtsp_urls": result.get("rtsp_urls"),
    })


@bp.route("/streaming/tasks/<int:task_id>:stop", methods=["POST"])
def stop_task(task_id):
    """停止推流（同步 terminate，复用 _stream_processes/_is_pid_alive 原语，不起线程）。
    任务非 running→409（30904，旧 400）。"""
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT id, status, pid, source_type, source_id, loop_count, started_at, "
        "current_video_index, current_loop FROM stream_tasks WHERE id = ?",
        (task_id,),
    )
    task = cur.fetchone()
    if not task:
        raise ApiError(20900, "任务不存在", 404)
    if task["status"] != "running":
        raise ApiError(30904, "任务未在运行中", 409)

    # 用当前播放位置作为续播点
    resume_index = task["current_video_index"] if task["current_video_index"] is not None else 0
    resume_loop = task["current_loop"] if task["current_loop"] is not None else 1
    resume_offset = 0.0

    with _legacy._stream_lock:
        entry = _legacy._stream_processes.pop(task_id, None)

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
        if pid and _legacy._is_pid_alive(pid):
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
    return ok({"status": new_status})


@bp.route("/streaming/tasks/<int:task_id>/logs", methods=["GET"])
def get_task_logs(task_id):
    """获取推流任务的 FFmpeg 日志内容（限 100KB）。"""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT log_path FROM stream_tasks WHERE id = ?", (task_id,))
    row = cur.fetchone()
    if not row:
        raise ApiError(20900, "任务不存在", 404)

    log_path = row["log_path"]
    if not log_path or not Path(log_path).exists():
        return ok({"content": "", "lines": 0})

    try:
        content = Path(log_path).read_text(encoding="utf-8", errors="replace")
        max_chars = 100 * 1024
        if len(content) > max_chars:
            content = "... (日志过长，仅显示最后部分) ...\n" + content[-max_chars:]
        lines = content.count("\n")
        return ok({"content": content, "lines": lines})
    except Exception as e:
        raise ApiError(40901, str(e), 500)


@bp.route("/streaming/tasks/<int:task_id>/progress", methods=["GET"])
def get_task_progress(task_id):
    """获取播放进度（视频列表、当前第几个/第几轮等）。"""
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT id, name, source_type, source_id, stream_name, loop_count, status, "
        "total_duration, started_at, ended_at, current_video_index, current_loop "
        "FROM stream_tasks WHERE id = ?",
        (task_id,),
    )
    task = cur.fetchone()
    if not task:
        raise ApiError(20900, "任务不存在", 404)

    videos = _resolve_or_raise(task["source_type"], task["source_id"])
    video_list = []
    for v in videos:
        dur = _legacy._ensure_duration(v["id"], v["output_path"])
        video_list.append({
            "filename": v["wm_filename"],
            "video_id": v["video_id"],
            "duration": dur or 0,
        })

    loop_count = task["loop_count"] or 1
    total_duration = task["total_duration"] or 0
    status = task["status"]

    ref_ts = None
    if status in ("done", "stopped", "failed") and task["ended_at"]:
        ended = _legacy._parse_started_at(task["ended_at"])
        if ended:
            ref_ts = ended.timestamp()
    elapsed = _legacy._calc_elapsed_seconds(
        video_list,
        task["current_loop"],
        task["current_video_index"],
        task["started_at"],
        ref_ts=ref_ts,
        total_duration=total_duration,
    )
    elapsed = max(0, min(elapsed, total_duration))
    progress = _legacy._calc_progress(elapsed, video_list, loop_count)

    return ok({
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


@bp.route("/streaming/tasks/<int:task_id>", methods=["PATCH"])
def update_task(task_id):
    """编辑任务参数（仅非运行中可编辑；运行中→409）。body 同 create，全字段校验后
    重置为 created。对齐旧 update_task（非部分更新，全量重校验）。"""
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT id, source_type, source_id, stream_name, loop_count, status, name "
        "FROM stream_tasks WHERE id = ?",
        (task_id,),
    )
    task = cur.fetchone()
    if not task:
        raise ApiError(20900, "任务不存在", 404)
    if task["status"] == "running":
        raise ApiError(30901, "任务运行中，无法编辑", 409)

    data = request.get_json() or {}
    source_type = (data.get("source_type") or "").strip()
    source_id = data.get("source_id")
    stream_name = (data.get("stream_name") or "").strip()
    loop_count = int(data.get("loop_count") or 1)
    name = (data.get("name") or "").strip()

    source_id, stream_name = _validate_task_fields(source_type, source_id, stream_name)
    if loop_count < 1:
        loop_count = 1
    if loop_count > 100:
        loop_count = 100

    videos = _resolve_or_raise(source_type, source_id)
    total_duration = _compute_total_duration(videos)
    suggested = _suggested_algorithms(videos)

    cur.execute(
        "UPDATE stream_tasks SET name = ?, source_type = ?, source_id = ?, "
        "stream_name = ?, loop_count = ?, total_duration = ?, "
        "suggested_algorithms = ?, status = 'created', error_message = NULL, "
        "ended_at = NULL WHERE id = ?",
        (
            name or f"推流-{stream_name}",
            source_type,
            source_id,
            stream_name,
            loop_count,
            total_duration * loop_count if total_duration else None,
            json.dumps(suggested, ensure_ascii=False),
            task_id,
        ),
    )
    db.commit()
    return ok({"id": task_id, "status": "created"})


@bp.route("/streaming/tasks/<int:task_id>", methods=["DELETE"])
def delete_task(task_id):
    """删除任务（运行中拒绝→409）。删除时清理续播临时文件。"""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id, status FROM stream_tasks WHERE id = ?", (task_id,))
    task = cur.fetchone()
    if not task:
        raise ApiError(20900, "任务不存在", 404)
    if task["status"] == "running":
        raise ApiError(30902, "请先停止任务再删除", 409)

    cur.execute("DELETE FROM stream_tasks WHERE id = ?", (task_id,))
    db.commit()
    _legacy._cleanup_resume_file(task_id)
    return no_content()


@bp.route("/streaming/tasks:preview", methods=["POST"])
def preview_task():
    """预览未创建的任务信息（RTSP 地址/时长/算法建议/视频数）。body: source_type/source_id/
    stream_name?/loop_count?。参数不完整→10904。"""
    data = request.get_json() or {}
    source_type = (data.get("source_type") or "").strip()
    source_id = data.get("source_id")
    stream_name = (data.get("stream_name") or "").strip()
    loop_count = int(data.get("loop_count") or 1)

    if not source_type or not source_id:
        raise ApiError(10904, "参数不完整", 400)

    videos = _resolve_or_raise(source_type, int(source_id))
    total_duration = _compute_total_duration(videos)
    suggested = _suggested_algorithms(videos)

    rtsp_urls = [
        {"iface": e["iface"], "url": f"rtsp://{e['ip']}:{MEDIAMTX_PORT}/{stream_name}"}
        for e in _legacy._get_local_ips()
    ] if stream_name else []

    return ok({
        "rtsp_urls": rtsp_urls,
        "total_duration": total_duration * loop_count if total_duration else None,
        "suggested_algorithms": suggested,
        "video_count": len(videos),
    })
