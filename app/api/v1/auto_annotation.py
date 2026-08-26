"""/api/v1/auto-annotation 资源族端点（自动标注任务 + 引擎状态）。

委托 app/routes/auto_annotation.py 的 4 个高风险端点（start/stop/status/
convert-to-events：起后台线程 / 操作模块级任务态 _auto_anno_lock/
_current_task_id/_task_queue/_stop_requested），只在新端点套统一信封 + 5 位
错误码（FF=10，见 docs/rest-api-error-codes.md）。旧视图在同一个 request
context 内运行，request.get_json / get_db / current_app 均可用，故 start 的
请求体由旧视图自读，新端点透传。

纯查询/CRUD（videos-without-events / tasks 列表 / by-video / get-json /
delete / clear）原位重写，复用 get_db。列表端点用 SQL 层 LIMIT/OFFSET +
COUNT(*) 真分页（不 fetchall 后切片）。

语义修正（新端点专属，旧不动，5 位码 H 位对齐 http_status）：
- 尚未生成水印视频 400→404（21022，对齐 videos 族 20121）
- 当前没有运行中的任务 400→409（31040，对齐 streaming 30904）
- 任务尚未完成 400→409（31041，状态冲突）
- 结果 JSON 不存在 400→404（21023，对齐同模块 get-json）
- DELETE→204（对齐 alerts/videos/streaming/algorithms）
- start 成功保 200（对齐 OCR ocr:batch「200 不改 202」先例，不改交互语义）

委托边界说明：worker（_do_auto_annotation 抽帧+模型分析+生成 GT、
_batch_capture_gt_frames 抓帧）是旧生产代码，本模块不改不重测；v1 只验信封/
状态码/错误码/委托真触发（task 落库、模块态更新）。auto-annotation 路由级
低危 bug（get_status 按 updated_at 取最近但任务 dict 无该字段、
frame_interval_sec 传字符串 cast 崩）由 bug-audit 另行修，不在本模块范围。
"""
import json
import threading
from pathlib import Path

from flask import Blueprint, request, current_app

from app.database import get_db
from app.routes import auto_annotation as _legacy
from .compat import call_old_view
from .responses import ok, paginated, no_content, ApiError

bp = Blueprint("api_v1_auto_annotation", __name__, url_prefix="/api/v1")


# ── 分页 / 错误映射 / 文件清理 辅助 ───────────────────────────────────────────

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


def _paginate(db, base_sql, order_sql, params, page, page_size, mapper=dict):
    """真分页：COUNT(*) 取 total，LIMIT/OFFSET 取当页，mapper 映射每行。
    base_sql 不含 ORDER BY / LIMIT（COUNT 子查询与 items 查询共用）。"""
    cur = db.cursor()
    cur.execute(f"SELECT COUNT(*) FROM ({base_sql}) _c", params)
    total = cur.fetchone()[0]
    offset = (page - 1) * page_size
    cur.execute(f"{base_sql} {order_sql} LIMIT ? OFFSET ?",
                (*params, page_size, offset))
    return paginated([mapper(r) for r in cur.fetchall()], total, page, page_size)


def _raise_msg(body, msg_to_code, fallback=(41080, 500, "操作失败")):
    """旧视图非 200：按 error 文案子串匹配 (code, http_status)，无匹配走 fallback。"""
    msg = (body.get("error") if isinstance(body, dict) else None) or fallback[2]
    for key, (code, http_status) in msg_to_code.items():
        if key in msg:
            raise ApiError(code, msg, http_status)
    raise ApiError(fallback[0], msg, fallback[1])


def _clear_frames_dir(task_id, project_root):
    """删除任务的帧图片目录（对齐旧 delete_task/clear-intermediate 的文件清理逻辑）。"""
    frames_dir = Path(project_root) / "auto_annotation_frames" / str(task_id)
    if frames_dir.exists():
        for f in frames_dir.iterdir():
            f.unlink(missing_ok=True)
        try:
            frames_dir.rmdir()
        except Exception:
            pass


# start 的 error 文案 → (5 位码, 新 http_status)
_START_MSG_CODE = {
    "未选择视频": (11000, 400),
    "抽帧间隔": (11001, 400),
    "合并间隔": (11002, 400),
    "至少选择一个事件类型": (11003, 400),
    "视频不存在": (21021, 404),
    "水印视频": (21022, 404),  # "尚未生成水印视频"
}

