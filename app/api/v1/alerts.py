"""alerts 资源 v1 端点（sxs2 蓝图子集，OCR 5 端点留第 3 步）。

策略（对应 docs/rest-api-alerts-borrow-analysis.md + sxs2 设计决策）：
- 方案 A（不改旧视图、接受查询重复）：CRUD/二进制走 wrap_old_view 委托；列表端点
  走 paginate_old_list（wrap 补不回 total/has_next）。
- PATCH 严格字段白名单：datasets 只 mode、images 只 event_label、eval-sets 只 name，
  未知字段返 400 UNKNOWN_FIELD（pick_fields）。
- :action 命名：images:import / images:batch-delete / datasets:batch-add / datasets:batch-remove。
- download POST→GET（读操作）。
- 两个新详情端点（datasets/<id>、eval-sets/<id> GET）用「调旧 list + 按 id 过滤」实现，
  复用旧版富化逻辑，零 SQL 重复。
- batch-add/remove 重实现：set_id 从 path（旧版从 body），复用 _parse_id_list 去重。

已知偏差（记入 docstring，后续可优化）：
- creates 返 200 非 201（旧视图均返 200，wrap 保持；与 videos 一致）。
- images list 沿用旧 per_page 参数名（保留服务端筛选分页，不重写复杂查询）。
"""
import json

from flask import Response, request

from app.api.v1 import v1_bp
from app.api.v1.compat import (
    _extract_message,
    _split_rv,
    paginate_old_list,
    wrap_old_view,
)
from app.api.v1.responses import err, ok, pick_fields
from app.database import get_db
from app.routes.alerts import (
    _parse_id_list,
    batch_delete_images,
    create_alert_eval_set,
    create_dataset,
    delete_alert_eval_set,
    delete_dataset,
    delete_image,
    download_dataset,
    get_dataset_algorithm_versions,
    get_image_detail,
    import_zip,
    list_alert_eval_sets,
    list_dataset_images,
    list_datasets,
    list_image_logs,
    rename_alert_eval_set,
    serve_image,
    set_dataset_algorithm_versions,
    set_label,
    update_dataset_mode,
    upload_to_dataset,
)

# 预包装旧视图（CRUD/二进制），避免每次请求重复构造
_create_dataset = wrap_old_view(create_dataset)
_delete_dataset = wrap_old_view(delete_dataset)
_update_mode = wrap_old_view(update_dataset_mode)
_set_algo_versions = wrap_old_view(set_dataset_algorithm_versions)
_upload = wrap_old_view(upload_to_dataset)
_import = wrap_old_view(import_zip)
_batch_delete = wrap_old_view(batch_delete_images)
_download = wrap_old_view(download_dataset)
_get_image = wrap_old_view(get_image_detail)
_serve_image = wrap_old_view(serve_image)
_set_label = wrap_old_view(set_label)
_delete_image = wrap_old_view(delete_image)
_create_eval_set = wrap_old_view(create_alert_eval_set)
_rename_eval_set = wrap_old_view(rename_alert_eval_set)
_delete_eval_set = wrap_old_view(delete_alert_eval_set)


def _extract(raw):
    """从旧视图返回值取 (data, status)：处理 Response / tuple / 裸 dict。"""
    body, status, _ = _split_rv(raw)
    if isinstance(body, Response):
        data = body.get_json(silent=True)
        if status == 200:
            status = body.status_code
    else:
        data = body
    return data, status


# ── 数据集 datasets ────────────────────────────────────────────────────────────

@v1_bp.route("/alerts/datasets", methods=["GET"])
def v1_list_datasets():
    """数据集列表（含 image_count、algorithm_versions），分页信封。"""
    return paginate_old_list(list_datasets)


@v1_bp.route("/alerts/datasets", methods=["POST"])
def v1_create_dataset():
    return _create_dataset()


@v1_bp.route("/alerts/datasets/<int:dataset_id>", methods=["GET"])
def v1_get_dataset(dataset_id):
    """数据集详情（REST 补全，旧版无 GET /<id>）。调旧 list 按 id 过滤，复用富化逻辑。"""
    data, _ = _extract(list_datasets())
    items = data if isinstance(data, list) else []
    for item in items:
        if item.get("id") == dataset_id:
            return ok(item)
    return err(404, "数据集不存在", error_code="DATASET_NOT_FOUND")


