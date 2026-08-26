"""/api/v1/alerts 资源族端点（查询型 CRUD，不含 OCR 系列）。

按批准计划实现 24 个端点：datasets CRUD + PATCH(mode)、algorithm-versions
GET/POST(保持 POST)、images 集合 GET/POST(多文件)/:import/:batch-delete/
logs/download(GET)、images 单条 GET/file/PATCH(label)/DELETE、eval-alert-sets
CRUD + GET + 成员 :batch-add/:batch-remove。OCR 系列依赖 _ocr_progress 内存态
进度+后台线程，留待后续阶段委托旧视图实现。

风格：只改 URL 结构 + 信封 + 明显错误动词（download POST→GET），不改交互语义。
单字段更新用 PATCH（旧版 PUT 改单字段不严谨，修正）。复用 app.routes.alerts
私有 helper 与 verification_service，不重复实现。二进制响应不走信封。
"""
import json
import os
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path

from flask import Blueprint, request, current_app, after_this_request

from app.database import get_db
from app.routes import send_file_with_cache, send_image_with_thumbnail
from app.routes.alerts import (
    _extract_archive,
    _find_image_root,
    _get_image_size,
    _get_dataset_algorithm_versions,
    _load_alert_config,
    _log_image_action,
    _parse_id_list,
    _set_dataset_algorithm_versions,
    _validate_algorithm_versions,
)
from app.services.verification_service import extract_alert_type_id
from app.utils import allowed_file, safe_filename
from .responses import ok, created, paginated, no_content, ApiError

bp = Blueprint("api_v1_alerts", __name__, url_prefix="/api/v1")

_IMAGE_FIELDS = (
    "id, filename, file_path, alert_type_id, alert_type, file_size, "
    "uploaded_at, dataset_id, image_width, image_height, event_label"
)
_DATASET_FIELDS = "id, name, notes, mode, created_at"
_EVAL_SET_FIELDS = "id, name, notes, dataset_ids, created_at"


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


def _require_dataset(cursor, dataset_id):
    cursor.execute("SELECT id FROM datasets WHERE id = ?", (dataset_id,))
    if not cursor.fetchone():
        raise ApiError(20220, "数据集不存在", 404)


# ── 数据集 datasets ────────────────────────────────────────────────────────────