# convert_to_events 的 error 文案 → (5 位码, 新 http_status)
_CONVERT_MSG_CODE = {
    "任务不存在": (21020, 404),
    "任务尚未完成": (31041, 409),
    "JSON 不存在": (21023, 404),  # "结果 JSON 不存在"
}

# 任务列表查询的统一列（auto_annotation_tasks 全列 + video_filename）
_TASK_COLS = """
    t.id, t.video_db_id, t.video_id, t.status, t.frame_interval_sec,
    t.merge_interval_sec, t.event_types, t.total_frames, t.analyzed_frames,
    t.current_phase, t.phase_progress, t.result_json_path, t.error_message,
    t.created_at, t.updated_at, v.filename AS video_filename
"""


# ── 辅助资源：可标注视频 ───────────────────────────────────────────────────────

@bp.route("/auto-annotation/videos-without-events", methods=["GET"])
def list_videos_without_events():
    """有水印但无事件（events 表 0 行）的视频（分页）。对齐旧 list_videos_without_events
    字段：id=水印视频 id，video_db_id=视频主键。事件过滤=LEFT JOIN events + HAVING COUNT(e.id)=0。"""
    page, page_size = _parse_pagination()
    base = """
        SELECT v.id, v.filename, v.video_id, v.duration,
               wv.id AS wm_id, wv.thumbnail_path
        FROM videos v
        LEFT JOIN watermarked_videos wv ON wv.original_video_id = v.id
        LEFT JOIN events e ON e.video_db_id = v.id
        WHERE wv.id IS NOT NULL
        GROUP BY v.id
        HAVING COUNT(e.id) = 0
    """

    def _m(r):
        return {
            "id": r["wm_id"],
            "video_db_id": r["id"],
            "filename": r["filename"],
            "video_id": r["video_id"],
            "duration": r["duration"],
            "thumbnail_path": r["thumbnail_path"],
        }

    return _paginate(get_db(), base, "ORDER BY v.created_at DESC",
                     (), page, page_size, _m)


# ── 自动标注任务 ───────────────────────────────────────────────────────────────

@bp.route("/auto-annotation/tasks", methods=["POST"])
def create_task():
    """创建并启动自动标注任务（委托旧 start_task：校验→建库→排队/起线程）。
    请求体 {video_db_id,frame_interval_sec?,merge_interval_sec?,event_types,
    api_key?,base_url?,model?,request_interval_sec?} 由旧视图自读。成功 200。"""
    body, status = call_old_view(_legacy.start_task)
    if status == 200:
        return ok({"task_id": body.get("task_id"), "queued": body.get("queued")})
    _raise_msg(body, _START_MSG_CODE)


@bp.route("/auto-annotation/tasks", methods=["GET"])
def list_tasks():
    """历史任务列表（分页，对齐旧 list_tasks 字段）。"""
    page, page_size = _parse_pagination()
    base = f"SELECT {_TASK_COLS} FROM auto_annotation_tasks t JOIN videos v ON v.id = t.video_db_id"
    return _paginate(get_db(), base, "ORDER BY t.created_at DESC",
                     (), page, page_size, dict)


@bp.route("/auto-annotation/tasks/<int:task_id>/json", methods=["GET"])
def get_task_json(task_id):
    """读取任务生成的 Ground Truth JSON 内容（套信封，data=GT 内容）。
    ?version=<version_no> 取指定历史版本快照（阶段3 版本化）。
    任务不存在→404(21020)；JSON 文件不存在→404(21023)；版本不存在→404(21024)。"""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT result_json_path, video_id FROM auto_annotation_tasks WHERE id = ?", (task_id,))
    row = cur.fetchone()
    if not row:
        raise ApiError(21020, "任务不存在", 404)

    version = request.args.get("version")
    if version:
        try:
            vno = int(version)
        except (TypeError, ValueError):
            raise ApiError(11004, "无效版本号", 400)
        cur.execute(
            "SELECT path FROM gt_versions WHERE video_id = ? AND version_no = ?",
            (row["video_id"], vno),
        )
        v = cur.fetchone()
        if not v:
            raise ApiError(21024, "版本不存在", 404)
        snap = Path(v["path"])
        if not snap.exists():
            raise ApiError(21023, "版本快照文件不存在", 404)
        try:
            return ok(json.loads(snap.read_text(encoding="utf-8")))
        except Exception as e:
            raise ApiError(41080, str(e), 500)

    json_path = row["result_json_path"]
    if not json_path or not Path(json_path).exists():
        raise ApiError(21023, "JSON 文件不存在", 404)
    try:
        data = json.loads(Path(json_path).read_text(encoding="utf-8"))
        return ok(data)
    except Exception as e:
        raise ApiError(41080, str(e), 500)