@v1_bp.route("/alerts/datasets/<int:dataset_id>", methods=["DELETE"])
def v1_delete_dataset(dataset_id):
    return _delete_dataset(dataset_id)


@v1_bp.route("/alerts/datasets/<int:dataset_id>", methods=["PATCH"])
def v1_patch_dataset(dataset_id):
    """只改 mode。未知字段返 400 UNKNOWN_FIELD；mode 值校验仍在旧视图。"""
    data = request.get_json(silent=True) or {}
    _, unknown = pick_fields(data, {"mode"})
    if unknown:
        return err(400, f"不支持的字段: {unknown}", error_code="UNKNOWN_FIELD")
    return _update_mode(dataset_id)


# ── 算法版本 algorithm-versions ────────────────────────────────────────────────

@v1_bp.route("/alerts/datasets/<int:dataset_id>/algorithm-versions", methods=["GET"])
def v1_list_algorithm_versions(dataset_id):
    """数据集启用的算法版本列表。数据集不存在时旧版返 404 → 透传错误信封。"""
    return paginate_old_list(lambda: get_dataset_algorithm_versions(dataset_id))


@v1_bp.route("/alerts/datasets/<int:dataset_id>/algorithm-versions", methods=["POST"])
def v1_set_algorithm_versions(dataset_id):
    """提交算法版本选择（保持 POST：服务端有校验 + is_active 历史逻辑）。"""
    return _set_algo_versions(dataset_id)


# ── 数据集图片 images 集合 ─────────────────────────────────────────────────────

@v1_bp.route("/alerts/datasets/<int:dataset_id>/images", methods=["GET"])
def v1_list_dataset_images(dataset_id):
    """数据集图片列表。旧版已服务端分页+筛选，这里重塑为 v1 信封（保留旧的 total/page）。

    已知偏差：沿用旧参数名 per_page（非 page_size）及 event_type/video_id/label_status
    筛选参数——不重写复杂查询，避免口径漂移。
    """
    data, status = _extract(list_dataset_images(dataset_id))
    if status >= 400:
        return err(status, _extract_message(data), error_code="DATASET_NOT_FOUND")
    data = data or {}
    items = data.get("images", [])
    total = data.get("total", 0)
    page = data.get("page", 1)
    page_size = data.get("per_page", 20)
    has_next = page * page_size < total
    return ok({
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_next": has_next,
    })


@v1_bp.route("/alerts/datasets/<int:dataset_id>/images", methods=["POST"])
def v1_upload_images(dataset_id):
    """多文件上传（字段 image）。"""
    return _upload(dataset_id)


@v1_bp.route("/alerts/datasets/<int:dataset_id>/images:import", methods=["POST"])
def v1_import_images(dataset_id):
    """zip/tar/tar.gz 导入（字段 file）。"""
    return _import(dataset_id)


@v1_bp.route("/alerts/datasets/<int:dataset_id>/images:batch-delete", methods=["POST"])
def v1_batch_delete_images(dataset_id):
    """批量删除（body 可带 video_id/event_type 筛选）。"""
    return _batch_delete(dataset_id)


@v1_bp.route("/alerts/datasets/<int:dataset_id>/images/logs", methods=["GET"])
def v1_list_image_logs(dataset_id):
    """数据集图片操作日志（分页信封）。"""
    return paginate_old_list(lambda: list_image_logs(dataset_id), list_key="logs")


@v1_bp.route("/alerts/datasets/<int:dataset_id>/download", methods=["GET"])
def v1_download_dataset(dataset_id):
    """打包下载全部图片（zip，二进制透传；旧版 POST→GET）。"""
    return _download(dataset_id)


# ── 单张图片 images ────────────────────────────────────────────────────────────

@v1_bp.route("/alerts/images/<int:image_id>", methods=["GET"])
def v1_get_image(image_id):
    """图片详情（含最新 OCR 结果）。"""
    return _get_image(image_id)


@v1_bp.route("/alerts/images/<int:image_id>/file", methods=["GET"])
def v1_serve_image(image_id):
    """图片文件/缩略图（?w= & ?h= 生成缩略图，二进制透传）。"""
    return _serve_image(image_id)


@v1_bp.route("/alerts/images/<int:image_id>", methods=["PATCH"])
def v1_patch_image(image_id):
    """只改 event_label。未知字段返 400 UNKNOWN_FIELD。"""
    data = request.get_json(silent=True) or {}
    _, unknown = pick_fields(data, {"event_label"})
    if unknown:
        return err(400, f"不支持的字段: {unknown}", error_code="UNKNOWN_FIELD")
    return _set_label(image_id)


