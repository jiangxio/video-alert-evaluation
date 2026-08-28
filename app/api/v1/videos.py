"""/api/v1/videos 资源族端点。

origin 委托模式（v1_bp + wrap_old_view + paginate_old_list）覆盖 5 个基础端点
（list/upload/delete/download/eval-sets GET+POST）；fork 超集端点
（GET 单条/PATCH/watermarked/eval-sets PUT/DELETE）原位重写，复用 fork legacy + get_db。
二进制响应（下载）不走信封。
"""
import json
import os
import re
from pathlib import Path

from flask import current_app, request

from app.api.v1 import v1_bp
from app.api.v1.compat import _extract, paginate_old_list, wrap_old_view
from app.api.v1.responses import ApiError, err, no_content, ok, paginate, parse_pagination
from app.database import get_db
from app.routes import send_file_with_cache
from app.routes.videos import (
    create_eval_set,
    delete_video,
    download_video,
    extract_video_id,
    get_video_duration,
    get_video_resolution,
    list_all_videos,
    list_eval_sets,
    search_videos,
    upload_video,
)
from app.utils import allowed_file, safe_filename

# 预包装旧视图（CRUD/二进制），避免每次请求重复构造
_upload = wrap_old_view(upload_video)
_delete = wrap_old_view(delete_video)
_download = wrap_old_view(download_video)
_create_eval_set = wrap_old_view(create_eval_set)


# ── fork 超集端点辅助 ─────────────────────────────────────────────────────────

_VIDEO_FIELDS = (
    "id, filename, original_path, video_id, file_size, duration, "
    "created_at, updated_at, video_id_confirmed"
)
_WM_FIELDS = (
    "id, original_video_id, filename, output_path, file_size, created_at, "
    "thumbnail_path, resolution, duration, ocr_check_status"
)


def _fetch_video(cursor, video_id):
    cursor.execute(f"SELECT {_VIDEO_FIELDS} FROM videos WHERE id = ?", (video_id,))
    return cursor.fetchone()


def _require_video(cursor, video_id):
    video = _fetch_video(cursor, video_id)
    if not video:
        raise ApiError(404, "视频不存在", error_code="VIDEO_NOT_FOUND")
    return video


def _video_with_watermark(cursor, video):
    """组装单个视频 dict，附带 has_watermark 标记与最新水印版本。"""
    v = dict(video)
    cursor.execute(
        f"SELECT {_WM_FIELDS} FROM watermarked_videos "
        "WHERE original_video_id = ? ORDER BY created_at DESC LIMIT 1",
        (v["id"],),
    )
    wm = cursor.fetchone()
    v["has_watermark"] = wm is not None
    if wm:
        v["watermarked"] = dict(wm)
    return v


def _slice_page(rows, page, page_size):
    """对已取出的列表做内存分页，返回 (page_rows, total)。"""
    total = len(rows)
    start = (page - 1) * page_size
    return rows[start:start + page_size], total


# ── 基础端点（origin 委托模式） ─────────────────────────────────────────────────

@v1_bp.route("/videos", methods=["GET"])
def v1_list_videos():
    """视频列表，支持 ?q= 过滤，返回分页信封。"""
    q = request.args.get("q", "").strip()
    return paginate_old_list(lambda: search_videos() if q else list_all_videos())


@v1_bp.route("/videos", methods=["POST"])
def v1_upload_video():
    return _upload()


@v1_bp.route("/videos/<int:video_id>", methods=["DELETE"])
def v1_delete_video(video_id):
    return _delete(video_id)


@v1_bp.route("/videos/<int:video_id>/download", methods=["GET"])
def v1_download_video(video_id):
    return _download(video_id)


@v1_bp.route("/videos/eval-sets", methods=["GET"])
def v1_list_video_eval_sets():
    return paginate_old_list(list_eval_sets, list_key="sets")


@v1_bp.route("/videos/eval-sets", methods=["POST"])
def v1_create_eval_set():
    return _create_eval_set()


# ── fork 超集端点（原位重写） ───────────────────────────────────────────────────

