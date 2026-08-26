"""/api/v1/alerts OCR 系列（5 端点）。

委托 app.routes.alerts 的 5 个旧 OCR 视图，不改其后台线程 / _ocr_progress /
_ocr_lock / 线程内独立 sqlite 连接（CLAUDE.md 高风险区），只在新端点套统一信封 +
5 位错误码。旧视图在同一个 request context 内运行，request.get_json / get_db /
current_app 均可用，故 ocr:manual / ocr:batch 的请求体由旧视图自读，新端点透传。

语义保持：ocr_single 的 OCR 失败仍 HTTP 200（success:false 在 data 里）；
ocr_batch 当场同步起线程非排队故 200；ocr_status 无任务返 200 空进度 + message
（修正旧版 404，前端轮询统一解析）；ocr_cancel 幂等，未运行也 200。
"""
from flask import Blueprint

from app.routes import alerts as _legacy
from .compat import call_old_view
from .responses import ok, ApiError

bp = Blueprint("api_v1_alerts_ocr", __name__, url_prefix="/api/v1")

# 旧视图错误 → 5 位错误码（docs/rest-api-error-codes.md：FF=02 datasets、FF=03 alert-images）。
# 元组顺序 = (code, http_status, default_message)，与 _raise_from_legacy 的索引语义一致。
_IMG_NOT_FOUND = (20320, 404, "图片不存在")
_DS_NOT_FOUND = (20220, 404, "数据集不存在")
_OCR_RUNNING = (30340, 409, "OCR 正在运行中")
_NO_IMG_TO_OCR = (10311, 400, "没有需要 OCR 的图片")


def _raise_from_legacy(body, status, mapping):
    """旧视图返回非 200 时按 status 映射到 ApiError；body 里若无 error 文案用默认。
    mapping = (code, http_status, default_message)。"""
    if body and isinstance(body, dict) and body.get("error"):
        msg = body["error"]
    else:
        msg = mapping[2]
    raise ApiError(mapping[0], msg, mapping[1])


# ── 单图 OCR（同步） ───────────────────────────────────────────────────────────

@bp.route("/alerts/images/<int:image_id>/ocr", methods=["POST"])
def ocr_single(image_id):
    """对单张告警图执行 OCR。OCR 失败仍 200（success:false, ocr:{error} 在 data）。"""
    body, status = call_old_view(_legacy.ocr_single, image_id)
    if status == 404:
        _raise_from_legacy(body, status, _IMG_NOT_FOUND)
    return ok({"success": body.get("success"), "ocr": body.get("ocr")})


@bp.route("/alerts/images/<int:image_id>/ocr:manual", methods=["POST"])
def ocr_save_manual(image_id):
    """手动保存 OCR 结果。请求体 {video_id,timestamp,timestamp_seconds,success} 由旧视图读取。"""
    body, status = call_old_view(_legacy.ocr_save_manual, image_id)
    if status == 404:
        _raise_from_legacy(body, status, _IMG_NOT_FOUND)
    return ok({"ocr": body.get("ocr")})


# ── 批量 OCR（后台线程） ───────────────────────────────────────────────────────

@bp.route("/alerts/datasets/<int:dataset_id>/ocr:batch", methods=["POST"])
def ocr_batch(dataset_id):
    """启动批量 OCR。旧视图当场起 daemon 线程，非排队，故 200（不改 202）。
    请求体 {force_all,stop_on_failure} 由旧视图读取。"""
    body, status = call_old_view(_legacy.ocr_batch, dataset_id)
    if status == 404:
        _raise_from_legacy(body, status, _DS_NOT_FOUND)
    if status == 400:
        _raise_from_legacy(body, status, _NO_IMG_TO_OCR)
    if status == 409:
        _raise_from_legacy(body, status, _OCR_RUNNING)
    return ok({"total": body.get("total")})


@bp.route("/alerts/datasets/<int:dataset_id>/ocr-status", methods=["GET"])
def ocr_status(dataset_id):
    """查询批量 OCR 进度。无任务 → 200 空进度 + message（修正旧版 404，前端轮询统一解析）。
    不校验数据集存在性，对齐旧版语义。"""
    body, status = call_old_view(_legacy.ocr_status, dataset_id)
    if status == 404:
        # 旧版无任务返 404，此处改为 200 + 空进度，前端轮询统一按 data 解析
        return ok(
            {
                "total": 0,
                "done": 0,
                "running": False,
                "cancelled": False,
                "stopped": False,
                "results": [],
            },
            message="没有正在进行的 OCR 任务",
        )
    return ok(body)


@bp.route("/alerts/datasets/<int:dataset_id>/ocr-status:cancel", methods=["POST"])
def ocr_cancel(dataset_id):
    """中断批量 OCR（已成功的保留）。幂等，未运行也返 200。"""
    call_old_view(_legacy.ocr_cancel, dataset_id)
    return ok({"cancelled": True})