@bp.route("/auto-annotation/videos/<int:video_db_id>/tasks", methods=["GET"])
def list_tasks_by_video(video_db_id):
    """指定视频的已完成（done + 有结果 JSON 路径）自动标注任务（分页）。"""
    page, page_size = _parse_pagination()
    base = (
        f"SELECT {_TASK_COLS} FROM auto_annotation_tasks t "
        "JOIN videos v ON v.id = t.video_db_id "
        "WHERE t.video_db_id = ? AND t.status = 'done' AND t.result_json_path IS NOT NULL"
    )
    return _paginate(get_db(), base, "ORDER BY t.created_at DESC",
                     (video_db_id,), page, page_size, dict)


@bp.route("/auto-annotation/tasks/<int:task_id>", methods=["DELETE"])
def delete_task(task_id):
    """删除任务及中间帧数据。不存在→404(21020)；成功→204。"""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM auto_annotation_tasks WHERE id = ?", (task_id,))
    if not cur.fetchone():
        raise ApiError(21020, "任务不存在", 404)
    _clear_frames_dir(task_id, current_app.config["PROJECT_ROOT"])
    cur.execute("DELETE FROM auto_annotation_frames WHERE task_id = ?", (task_id,))
    cur.execute("DELETE FROM auto_annotation_tasks WHERE id = ?", (task_id,))
    db.commit()
    return no_content()


@bp.route("/auto-annotation/tasks/<int:task_id>:clear", methods=["POST"])
def clear_intermediate(task_id):
    """清空任务中间数据（帧图片 + 帧记录）。task_id 来自路径恒存在，旧「缺少 task_id」
    不可达。幂等：目录不存在也不报错（对齐旧 clear-intermediate 不校验任务存在性）。"""
    _clear_frames_dir(task_id, current_app.config["PROJECT_ROOT"])
    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM auto_annotation_frames WHERE task_id = ?", (task_id,))
    db.commit()
    return ok({"task_id": task_id})


# ── 引擎状态 / 控制（委托：模块级任务态） ───────────────────────────────────────

@bp.route("/auto-annotation/tasks:stop", methods=["POST"])
def stop_task():
    """中断当前运行中任务（委托旧 stop_task：置 _stop_requested）。
    无运行任务→409(31040，旧 400)。成功 200。"""
    body, status = call_old_view(_legacy.stop_task)
    if status == 200:
        return ok({"task_id": body.get("task_id")})
    _raise_msg(body, {"没有运行中的任务": (31040, 409)})


@bp.route("/auto-annotation/status", methods=["GET"])
def get_status():
    """获取当前任务状态和排队信息（委托旧 get_status，旧恒 200）。"""
    body, _ = call_old_view(_legacy.get_status)
    return ok(body)


@bp.route("/auto-annotation/tasks/<int:task_id>:convert-to-events", methods=["POST"])
def convert_to_events(task_id):
    """将自动标注 JSON 转成 DB events（委托旧 convert_to_events：插 events + 起
    _batch_capture_gt_frames 线程 + 调 generate_ground_truth_json）。成功 200。
    任务不存在→404(21020)；任务尚未完成→409(31041)；结果 JSON 不存在→404(21023)。"""
    body, status = call_old_view(_legacy.convert_to_events, task_id)
    if status == 200:
        return ok({"event_count": body.get("event_count")})
    _raise_msg(body, _CONVERT_MSG_CODE)


# ── 复核（阶段2：置信度分流后的 pending 事件人工复核）──────────────────────────

@bp.route("/auto-annotation/tasks/<int:task_id>/pending-events", methods=["GET"])
def list_pending_events(task_id):
    """列待复核事件（review_status='pending'，分页）。任务不存在→404(21020)。"""
    page, page_size = _parse_pagination()
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM auto_annotation_tasks WHERE id = ?", (task_id,))
    if not cur.fetchone():
        raise ApiError(21020, "任务不存在", 404)
    base = (
        "SELECT id, task_id, video_db_id, event_type, start_sec, end_sec, "
        "confidence, review_status FROM auto_annotation_events "
        "WHERE task_id = ? AND review_status = 'pending'"
    )
    return _paginate(db, base, "ORDER BY start_sec", (task_id,), page, page_size, dict)


