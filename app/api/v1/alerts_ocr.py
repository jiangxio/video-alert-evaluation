"""alerts OCR 系列 v1 端点（sxs-rest-api-alerts-ocr.md 蓝图，第 3 步）。

OCR 逻辑（后台线程 / `_ocr_progress` 内存态 / 线程内独立 sqlite 连接）属 CLAUDE.md
警告的高风险区——只 wrap 委托不改。

唯一改语义的点：`ocr-status` 无任务时旧版返 404，v1 改返 200 空进度，让前端轮询
统一按 data 解析、无需区分 404/200（对应 sxs 文档 §3 的有意偏差）。

不引入 sxs 文档的 5 位错误码 / BLUEPRINTS / call_old_view——沿用已落地的
wrap_old_view + v1_bp + 方案3 error_code。
"""
from flask import Response

from app.api.v1 import v1_bp
from app.api.v1.compat import _split_rv, wrap_old_view
from app.api.v1.responses import ok
from app.routes.alerts import (
    ocr_batch,
    ocr_cancel,
    ocr_save_manual,
    ocr_single,
    ocr_status,
)

# 预包装旧视图（CRUD），避免每次请求重复构造
_ocr_single = wrap_old_view(ocr_single)
_ocr_manual = wrap_old_view(ocr_save_manual)
_ocr_batch = wrap_old_view(ocr_batch)
_ocr_cancel = wrap_old_view(ocr_cancel)


def _extract(raw):
    """从旧视图返回值取 (data, status)：处理 Response / tuple。"""
    body, status, _ = _split_rv(raw)
    if isinstance(body, Response):
        data = body.get_json(silent=True)
        if status == 200:
            status = body.status_code
    else:
        data = body
    return data, status


@v1_bp.route("/alerts/images/<int:image_id>/ocr", methods=["POST"])
def v1_ocr_single(image_id):
    """对单张图片执行 OCR。OCR 失败仍 HTTP 200（success 在 data，不在 HTTP）。"""
    return _ocr_single(image_id)


@v1_bp.route("/alerts/images/<int:image_id>/ocr:manual", methods=["POST"])
def v1_ocr_manual(image_id):
    """手动保存 OCR 结果。"""
    return _ocr_manual(image_id)


@v1_bp.route("/alerts/datasets/<int:dataset_id>/ocr:batch", methods=["POST"])
def v1_ocr_batch(dataset_id):
    """批量 OCR（后台线程）。

    保持 200 非 202：旧版当场同步起 daemon 线程（非排队），故 200。
    404=数据集不存在；400=无可 OCR 的图；409=已在运行（错误信封由 wrap 转换）。
    """
    return _ocr_batch(dataset_id)


@v1_bp.route("/alerts/datasets/<int:dataset_id>/ocr-status", methods=["GET"])
def v1_ocr_status(dataset_id):
    """查询批量 OCR 进度。

    有意偏差：无任务时旧版返 404，v1 改返 200 空进度，便于前端轮询统一解析。
    """
    data, status = _extract(ocr_status(dataset_id))
    if status == 404:
        return ok({
            "total": 0,
            "done": 0,
            "running": False,
            "cancelled": False,
            "stopped": False,
            "results": [],
        })
    return ok(data)


@v1_bp.route("/alerts/datasets/<int:dataset_id>/ocr-status:cancel", methods=["POST"])
def v1_ocr_cancel(dataset_id):
    """中断批量 OCR（幂等：未运行也 200）。"""
    return _ocr_cancel(dataset_id)