@v1_bp.route("/videos/<int:video_id>", methods=["GET"])
def v1_get_video(video_id):
    """单视频详情（REST 补全）。调旧 list 按 id 过滤，复用富化逻辑。"""
    data, _ = _extract(list_all_videos())
    items = data if isinstance(data, list) else []
    for item in items:
        if item.get("id") == video_id:
            return ok(item)
    raise ApiError(404, "视频不存在", error_code="VIDEO_NOT_FOUND")


@v1_bp.route("/videos/<int:video_id>", methods=["PATCH"])
def v1_update_video(video_id):
    """部分更新：body 含 filename→重命名；含 video_id→设置视频 ID（10 位数字）。"""
    data = request.get_json(silent=True) or {}
    db = get_db()
    cur = db.cursor()
    video = _require_video(cur, video_id)

    touched = False
    if "filename" in data:
        new_filename = (data.get("filename") or "").strip()
        if not new_filename:
            return err(400, "文件名不能为空")
        new_filename = safe_filename(new_filename)
        if not new_filename:
            return err(400, "非法文件名")
        if not allowed_file(new_filename, current_app.config["ALLOWED_VIDEO_EXTENSIONS"]):
            return err(400, "不支持的文件格式")
        cur.execute("SELECT id FROM videos WHERE filename = ? AND id != ?", (new_filename, video_id))
        if cur.fetchone():
            return err(409, f'文件名 "{new_filename}" 已被其他视频使用', error_code="VIDEO_FILENAME_EXISTS")
        old_path = Path(video["original_path"])
        new_path = old_path.parent / new_filename
        try:
            os.rename(str(old_path), str(new_path))
        except OSError as e:
            return err(500, f"文件重命名失败: {e}", error_code="VIDEO_RENAME_FAILED")
        cur.execute(
            "UPDATE videos SET filename = ?, original_path = ? WHERE id = ?",
            (new_filename, str(new_path), video_id),
        )
        cur.execute(
            "UPDATE watermarked_videos SET filename = ?, output_path = ? "
            "WHERE original_video_id = ? AND output_path = ?",
            (new_filename, str(new_path), video_id, str(old_path)),
        )
        touched = True

    if "video_id" in data:
        new_vid = (data.get("video_id") or "").strip()
        if not new_vid:
            return err(400, "video_id 不能为空")
        if not re.fullmatch(r"\d{10}", new_vid):
            return err(400, "video_id 必须为恰好10位数字")
        cur.execute("SELECT id FROM videos WHERE video_id = ? AND id != ?", (new_vid, video_id))
        if cur.fetchone():
            return err(400, "该 video_id 已被其他视频使用，请使用不同的 ID", error_code="VIDEO_ID_EXISTS")
        cur.execute("UPDATE videos SET video_id = ? WHERE id = ?", (new_vid, video_id))
        touched = True

    if not touched:
        return err(400, "没有可更新的字段（filename 或 video_id）")

    db.commit()
    video = _fetch_video(cur, video_id)
    return ok(_video_with_watermark(cur, video))