def _fetch_reviewable_event(db, event_id):
    """取事件并校验可复核（pending/auto_approved）。返回 row 或 raise。"""
    cur = db.cursor()
    cur.execute(
        "SELECT id, task_id, video_db_id, event_type, start_sec, end_sec, "
        "confidence, review_status FROM auto_annotation_events WHERE id = ?",
        (event_id,),
    )
    ev = cur.fetchone()
    if not ev:
        raise ApiError(21024, "事件不存在", 404)
    if ev["review_status"] not in ("pending", "auto_approved"):
        raise ApiError(31042, "事件非待复核状态", 409)
    return ev


@bp.route("/auto-annotation/events/<int:event_id>:review", methods=["POST"])
def review_event(event_id):
    """复核单个事件。body: {action: 'approve'|'reject'|'edit', type?, start?, end?}。
    事件不存在→404(21024)；非待复核状态→409(31042)；无效 action/参数→400(11004)。
    approve（可带 type/start/end 编辑）：写 DB events + 起 _batch_capture_gt_frames。
    edit：仅改字段不改状态（仍 pending）。reject：标记 rejected 不入库。"""
    db = get_db()
    ev = _fetch_reviewable_event(db, event_id)
    data = request.get_json() or {}
    action = (data.get("action") or "").strip()
    if action not in ("approve", "reject", "edit"):
        raise ApiError(11004, "无效复核操作", 400)

    # 解析可选编辑字段
    new_type = ev["event_type"]
    new_start = ev["start_sec"]
    new_end = ev["end_sec"]
    if data.get("type") is not None:
        new_type = str(data["type"]).strip() or ev["event_type"]
    if data.get("start") is not None:
        try:
            new_start = float(data["start"])
        except (TypeError, ValueError):
            raise ApiError(11004, "无效的事件起始时间", 400)
    if data.get("end") is not None:
        try:
            new_end = float(data["end"])
        except (TypeError, ValueError):
            raise ApiError(11004, "无效的事件结束时间", 400)

    cur = db.cursor()

    if action == "reject":
        cur.execute(
            "UPDATE auto_annotation_events SET review_status='rejected', "
            "reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
            (event_id,),
        )
        db.commit()
        return ok({"event_id": event_id, "review_status": "rejected"})

    if action == "edit":
        # 仅修改字段，状态保持 pending（不写 DB events、不起帧捕获）
        cur.execute(
            "UPDATE auto_annotation_events SET event_type=?, start_sec=?, end_sec=? WHERE id=?",
            (new_type, new_start, new_end, event_id),
        )
        db.commit()
        return ok({"event_id": event_id, "review_status": ev["review_status"]})

    # approve（可带编辑）：写 DB events + 起 GT 帧捕获
    cur.execute(
        "INSERT INTO events (video_db_id, event_type, start_seconds, end_seconds, gt_frames_status) "
        "VALUES (?, ?, ?, ?, 'pending')",
        (ev["video_db_id"], new_type, new_start, new_end),
    )
    db_event_id = cur.lastrowid
    cur.execute(
        "UPDATE auto_annotation_events SET review_status='approved', reviewed_at=CURRENT_TIMESTAMP, "
        "event_type=?, start_sec=?, end_sec=? WHERE id=?",
        (new_type, new_start, new_end, event_id),
    )
    db.commit()

    # 后台串行生成 GT 帧（复用旧 _batch_capture_gt_frames，处理单事件列表）
    project_root = current_app.config["PROJECT_ROOT"]
    threading.Thread(
        target=_legacy._batch_capture_gt_frames,
        args=(ev["video_db_id"], [(db_event_id, new_type, new_start, new_end)], project_root),
        daemon=True,
    ).start()

    return ok({"event_id": event_id, "review_status": "approved", "db_event_id": db_event_id})


