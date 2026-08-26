"""/api/v1/review 资源族端点（误检复核工作台 + 智能审查）。

委托 app/routes/review.py 的 2 个高风险端点（ai-check/ai-check/status：起 daemon
线程 `_ai_check_worker` 调多模态 OpenAI + 模块级 `_ai_batches`/`_ai_batches_lock`
状态），只在新端点套统一信封 + 5 位错误码（FF=14，见 docs/rest-api-error-codes.md）。
旧视图在同一个 request context 内运行，request.get_json/get_db/current_app 可用，
故 ai-check 的请求体由旧视图自读，ai-check/status 的 batch_id 由旧视图从 query 自读。

纯查询端点（alerts/gt-context）原位重写，复用 get_db + `eval_service.get_effective_status`
（纯读，不算指标——review 只写 ai_suggestion，不碰 manual_status/is_false_positive，
后者写端点在 evaluation.py:912/938 归 FF=13）。

语义修正（新端点专属，旧不动）：ai-check 成功保 200（对齐 auto-annotation start /
OCR ocr:batch「200 不改 202」先例）；ai-check/status 保 query param batch_id（委托
兼容，不强行入路径）。review 无 DELETE 端点。

委托边界盲区（bug-audit 另修）：worker `_ai_check_worker`/`_review_one`/`_parse_suggestion`
是旧生产代码，v1 不改不重测，只验信封/状态码/错误码/委托真触发（batch 落 _ai_batches）。
注 review 是全仓唯一自带 LLM timeout=120 的调用点（review.py:333），不受 #20 影响。
"""
import json

from flask import Blueprint, request

from app.database import get_db
from app.routes import review as _legacy
from app.services.eval_service import get_effective_status
from ._helpers import raise_msg
from .compat import call_old_view
from .responses import ok, ApiError

bp = Blueprint("api_v1_review", __name__, url_prefix="/api/v1")

# 服务端错误兜底码（FF=14 SS=80 段）
_REVIEW_FALLBACK = (41400, 500, "操作失败")

# ai-check 的 error 文案 → (5 位码, http_status)
_AI_CHECK_MSG_CODE = {
    "请提供 merged_ids 列表": (11400, 400),
    "任务不存在": (21400, 404),
    "未配置 OpenAI 兼容 API": (11402, 400),
    "未找到匹配的告警记录": (21401, 404),
}

# ai-check/status 的 error 文案 → (5 位码, http_status)
_AI_STATUS_MSG_CODE = {
    "缺少 batch_id": (11403, 400),
    "批次不存在": (21402, 404),
    "批次不属于该任务": (21402, 404),
}


@bp.route("/review/tasks/<int:task_id>/alerts", methods=["GET"])
def get_alerts(task_id):
    """任务全量告警（含 effective_status + ai_suggestion，前端做筛选/分组，保全量不分页）。
    任务不存在→404(21400)。"""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM eval_tasks WHERE id = ?", (task_id,))
    if not cur.fetchone():
        raise ApiError(21400, "任务不存在", 404)

    cur.execute('''
        SELECT m.id, m.video_id, m.event_type, m.image_ids,
               m.representative_image_id, m.ts_start, m.ts_end,
               m.is_false_positive, m.matched_gt_event_id, m.manual_status,
               m.ai_suggestion,
               o.timestamp_seconds
        FROM eval_merged_events m
        LEFT JOIN (
            SELECT alert_image_id, timestamp_seconds
            FROM ocr_results
            WHERE id IN (SELECT MAX(id) FROM ocr_results GROUP BY alert_image_id)
        ) o ON o.alert_image_id = m.representative_image_id
        WHERE m.task_id = ?
        ORDER BY m.video_id, m.event_type, m.ts_start
    ''', (task_id,))
    rows = [dict(r) for r in cur.fetchall()]

    for r in rows:
        r["image_ids"] = json.loads(r.get("image_ids") or "[]")
        r["effective_status"] = get_effective_status(r)
        raw = r.get("ai_suggestion")
        if raw:
            try:
                r["ai_suggestion"] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                r["ai_suggestion"] = None
        else:
            r["ai_suggestion"] = None

    return ok({"alerts": rows, "count": len(rows)})


@bp.route("/review/tasks/<int:task_id>/gt-context", methods=["GET"])
def gt_context(task_id):
    """某视频的 GT 事件区间 + 同视频告警时间点（时间轴渲染）。
    缺少 video_id→400(11401)；任务不存在返空（对齐旧版不查任务存在性）。"""
    video_id = request.args.get("video_id")
    if not video_id:
        raise ApiError(11401, "缺少 video_id", 400)

    db = get_db()
    cur = db.cursor()
    cur.execute('''
        SELECT id, event_type, start_sec, end_sec
        FROM eval_gt_events
        WHERE task_id = ? AND video_id = ?
        ORDER BY start_sec
    ''', (task_id, video_id))
    gt_events = [dict(r) for r in cur.fetchall()]

    cur.execute('''
        SELECT id, event_type, ts_start, ts_end, is_false_positive, manual_status
        FROM eval_merged_events
        WHERE task_id = ? AND video_id = ?
        ORDER BY ts_start
    ''', (task_id, video_id))
    alerts = []
    for r in cur.fetchall():
        d = dict(r)
        d["effective_status"] = get_effective_status(d)
        alerts.append(d)

    return ok({"gt_events": gt_events, "alerts": alerts})


@bp.route("/review/tasks/<int:task_id>/ai-check", methods=["POST"])
def ai_check(task_id):
    """提交批量智能审查（委托旧 ai_check：校验→起 _ai_check_worker 线程）。
    请求体 {merged_ids} 由旧视图自读。成功 200。"""
    body, status = call_old_view(_legacy.ai_check, task_id)
    if status == 200:
        return ok({"batch_id": body.get("batch_id"), "total": body.get("total")})
    raise_msg(body, _AI_CHECK_MSG_CODE, fallback=_REVIEW_FALLBACK)


@bp.route("/review/tasks/<int:task_id>/ai-check/status", methods=["GET"])
def ai_check_status(task_id):
    """轮询批量审查进度（委托旧 ai_check_status：读模块态 _ai_batches，batch_id 由 query 传）。
    缺 batch_id→400(11403)；批次不存在/不属于该任务→404(21402)。成功 200。"""
    body, status = call_old_view(_legacy.ai_check_status, task_id)
    if status == 200:
        return ok({
            "status": body.get("status"),
            "total": body.get("total"),
            "done": body.get("done"),
            "current_id": body.get("current_id"),
            "results": body.get("results"),
            "error": body.get("error"),
        })
    raise_msg(body, _AI_STATUS_MSG_CODE, fallback=_REVIEW_FALLBACK)