@v1_bp.route("/videos/watermarked", methods=["GET"])
def v1_list_watermarked():
    """打水印视频列表，支持 ?eval_set_id= 筛选、?q= 搜索、分页。"""
    page, page_size = parse_pagination(request.args)
    eval_set_id = request.args.get("eval_set_id")
    q = request.args.get("q", "").strip()
    filter_video_ids = None

    db = get_db()
    cur = db.cursor()
    if eval_set_id:
        cur.execute("SELECT video_ids FROM eval_video_sets WHERE id = ?", (eval_set_id,))
        row = cur.fetchone()
        if row and row["video_ids"]:
            try:
                filter_video_ids = json.loads(row["video_ids"])
            except Exception:
                filter_video_ids = []
        else:
            filter_video_ids = []

    where_conditions = ""
    query_params = []
    if q:
        where_conditions = "AND (v.video_id LIKE ? OR w.filename LIKE ?)"
        like = f"%{q}%"
        query_params.extend([like, like])

    cur.execute(f"""
        SELECT
            w.id as wm_id, w.filename as wm_filename, w.output_path, w.file_size as wm_file_size,
            w.thumbnail_path, w.resolution, w.duration as wm_duration, w.ocr_check_status,
            v.id as video_id, v.video_id as vid, v.duration as orig_duration,
            GROUP_CONCAT(DISTINCT e.event_type) as event_types
        FROM watermarked_videos w
        JOIN videos v ON v.id = w.original_video_id
        LEFT JOIN events e ON e.video_db_id = v.id
        WHERE w.id IN (
            SELECT MAX(w2.id) FROM watermarked_videos w2 GROUP BY w2.original_video_id
        )
        {where_conditions}
        GROUP BY w.id
        ORDER BY w.created_at DESC
    """, query_params)
    rows = cur.fetchall()

    # 评测集筛选在内存侧做（JSON 数组难以在 SQL 内联过滤）
    if filter_video_ids is not None:
        rows = [r for r in rows if r["video_id"] in filter_video_ids]

    page_rows, total = _slice_page(rows, page, page_size)
    items = []
    for row in page_rows:
        vid = row["vid"]
        has_gt_json = False
        if vid:
            has_gt_json = (Path(current_app.config["GROUND_TRUTH_DIR"]) / f"{vid}.json").exists()
        items.append({
            "id": row["wm_id"],
            "video_db_id": row["video_id"],
            "video_id": row["vid"],
            "filename": row["wm_filename"],
            "output_path": row["output_path"],
            "file_size": row["wm_file_size"],
            "thumbnail_path": row["thumbnail_path"],
            "resolution": row["resolution"],
            "duration": row["wm_duration"] or row["orig_duration"],
            "event_types": row["event_types"].split(",") if row["event_types"] else [],
            "has_ground_truth": has_gt_json,
            "ocr_check_status": row["ocr_check_status"],
        })
    return ok(paginate(items, total, page, page_size))


@v1_bp.route("/videos/eval-sets/<int:set_id>", methods=["PUT"])
def v1_replace_eval_set(set_id):
    """整体替换评测集元数据；body 含 video_ids 时一并替换成员列表。"""
    data = request.get_json(silent=True) or {}
    new_name = (data.get("name") or "").strip()
    if not new_name:
        return err(400, "名称不能为空")
    new_notes = (data.get("notes") or "").strip()

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM eval_video_sets WHERE id = ?", (set_id,))
    if not cur.fetchone():
        return err(404, "评测集不存在", error_code="VIDEO_EVAL_SET_NOT_FOUND")

    if "video_ids" in data:
        video_ids = data.get("video_ids") or []
        cur.execute(
            "UPDATE eval_video_sets SET name = ?, notes = ?, video_ids = ? WHERE id = ?",
            (new_name, new_notes, json.dumps(video_ids), set_id),
        )
        result_vids = video_ids
    else:
        cur.execute(
            "UPDATE eval_video_sets SET name = ?, notes = ? WHERE id = ?",
            (new_name, new_notes, set_id),
        )
        cur.execute("SELECT video_ids FROM eval_video_sets WHERE id = ?", (set_id,))
        r = cur.fetchone()
        try:
            result_vids = json.loads(r["video_ids"]) if r and r["video_ids"] else []
        except Exception:
            result_vids = []
    db.commit()
    return ok({"id": set_id, "name": new_name, "notes": new_notes, "video_ids": result_vids})


@v1_bp.route("/videos/eval-sets/<int:set_id>", methods=["DELETE"])
def v1_delete_eval_set(set_id):
    """删除评测集。不存在→404；成功→204。"""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM eval_video_sets WHERE id = ?", (set_id,))
    if not cur.fetchone():
        return err(404, "评测集不存在", error_code="VIDEO_EVAL_SET_NOT_FOUND")
    cur.execute("DELETE FROM eval_video_sets WHERE id = ?", (set_id,))
    db.commit()
    return no_content()