@bp.route("/alerts/datasets", methods=["GET"])
def list_datasets():
    page, page_size = _parse_pagination()
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT d.*, COUNT(a.id) AS image_count
        FROM datasets d
        LEFT JOIN alert_images a ON a.dataset_id = d.id
        GROUP BY d.id
        ORDER BY d.created_at DESC
    """)
    rows = []
    for r in cur.fetchall():
        row = dict(r)
        row["algorithm_versions"] = _get_dataset_algorithm_versions(db, row["id"])
        rows.append(row)
    page_rows, total = _slice_page(rows, page, page_size)
    return paginated(page_rows, total, page, page_size)


@bp.route("/alerts/datasets", methods=["POST"])
def create_dataset():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    notes = (data.get("notes") or "").strip()
    mode = data.get("mode", "normal")
    algorithm_version_ids = data.get("algorithm_version_ids", [])
    if not name:
        raise ApiError(10200, "数据集名称不能为空", 400)

    db = get_db()
    cur = db.cursor()
    cur.execute(
        "INSERT INTO datasets (name, notes, mode) VALUES (?, ?, ?)",
        (name, notes or None, mode),
    )
    db.commit()
    dataset_id = cur.lastrowid

    if algorithm_version_ids:
        ok_flag, err = _validate_algorithm_versions(db, algorithm_version_ids)
        if not ok_flag:
            raise ApiError(10201, err, 400)
        _set_dataset_algorithm_versions(db, dataset_id, algorithm_version_ids)

    cur.execute(f"SELECT {_DATASET_FIELDS} FROM datasets WHERE id = ?", (dataset_id,))
    row = dict(cur.fetchone())
    cur.execute("SELECT COUNT(id) AS c FROM alert_images WHERE dataset_id = ?", (dataset_id,))
    row["image_count"] = cur.fetchone()["c"]
    row["algorithm_versions"] = _get_dataset_algorithm_versions(db, dataset_id)
    return created(row, location=f"/api/v1/alerts/datasets/{dataset_id}")


@bp.route("/alerts/datasets/<int:dataset_id>", methods=["GET"])
def get_dataset(dataset_id):
    db = get_db()
    cur = db.cursor()
    _require_dataset(cur, dataset_id)
    cur.execute(f"SELECT {_DATASET_FIELDS} FROM datasets WHERE id = ?", (dataset_id,))
    row = dict(cur.fetchone())
    cur.execute("SELECT COUNT(id) AS c FROM alert_images WHERE dataset_id = ?", (dataset_id,))
    row["image_count"] = cur.fetchone()["c"]
    row["algorithm_versions"] = _get_dataset_algorithm_versions(db, dataset_id)
    return ok(row)


@bp.route("/alerts/datasets/<int:dataset_id>", methods=["DELETE"])
def delete_dataset(dataset_id):
    db = get_db()
    cur = db.cursor()
    _require_dataset(cur, dataset_id)
    cur.execute("SELECT file_path FROM alert_images WHERE dataset_id = ?", (dataset_id,))
    for row in cur.fetchall():
        try:
            os.unlink(row["file_path"])
        except Exception:
            pass
    cur.execute("DELETE FROM alert_images WHERE dataset_id = ?", (dataset_id,))
    cur.execute("DELETE FROM datasets WHERE id = ?", (dataset_id,))
    db.commit()
    return no_content()


@bp.route("/alerts/datasets/<int:dataset_id>", methods=["PATCH"])
def update_dataset(dataset_id):
    """部分更新；本轮只支持 mode 字段（对齐旧版 /mode PUT）。"""
    data = request.get_json(silent=True) or {}
    if "mode" not in data:
        raise ApiError(10210, "没有可更新的字段（mode）", 400)
    mode = data["mode"]
    if mode not in ("normal", "realtime"):
        raise ApiError(10202, "无效的模式，必须是 normal 或 realtime", 400)
    db = get_db()
    cur = db.cursor()
    _require_dataset(cur, dataset_id)
    cur.execute("UPDATE datasets SET mode = ? WHERE id = ?", (mode, dataset_id))
    db.commit()
    cur.execute(f"SELECT {_DATASET_FIELDS} FROM datasets WHERE id = ?", (dataset_id,))
    return ok(dict(cur.fetchone()))


# ── 数据集算法版本 algorithm-versions（子集合） ────────────────────────────────

@bp.route("/alerts/datasets/<int:dataset_id>/algorithm-versions", methods=["GET"])
def get_dataset_algorithm_versions(dataset_id):
    db = get_db()
    cur = db.cursor()
    _require_dataset(cur, dataset_id)
    return ok(_get_dataset_algorithm_versions(db, dataset_id))


@bp.route("/alerts/datasets/<int:dataset_id>/algorithm-versions", methods=["POST"])
def set_dataset_algorithm_versions(dataset_id):
    """设置启用集合（保持 POST：提交选择让服务端校验处理，非客户端全权定状态）。"""
    data = request.get_json(silent=True) or {}
    algorithm_version_ids = data.get("algorithm_version_ids", [])
    db = get_db()
    cur = db.cursor()
    _require_dataset(cur, dataset_id)
    ok_flag, err = _validate_algorithm_versions(db, algorithm_version_ids)
    if not ok_flag:
        raise ApiError(10203, err, 400)
    _set_dataset_algorithm_versions(db, dataset_id, algorithm_version_ids)
    return ok(_get_dataset_algorithm_versions(db, dataset_id))


# ── 数据集图片 images（集合） ──────────────────────────────────────────────────

@bp.route("/alerts/datasets/<int:dataset_id>/images", methods=["GET"])
def list_dataset_images(dataset_id):
    page, page_size = _parse_pagination()
    db = get_db()
    cur = db.cursor()
    _require_dataset(cur, dataset_id)
    cur.execute(
        f"SELECT {_IMAGE_FIELDS} FROM alert_images WHERE dataset_id = ? ORDER BY uploaded_at ASC",
        (dataset_id,),
    )
    rows = []
    for row in cur.fetchall():
        img = dict(row)
        cur.execute(
            "SELECT video_id, timestamp, timestamp_seconds, success "
            "FROM ocr_results WHERE alert_image_id = ? ORDER BY created_at DESC LIMIT 1",
            (img["id"],),
        )
        ocr = cur.fetchone()
        img["ocr"] = dict(ocr) if ocr else None
        rows.append(img)
    page_rows, total = _slice_page(rows, page, page_size)
    return paginated(page_rows, total, page, page_size)


@bp.route("/alerts/datasets/<int:dataset_id>/images", methods=["POST"])
def upload_images(dataset_id):
    """上传图片（多文件）。multipart 字段名 image。"""
    db = get_db()
    cur = db.cursor()
    _require_dataset(cur, dataset_id)
    files = request.files.getlist("image")
    if not files:
        raise ApiError(10301, "没有上传文件", 400)

    config = _load_alert_config()
    uploaded, errors = [], []
    for file in files:
        if not file.filename:
            continue
        if not allowed_file(file.filename, current_app.config["ALLOWED_IMAGE_EXTENSIONS"]):
            errors.append(f"{file.filename}: 不支持的格式")
            continue
        filename = safe_filename(file.filename)
        if not filename:
            errors.append(f"{file.filename}: 非法文件名")
            continue

        cur.execute(
            "SELECT id FROM alert_images WHERE dataset_id = ? AND filename = ?",
            (dataset_id, filename),
        )
        if cur.fetchone():
            errors.append(f"{filename}: 已存在于该数据集")
            continue

        dataset_dir = Path(current_app.config["UPLOAD_ALERTS"]) / str(dataset_id)
        dataset_dir.mkdir(parents=True, exist_ok=True)
        save_path = dataset_dir / filename
        if save_path.exists():
            save_path = save_path.parent / f"{save_path.stem}_{uuid.uuid4().hex[:6]}{save_path.suffix}"
            filename = save_path.name

        file.save(str(save_path))
        width, height = _get_image_size(str(save_path))
        alert_type_id = extract_alert_type_id(filename)
        alert_type = config.get(alert_type_id) if alert_type_id else None
        file_size = save_path.stat().st_size
        cur.execute(
            "INSERT INTO alert_images (filename, file_path, alert_type_id, alert_type, file_size, "
            "dataset_id, image_width, image_height, event_label) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (filename, str(save_path), alert_type_id, alert_type, file_size, dataset_id, width, height, alert_type),
        )
        db.commit()
        uploaded.append({
            "id": cur.lastrowid,
            "filename": filename,
            "alert_type": alert_type,
            "image_width": width,
            "image_height": height,
        })

    _log_image_action(db, dataset_id, "upload", len(uploaded), "; ".join(errors) if errors else None)
    return ok({"uploaded": uploaded, "errors": errors})


@bp.route("/alerts/datasets/<int:dataset_id>/images:import", methods=["POST"])
def import_images(dataset_id):
    """从压缩包（zip/tar/tar.gz/tgz）导入图片。multipart 字段名 file。"""
    db = get_db()
    cur = db.cursor()
    _require_dataset(cur, dataset_id)
    if "file" not in request.files:
        raise ApiError(10302, "没有上传文件", 400)
    f = request.files["file"]
    fname = (f.filename or "").lower()
    supported = (".zip", ".tar", ".tar.gz", ".tgz")
    if not any(fname.endswith(ext) for ext in supported):
        raise ApiError(10303, "仅支持 .zip / .tar / .tar.gz / .tgz 格式", 400)

    config = _load_alert_config()
    image_exts = current_app.config["ALLOWED_IMAGE_EXTENSIONS"]
    tmp_dir = tempfile.mkdtemp()
    try:
        archive_path = os.path.join(tmp_dir, "upload")
        f.save(archive_path)
        _extract_archive(archive_path, tmp_dir, f.filename)
        search_root = _find_image_root(tmp_dir)

        imported, skipped = [], []
        for src in sorted(search_root.rglob("*")):
            if not src.is_file() or src.suffix.lower().lstrip(".") not in image_exts:
                continue
            filename = src.name
            cur.execute(
                "SELECT id FROM alert_images WHERE dataset_id = ? AND filename = ?",
                (dataset_id, filename),
            )
            if cur.fetchone():
                skipped.append(filename)
                continue

            dataset_dir = Path(current_app.config["UPLOAD_ALERTS"]) / str(dataset_id)
            dataset_dir.mkdir(parents=True, exist_ok=True)
            dest = dataset_dir / filename
            if dest.exists():
                dest = dest.parent / f"{dest.stem}_{uuid.uuid4().hex[:6]}{dest.suffix}"
                filename = dest.name
            shutil.copy2(str(src), str(dest))

            width, height = _get_image_size(str(dest))
            alert_type_id = extract_alert_type_id(filename)
            alert_type = config.get(alert_type_id) if alert_type_id else None
            file_size = dest.stat().st_size
            cur.execute(
                "INSERT INTO alert_images (filename, file_path, alert_type_id, alert_type, file_size, "
                "dataset_id, image_width, image_height, event_label) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (filename, str(dest), alert_type_id, alert_type, file_size, dataset_id, width, height, alert_type),
            )
            db.commit()
            imported.append(filename)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    _log_image_action(db, dataset_id, "import", len(imported), f"跳过 {len(skipped)} 张" if skipped else None)
    return ok({"imported": len(imported), "skipped": len(skipped), "skipped_files": skipped})


@bp.route("/alerts/datasets/<int:dataset_id>/images:batch-delete", methods=["POST"])
def batch_delete_images(dataset_id):
    """批量删图片，可选 video_id/event_type 筛选。"""
    db = get_db()
    cur = db.cursor()
    _require_dataset(cur, dataset_id)
    data = request.get_json(silent=True) or {}
    video_id = (data.get("video_id") or "").strip()
    event_type = (data.get("event_type") or "").strip()

    conditions = ["dataset_id = ?"]
    params = [dataset_id]
    if video_id:
        conditions.append("id IN (SELECT alert_image_id FROM ocr_results WHERE video_id = ?)")
        params.append(video_id)
    if event_type:
        conditions.append("alert_type = ?")
        params.append(event_type)

    where_clause = " AND ".join(conditions)
    cur.execute(f"SELECT id, file_path FROM alert_images WHERE {where_clause}", params)
    images = cur.fetchall()
    if not images:
        raise ApiError(20221, "没有找到符合条件的图片", 404)

    deleted_count = 0
    for row in images:
        try:
            os.unlink(row["file_path"])
        except Exception:
            pass
        deleted_count += 1
    cur.execute(f"DELETE FROM alert_images WHERE {where_clause}", params)
    db.commit()

    details_parts = []
    if video_id:
        details_parts.append(f"视频ID: {video_id}")
    if event_type:
        details_parts.append(f"事件类型: {event_type}")
    _log_image_action(db, dataset_id, "batch_delete", deleted_count, "; ".join(details_parts) if details_parts else None)
    return ok({"deleted_count": deleted_count})


@bp.route("/alerts/datasets/<int:dataset_id>/images/logs", methods=["GET"])
def list_image_logs(dataset_id):
    """数据集图片操作日志（最近 50 条）。"""
    db = get_db()
    cur = db.cursor()
    _require_dataset(cur, dataset_id)
    cur.execute(
        "SELECT id, action, image_count, details, created_at "
        "FROM dataset_image_logs WHERE dataset_id = ? ORDER BY created_at DESC LIMIT 50",
        (dataset_id,),
    )
    return ok([dict(r) for r in cur.fetchall()])


@bp.route("/alerts/datasets/<int:dataset_id>/download", methods=["GET"])
def download_dataset(dataset_id):
    """打包下载数据集全部图片为 zip（二进制，不走信封）。"""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id, name FROM datasets WHERE id = ?", (dataset_id,))
    dataset = cur.fetchone()
    if not dataset:
        raise ApiError(20220, "数据集不存在", 404)
    cur.execute("SELECT file_path, filename FROM alert_images WHERE dataset_id = ?", (dataset_id,))
    images = cur.fetchall()
    if not images:
        raise ApiError(20222, "数据集为空", 404)

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".zip")
    os.close(tmp_fd)
    try:
        added = 0
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_STORED) as zf:
            for img in images:
                fp = Path(img["file_path"])
                if fp.exists():
                    zf.write(str(fp), img["filename"])
                    added += 1
        if added == 0:
            os.unlink(tmp_path)
            raise ApiError(20223, "没有可下载的图片文件", 404)

        @after_this_request
        def cleanup(response):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            return response

        return send_file_with_cache(
            tmp_path,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"{dataset['name']}.zip",
        )
    except ApiError:
        raise
    except Exception as e:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise ApiError(40280, f"打包下载失败: {e}", 500)


# ── 单张图片 images ────────────────────────────────────────────────────────────

def _fetch_image(cursor, image_id):
    cursor.execute(f"SELECT {_IMAGE_FIELDS} FROM alert_images WHERE id = ?", (image_id,))
    return cursor.fetchone()


@bp.route("/alerts/images/<int:image_id>", methods=["GET"])
def get_image_detail(image_id):
    db = get_db()
    cur = db.cursor()
    img = _fetch_image(cur, image_id)
    if not img:
        raise ApiError(20320, "图片不存在", 404)
    result = dict(img)
    cur.execute(
        "SELECT video_id, timestamp, timestamp_seconds, success, full_result, raw_ocr_text "
        "FROM ocr_results WHERE alert_image_id = ? ORDER BY created_at DESC LIMIT 1",
        (image_id,),
    )
    ocr_row = cur.fetchone()
    if ocr_row:
        ocr = dict(ocr_row)
        if ocr.get("full_result"):
            try:
                full_result = json.loads(ocr["full_result"])
                for key, value in full_result.items():
                    if key not in ocr or ocr[key] is None:
                        ocr[key] = value
            except (json.JSONDecodeError, TypeError):
                pass
        result["ocr"] = ocr
    else:
        result["ocr"] = None
    return ok(result)


@bp.route("/alerts/images/<int:image_id>/file", methods=["GET"])
def serve_image(image_id):
    """图片文件，?w=&h= 生成缩略图（二进制，不走信封）。"""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT file_path, filename FROM alert_images WHERE id = ?", (image_id,))
    row = cur.fetchone()
    if not row:
        raise ApiError(20320, "图片不存在", 404)
    path = Path(row["file_path"])
    if not path.exists():
        raise ApiError(20321, "文件不存在于磁盘", 404)
    max_w = request.args.get("w", type=int)
    max_h = request.args.get("h", type=int)
    if max_w or max_h:
        return send_image_with_thumbnail(str(path), max_width=max_w, max_height=max_h)
    return send_file_with_cache(str(path))


@bp.route("/alerts/images/<int:image_id>", methods=["PATCH"])
def update_image(image_id):
    """部分更新；只支持 event_label 字段（对齐旧版 /label PUT）。"""
    data = request.get_json(silent=True) or {}
    if "event_label" not in data:
        raise ApiError(10310, "没有可更新的字段（event_label）", 400)
    label = (data.get("event_label") or "").strip()
    if not label:
        raise ApiError(10300, "event_label 不能为空", 400)
    db = get_db()
    cur = db.cursor()
    if not _fetch_image(cur, image_id):
        raise ApiError(20320, "图片不存在", 404)
    cur.execute("UPDATE alert_images SET event_label = ? WHERE id = ?", (label, image_id))
    db.commit()
    return ok({"event_label": label})


@bp.route("/alerts/images/<int:image_id>", methods=["DELETE"])
def delete_image(image_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT file_path FROM alert_images WHERE id = ?", (image_id,))
    row = cur.fetchone()
    if not row:
        raise ApiError(20320, "图片不存在", 404)
    try:
        os.unlink(row["file_path"])
    except Exception:
        pass
    cur.execute("DELETE FROM alert_images WHERE id = ?", (image_id,))
    db.commit()
    return no_content()


# ── 告警评测集 eval-alert-sets（对应 eval_alert_sets 表） ──────────────────────

@bp.route("/alerts/eval-sets", methods=["GET"])
def list_eval_sets():
    page, page_size = _parse_pagination()
    db = get_db()
    cur = db.cursor()
    cur.execute(f"SELECT {_EVAL_SET_FIELDS} FROM eval_alert_sets ORDER BY created_at DESC")
    rows = []
    for row in cur.fetchall():
        item = dict(row)
        dataset_ids = _parse_id_list(item.get("dataset_ids"))
        item["dataset_ids"] = dataset_ids
        item["dataset_count"] = len(dataset_ids)
        item["image_count"] = 0
        item["dataset_names"] = []
        if dataset_ids:
            placeholders = ",".join("?" for _ in dataset_ids)
            cur.execute(
                f"SELECT d.id, d.name, COUNT(a.id) AS image_count "
                f"FROM datasets d LEFT JOIN alert_images a ON a.dataset_id = d.id "
                f"WHERE d.id IN ({placeholders}) GROUP BY d.id",
                dataset_ids,
            )
            sub = cur.fetchall()
            item["image_count"] = sum(r["image_count"] or 0 for r in sub)
            names_by_id = {r["id"]: r["name"] for r in sub}
            item["dataset_names"] = [names_by_id.get(i) for i in dataset_ids if names_by_id.get(i)]
        rows.append(item)
    page_rows, total = _slice_page(rows, page, page_size)
    return paginated(page_rows, total, page, page_size)


@bp.route("/alerts/eval-sets", methods=["POST"])
def create_eval_set():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        raise ApiError(10500, "评测集名称不能为空", 400)
    dataset_ids = data.get("dataset_ids", [])
    if not isinstance(dataset_ids, list):
        dataset_ids = []
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "INSERT INTO eval_alert_sets (name, notes, dataset_ids) VALUES (?, ?, ?)",
        (name, data.get("notes", ""), json.dumps(dataset_ids)),
    )
    db.commit()
    new_id = cur.lastrowid
    return created(
        {"id": new_id, "name": name, "notes": data.get("notes", ""), "dataset_ids": dataset_ids},
        location=f"/api/v1/alerts/eval-sets/{new_id}",
    )


@bp.route("/alerts/eval-sets/<int:set_id>", methods=["GET"])
def get_eval_set(set_id):
    db = get_db()
    cur = db.cursor()
    cur.execute(f"SELECT {_EVAL_SET_FIELDS} FROM eval_alert_sets WHERE id = ?", (set_id,))
    row = cur.fetchone()
    if not row:
        raise ApiError(20520, "评测集不存在", 404)
    item = dict(row)
    dataset_ids = _parse_id_list(item.get("dataset_ids"))
    item["dataset_ids"] = dataset_ids
    item["dataset_count"] = len(dataset_ids)
    return ok(item)


@bp.route("/alerts/eval-sets/<int:set_id>", methods=["PATCH"])
def update_eval_set(set_id):
    """部分更新元数据；只支持 name 字段（对齐旧版 rename）。不碰 dataset_ids（成员管理专属 :batch-add/:batch-remove）。"""
    data = request.get_json(silent=True) or {}
    if "name" not in data:
        raise ApiError(10510, "没有可更新的字段（name）", 400)
    new_name = (data.get("name") or "").strip()
    if not new_name:
        raise ApiError(10501, "名称不能为空", 400)
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM eval_alert_sets WHERE id = ?", (set_id,))
    if not cur.fetchone():
        raise ApiError(20520, "评测集不存在", 404)
    cur.execute("UPDATE eval_alert_sets SET name = ? WHERE id = ?", (new_name, set_id))
    db.commit()
    cur.execute(f"SELECT {_EVAL_SET_FIELDS} FROM eval_alert_sets WHERE id = ?", (set_id,))
    item = dict(cur.fetchone())
    item["dataset_ids"] = _parse_id_list(item.get("dataset_ids"))
    return ok(item)


@bp.route("/alerts/eval-sets/<int:set_id>", methods=["DELETE"])
def delete_eval_set(set_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM eval_alert_sets WHERE id = ?", (set_id,))
    if not cur.fetchone():
        raise ApiError(20520, "评测集不存在", 404)
    cur.execute("DELETE FROM eval_alert_sets WHERE id = ?", (set_id,))
    db.commit()
    return no_content()


# ── 评测集成员管理 datasets 子集合（增量语义，:batch-add / :batch-remove） ───────

@bp.route("/alerts/eval-sets/<int:set_id>/datasets:batch-add", methods=["POST"])
def batch_add_datasets(set_id):
    """批量加数据集成员（接收 dataset_ids 数组，去重加入）。"""
    data = request.get_json(silent=True) or {}
    dataset_ids = data.get("dataset_ids", [])
    if not dataset_ids:
        raise ApiError(10502, "请选择要添加的数据集", 400)
    db = get_db()
    cur = db.cursor()
    cur.execute(f"SELECT {_EVAL_SET_FIELDS} FROM eval_alert_sets WHERE id = ?", (set_id,))
    row = cur.fetchone()
    if not row:
        raise ApiError(20520, "评测集不存在", 404)
    current_ids = _parse_id_list(row["dataset_ids"])
    added_count = 0
    for did in dataset_ids:
        if did not in current_ids:
            current_ids.append(did)
            added_count += 1
    cur.execute(
        "UPDATE eval_alert_sets SET dataset_ids = ? WHERE id = ?",
        (json.dumps(current_ids), set_id),
    )
    db.commit()
    return ok({"added_count": added_count, "dataset_ids": current_ids})


@bp.route("/alerts/eval-sets/<int:set_id>/datasets:batch-remove", methods=["POST"])
def batch_remove_datasets(set_id):
    """批量移数据集成员（接收 dataset_ids 数组）。"""
    data = request.get_json(silent=True) or {}
    dataset_ids = data.get("dataset_ids", [])
    if not dataset_ids:
        raise ApiError(10503, "请选择要移出的数据集", 400)
    db = get_db()
    cur = db.cursor()
    cur.execute(f"SELECT {_EVAL_SET_FIELDS} FROM eval_alert_sets WHERE id = ?", (set_id,))
    row = cur.fetchone()
    if not row:
        raise ApiError(20520, "评测集不存在", 404)
    current_ids = _parse_id_list(row["dataset_ids"])
    removed_count = 0
    for did in dataset_ids:
        if did in current_ids:
            current_ids.remove(did)
            removed_count += 1
    cur.execute(
        "UPDATE eval_alert_sets SET dataset_ids = ? WHERE id = ?",
        (json.dumps(current_ids), set_id),
    )
    db.commit()
    return ok({"removed_count": removed_count, "dataset_ids": current_ids})
