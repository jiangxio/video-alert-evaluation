"""/api/v1/assistant 资源族端点（AI 助手聊天 + 任务）。

委托 app/routes/assistant.py 的 5 个端点（chat/confirm/cancel/clear/history：
chat/confirm 调 LLM + 可能起 worker；clear/history 用 session + 过滤逻辑——委托避免
复制 session/过滤逻辑引入偏差），只在新端点套统一信封 + 5 位错误码（FF=12，
见 docs/rest-api-error-codes.md）。旧视图在同一个 request context 内运行，session/
request.get_json/get_db/current_app 可用，故请求体由旧视图自读。

纯查询/CRUD（settings GET/POST、tasks 列表、tasks/<id>）原位重写，复用 get_db +
service 层纯读函数。tasks 列表用 SQL 层 LIMIT/OFFSET + COUNT(*) 真分页。

核心约束（不改交互语义）：chat/confirm/cancel 的 200 + {type:'error',...} 是前端
契约，v1 **保 200 套 ok(body)，绝不映射成 4xx/5xx**（NOT_CONFIGURED/EXECUTION_FAILED/
CONFIRMATION_EXPIRED 等仍是 200+type body）。仅「消息不能为空」「缺少 confirmation_id」
这类纯参数缺失映射为 400。无 DELETE 端点、无 400→409（HTTP 语义已干净）。

委托边界盲区（bug-audit 另修）：worker（confirm 触发的 batch_ocr/add_watermark/concat/
package/extract）是旧生产代码，v1 不改不重测；#20 LLM 无 timeout（assistant_service
._call_openai，max_tokens 已设 2048 但 timeout 缺）、#21 execute_update_alert_status
空操作返成功、低危 list_alerts cursor.rowcount -1。
"""
import json

from flask import Blueprint, request

from app.database import get_db
from app.routes import assistant as _legacy
from app.services.assistant_settings import get_settings_for_display, update_assistant_settings
from app.services.assistant_tasks import get_assistant_task, get_task_progress
from ._helpers import parse_pagination, paginate, raise_msg
from .compat import call_old_view
from .responses import ok, ApiError

bp = Blueprint("api_v1_assistant", __name__, url_prefix="/api/v1")

# 服务端错误兜底码（FF=12 SS=80 段）
_ASSISTANT_FALLBACK = (41200, 500, "操作失败")

# chat 的 error 文案 → (5 位码, http_status)
_CHAT_MSG_CODE = {"消息不能为空": (11200, 400)}
# confirm/cancel 的 error 文案 → (5 位码, http_status)
_CONFIRM_MSG_CODE = {"缺少 confirmation_id": (11201, 400)}
_CANCEL_MSG_CODE = {"缺少 confirmation_id": (11202, 400)}


@bp.route("/assistant/settings", methods=["GET"])
def get_settings():
    """获取当前设置（API Key 脱敏）。对齐旧 api_get_settings。"""
    return ok({"settings": get_settings_for_display()})


@bp.route("/assistant/settings", methods=["POST"])
def update_settings():
    """更新设置。请求体由旧 service update_assistant_settings 处理。"""
    data = request.get_json() or {}
    update_assistant_settings(data)
    return ok({"settings": get_settings_for_display()})


@bp.route("/assistant/chat", methods=["POST"])
def chat():
    """聊天（委托旧 api_chat：调 LLM + 工具循环）。成功 200（含 type:'error' 也保 200，
    前端契约）。消息为空→400(11200)。请求体 {message} 由旧视图自读。"""
    body, status = call_old_view(_legacy.api_chat)
    if status == 200:
        return ok(body)
    raise_msg(body, _CHAT_MSG_CODE, fallback=_ASSISTANT_FALLBACK)


@bp.route("/assistant/pending-confirmations:confirm", methods=["POST"])
def confirm():
    """确认执行待确认操作（委托旧 api_confirm：可能起 worker）。成功 200（含 type:'error'
    也保 200）。缺 confirmation_id→400(11201)。请求体 {confirmation_id} 由旧视图自读。"""
    body, status = call_old_view(_legacy.api_confirm)
    if status == 200:
        return ok(body)
    raise_msg(body, _CONFIRM_MSG_CODE, fallback=_ASSISTANT_FALLBACK)


@bp.route("/assistant/pending-confirmations:cancel", methods=["POST"])
def cancel():
    """取消待确认操作（委托旧 api_cancel）。成功 200。缺 confirmation_id→400(11202)。"""
    body, status = call_old_view(_legacy.api_cancel)
    if status == 200:
        return ok(body)
    raise_msg(body, _CANCEL_MSG_CODE, fallback=_ASSISTANT_FALLBACK)


@bp.route("/assistant/sessions:clear", methods=["POST"])
def clear_history():
    """清除当前会话对话历史（委托旧 api_clear：用 session，避免复制 session 逻辑）。成功 200。"""
    body, _ = call_old_view(_legacy.api_clear)
    return ok({"message": body.get("message")})


@bp.route("/assistant/sessions/history", methods=["GET"])
def history():
    """获取当前会话对话历史（委托旧 api_history：含 role/tool_calls 过滤逻辑，避免复制偏差）。成功 200。"""
    body, _ = call_old_view(_legacy.api_history)
    return ok({"messages": body.get("messages", [])})


@bp.route("/assistant/tasks", methods=["GET"])
def list_tasks():
    """assistant 任务列表（真分页，对齐旧 api_list_tasks 字段 + params JSON 解析）。"""
    page, page_size = parse_pagination()
    base = """
        SELECT id, task_type, ref_type, ref_id, status, params,
               result_summary, error_message, created_at, updated_at
        FROM assistant_tasks
    """

    def _mapper(r):
        t = dict(r)
        try:
            t["params"] = json.loads(t["params"]) if t["params"] else {}
        except Exception:
            t["params"] = {}
        return t

    return paginate(get_db(), base, "ORDER BY created_at DESC", (), page, page_size, _mapper)


@bp.route("/assistant/tasks/<int:task_id>", methods=["GET"])
def get_task(task_id):
    """查询 assistant 任务状态。任务不存在→404(21200)。"""
    task = get_assistant_task(task_id)
    if not task:
        raise ApiError(21200, "任务不存在", 404)
    progress = get_task_progress(task_id)
    return ok({"task": task, "progress": progress})