@bp.route("/auto-annotation/tasks/<int:task_id>:batch-approve", methods=["POST"])
def batch_approve(task_id):
    """批量通过待复核事件。body: {event_ids?: [int, ...]}（不传则通过该任务全部 pending）。
    approve 的事件写 DB events + 起 _batch_capture_gt_frames。任务不存在→404(21020)。"""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id, video_db_id FROM auto_annotation_tasks WHERE id = ?", (task_id,))
    task = cur.fetchone()
    if not task:
        raise ApiError(21020, "任务不存在", 404)

    data = request.get_json() or {}
    event_ids = data.get("event_ids")
    if event_ids:
        ph = ",".join("?" for _ in event_ids)
        cur.execute(
            f"SELECT id, video_db_id, event_type, start_sec, end_sec, confidence "
            f"FROM auto_annotation_events WHERE task_id=? AND id IN ({ph}) "
            f"AND review_status='pending'",
            (task_id, *event_ids),
        )
    else:
        cur.execute(
            "SELECT id, video_db_id, event_type, start_sec, end_sec, confidence "
            "FROM auto_annotation_events WHERE task_id=? AND review_status='pending'",
            (task_id,),
        )
    rows = [dict(r) for r in cur.fetchall()]

    inserted = []
    for r in rows:
        cur.execute(
            "INSERT INTO events (video_db_id, event_type, start_seconds, end_seconds, gt_frames_status) "
            "VALUES (?, ?, ?, ?, 'pending')",
            (r["video_db_id"], r["event_type"], r["start_sec"], r["end_sec"]),
        )
        inserted.append((cur.lastrowid, r["event_type"], r["start_sec"], r["end_sec"]))
        cur.execute(
            "UPDATE auto_annotation_events SET review_status='approved', reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
            (r["id"],),
        )
    db.commit()

    # 串行生成所有已通过事件的 GT 帧（复用旧 _batch_capture_gt_frames）
    if inserted:
        project_root = current_app.config["PROJECT_ROOT"]
        threading.Thread(
            target=_legacy._batch_capture_gt_frames,
            args=(rows[0]["video_db_id"], inserted, project_root),
            daemon=True,
        ).start()

    return ok({"task_id": task_id, "approved_count": len(rows)})


# ── 质量评估（阶段3：只读，不碰评测指标算法）──────────────────────────────────