@v1_bp.route("/alerts/images/<int:image_id>", methods=["DELETE"])
def v1_delete_image(image_id):
    return _delete_image(image_id)


# ── 告警评测集 eval-sets ───────────────────────────────────────────────────────

@v1_bp.route("/alerts/eval-sets", methods=["GET"])
def v1_list_alert_eval_sets():
    """评测集列表（含 dataset_count/image_count/dataset_names），分页信封。"""
    return paginate_old_list(list_alert_eval_sets, list_key="sets")


@v1_bp.route("/alerts/eval-sets", methods=["POST"])
def v1_create_alert_eval_set():
    return _create_eval_set()


@v1_bp.route("/alerts/eval-sets/<int:set_id>", methods=["GET"])
def v1_get_alert_eval_set(set_id):
    """评测集详情（REST 补全）。调旧 list 按 id 过滤，复用富化逻辑。"""
    data, _ = _extract(list_alert_eval_sets())
    sets = (data or {}).get("sets", []) if isinstance(data, dict) else []
    for item in sets:
        if item.get("id") == set_id:
            return ok(item)
    return err(404, "评测集不存在", error_code="EVAL_SET_NOT_FOUND")


@v1_bp.route("/alerts/eval-sets/<int:set_id>", methods=["PATCH"])
def v1_patch_alert_eval_set(set_id):
    """只改 name。未知字段返 400 UNKNOWN_FIELD。"""
    data = request.get_json(silent=True) or {}
    _, unknown = pick_fields(data, {"name"})
    if unknown:
        return err(400, f"不支持的字段: {unknown}", error_code="UNKNOWN_FIELD")
    return _rename_eval_set(set_id)


@v1_bp.route("/alerts/eval-sets/<int:set_id>", methods=["DELETE"])
def v1_delete_alert_eval_set(set_id):
    return _delete_eval_set(set_id)


@v1_bp.route("/alerts/eval-sets/<int:set_id>/datasets:batch-add", methods=["POST"])
def v1_alert_eval_set_batch_add(set_id):
    """批量添加数据集到评测集（set_id 从 path，dataset_ids 从 body）。

    重实现去重（方案 A）：旧版从 body 读 set_id，v1 改 RESTful path 参数，不能 raw-wrap。
    复用 _parse_id_list，逻辑仅 list append + json dump。
    """
    data = request.get_json(silent=True) or {}
    dataset_ids = data.get("dataset_ids", [])
    if not dataset_ids:
        return err(400, "请选择要添加的数据集", error_code="VALIDATION_ERROR")

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT dataset_ids FROM eval_alert_sets WHERE id = ?", (set_id,))
    row = cur.fetchone()
    if not row:
        return err(404, "评测集不存在", error_code="EVAL_SET_NOT_FOUND")

    current = _parse_id_list(row["dataset_ids"])
    added_count = 0
    for did in dataset_ids:
        if did not in current:
            current.append(did)
            added_count += 1
    cur.execute(
        "UPDATE eval_alert_sets SET dataset_ids = ? WHERE id = ?",
        (json.dumps(current), set_id),
    )
    db.commit()
    return ok({"added_count": added_count})


@v1_bp.route("/alerts/eval-sets/<int:set_id>/datasets:batch-remove", methods=["POST"])
def v1_alert_eval_set_batch_remove(set_id):
    """批量移除数据集（set_id 从 path，dataset_ids 从 body）。重实现，同 batch-add。"""
    data = request.get_json(silent=True) or {}
    dataset_ids = data.get("dataset_ids", [])
    if not dataset_ids:
        return err(400, "请选择要移出的数据集", error_code="VALIDATION_ERROR")

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT dataset_ids FROM eval_alert_sets WHERE id = ?", (set_id,))
    row = cur.fetchone()
    if not row:
        return err(404, "评测集不存在", error_code="EVAL_SET_NOT_FOUND")

    current = _parse_id_list(row["dataset_ids"])
    removed_count = 0
    for did in dataset_ids:
        if did in current:
            current.remove(did)
            removed_count += 1
    cur.execute(
        "UPDATE eval_alert_sets SET dataset_ids = ? WHERE id = ?",
        (json.dumps(current), set_id),
    )
    db.commit()
    return ok({"removed_count": removed_count})