@bp.route("/auto-annotation/tasks/<int:task_id>/quality", methods=["GET"])
def get_task_quality(task_id):
    """标注质量评估。任务不存在→404(21020)。

    返回：置信度分布（均值/中位数/极值/分箱，来自 auto_annotation_frames.confidence）、
    覆盖率（有检测帧/总抽帧数）、复核拒绝率（rejected/(approved+rejected)）。
    若本任务视频被某个已 final 评测任务引用，best-effort 只读读其 P/R/FP-per-hour
    （读 eval_tasks 存储值，不改 compute_task_metrics）。"""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id, video_db_id FROM auto_annotation_tasks WHERE id = ?", (task_id,))
    task = cur.fetchone()
    if not task:
        raise ApiError(21020, "任务不存在", 404)

    # 置信度分布（auto_annotation_frames）
    cur.execute("SELECT confidence FROM auto_annotation_frames WHERE task_id = ?", (task_id,))
    confs = [r["confidence"] for r in cur.fetchall() if r["confidence"] is not None]
    total_frames = len(confs)
    detected_frames = sum(1 for c in confs if c and c > 0)
    conf_mean = round(sum(confs) / total_frames, 4) if total_frames else 0
    conf_median = round(sorted(confs)[total_frames // 2], 4) if total_frames else 0
    conf_min = round(min(confs), 4) if total_frames else 0
    conf_max = round(max(confs), 4) if total_frames else 0
    bin_keys = ["0-0.2", "0.2-0.4", "0.4-0.6", "0.6-0.8", "0.8-1.0"]
    bins = {k: 0 for k in bin_keys}
    for c in confs:
        idx = min(int((c or 0) // 0.2), 4)
        bins[bin_keys[idx]] += 1

    # 复核状态（auto_annotation_events）
    cur.execute("SELECT review_status FROM auto_annotation_events WHERE task_id = ?", (task_id,))
    statuses = [r["review_status"] for r in cur.fetchall()]
    approved = sum(1 for s in statuses if s in ("auto_approved", "approved"))
    rejected = sum(1 for s in statuses if s == "rejected")
    pending = sum(1 for s in statuses if s == "pending")
    rejection_rate = round(rejected / (approved + rejected), 4) if (approved + rejected) else 0

    # 下游评测验证（只读 best-effort：找 finalized 评测任务其 eval_video_set 含本视频）
    downstream = None
    vdb = task["video_db_id"]
    cur.execute(
        "SELECT id, eval_set_id, accuracy, recall, avg_fp_per_hour FROM eval_tasks WHERE finalized = 1"
    )
    for et in cur.fetchall():
        cur.execute("SELECT video_ids FROM eval_video_sets WHERE id = ?", (et["eval_set_id"],))
        vs = cur.fetchone()
        if not vs:
            continue
        try:
            vids = json.loads(vs["video_ids"] or "[]")
        except Exception:
            vids = []
        if vdb in vids:
            downstream = {
                "eval_task_id": et["id"],
                "accuracy": et["accuracy"],
                "recall": et["recall"],
                "avg_fp_per_hour": et["avg_fp_per_hour"],
            }
            break

    return ok({
        "task_id": task_id,
        "confidence": {
            "mean": conf_mean, "median": conf_median,
            "min": conf_min, "max": conf_max,
            "bins": bins, "count": total_frames,
        },
        "coverage_rate": round(detected_frames / total_frames, 4) if total_frames else 0,
        "review": {
            "approved": approved, "rejected": rejected,
            "pending": pending, "rejection_rate": rejection_rate,
        },
        "downstream_eval": downstream,
    })


# ── GT 版本管理（阶段3：历史快照回溯）──────────────────────────────────────────

@bp.route("/auto-annotation/videos/<video_id>/gt-versions", methods=["GET"])
def list_gt_versions(video_id):
    """某视频的 GT 版本列表（按 version_no 倒序分页）。video_id 为字符串（如 046-001）。"""
    page, page_size = _parse_pagination()
    base = ("SELECT id, video_id, task_id, version_no, parent_version_no, "
            "review_status, created_at FROM gt_versions WHERE video_id = ?")
    return _paginate(get_db(), base, "ORDER BY version_no DESC",
                      (video_id,), page, page_size, dict)


@bp.route("/auto-annotation/gt-versions/<int:version_id>", methods=["GET"])
def get_gt_version(version_id):
    """取某版本快照内容。版本不存在→404(21024)；快照文件缺失→404(21023)。"""
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT id, video_id, task_id, version_no, path, parent_version_no, "
        "review_status, created_at FROM gt_versions WHERE id = ?",
        (version_id,),
    )
    v = cur.fetchone()
    if not v:
        raise ApiError(21024, "版本不存在", 404)
    snap = Path(v["path"])
    if not snap.exists():
        raise ApiError(21023, "版本快照文件不存在", 404)
    try:
        content = json.loads(snap.read_text(encoding="utf-8"))
    except Exception as e:
        raise ApiError(41080, str(e), 500)
    return ok({"version": dict(v), "content": content})


@bp.route("/auto-annotation/gt-versions/<int:version_id>:restore", methods=["POST"])
def restore_gt_version(version_id):
    """回滚：把指定版本快照内容写回当前 GT（gt_dir/{video_id}.json）并记新版本。
    版本不存在→404(21024)；快照文件缺失→404(21023)。"""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id, video_id, version_no, path FROM gt_versions WHERE id = ?", (version_id,))
    v = cur.fetchone()
    if not v:
        raise ApiError(21024, "版本不存在", 404)
    snap = Path(v["path"])
    if not snap.exists():
        raise ApiError(21023, "版本快照文件不存在", 404)
    try:
        content = json.loads(snap.read_text(encoding="utf-8"))
    except Exception as e:
        raise ApiError(41080, str(e), 500)

    # 写回当前生效 GT
    gt_dir = Path(current_app.config["GROUND_TRUTH_DIR"])
    gt_dir.mkdir(parents=True, exist_ok=True)
    gt_path = gt_dir / f"{v['video_id']}.json"
    with open(gt_path, "w", encoding="utf-8") as f:
        json.dump(content, f, ensure_ascii=False, indent=2)
    # 回滚也记一个新版本（内容=被回滚版本的内容）
    from app.routes.videos import _snapshot_gt_version
    new_no = _snapshot_gt_version(v["video_id"], content, gt_dir.parent / "ground_truth_versions")
    return ok({"video_id": v["video_id"],
                "restored_from_version": v["version_no"],
                "new_version_no": new_no})
