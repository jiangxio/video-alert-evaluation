"""/api/v1/videos 资源族端点。

视频列表/上传/详情/删除/部分更新/下载、打水印视频列表、评测视频集 CRUD。
逻辑与旧 app/routes/videos.py 对齐，但返回统一信封并修正 HTTP 动词；
纯工具函数（extract_video_id / get_video_resolution / get_video_duration）
复用旧模块，不重复实现。二进制响应（下载）不走信封。
"""
import json
import os
import re
from pathlib import Path

from flask import Blueprint, request, current_app

from app.database import get_db
from app.routes import send_file_with_cache
from app.routes.videos import (
    extract_video_id,
    get_video_resolution,
    get_video_duration,
)
from app.utils import allowed_file, safe_filename
from .responses import ok, created, paginated, no_content, ApiError

bp = Blueprint("api_v1_videos", __name__, url_prefix="/api/v1")

_VIDEO_FIELDS = (
    "id, filename, original_path, video_id, file_size, duration, "
    "created_at, updated_at, video_id_confirmed"
)
_WM_FIELDS = (
    "id, original_video_id, filename, output_path, file_size, created_at, "
    "thumbnail_path, resolution, duration, ocr_check_status"
)


def _parse_pagination():
    """解析 ?page & ?page_size，做边界与上限约束（page≥1，page_size 1..100，默认 20）。"""
    try:
        page = max(1, int(request.args.get("page", "1")))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(request.args.get("page_size", "20"))
    except (TypeError, ValueError):
        page_size = 20
    page_size = max(1, min(page_size, 100))
    return page, page_size


def _fetch_video(cursor, video_id):
    cursor.execute(f"SELECT {_VIDEO_FIELDS} FROM videos WHERE id = ?", (video_id,))
    return cursor.fetchone()


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


# ── 视频列表/上传 ─────────────────────────────────────────────────────────────

@bp.route("/videos", methods=["GET"])
def list_videos():
    """视频列表，支持 ?q= 搜索、?page/&page_size= 分页。"""
    page, page_size = _parse_pagination()
    q = request.args.get("q", "").strip()
    db = get_db()
    cur = db.cursor()
    if q:
        like = f"%{q}%"
        cur.execute(
            f"SELECT {_VIDEO_FIELDS} FROM videos "
            "WHERE filename LIKE ? OR video_id LIKE ? ORDER BY created_at DESC",
            (like, like),
        )
    else:
        cur.execute(f"SELECT {_VIDEO_FIELDS} FROM videos ORDER BY created_at DESC")
    rows = cur.fetchall()
    page_rows, total = _slice_page(rows, page, page_size)
    items = [_video_with_watermark(cur, r) for r in page_rows]
    return paginated(items, total, page, page_size)


@bp.route("/videos", methods=["POST"])
def upload_video():
    """上传视频。multipart 字段名 video；可选表单 already_watermarked。"""
    if "video" not in request.files:
        raise ApiError(10100, "没有上传文件", 400)
    file = request.files["video"]
    if file.filename == "":
        raise ApiError(10101, "没有选择文件", 400)
    if not allowed_file(file.filename, current_app.config["ALLOWED_VIDEO_EXTENSIONS"]):
        raise ApiError(10102, "不支持的文件格式", 400)
    filename = safe_filename(file.filename)
    if not filename:
        raise ApiError(10103, "非法文件名", 400)

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM videos WHERE filename = ?", (filename,))
    if cur.fetchone():
        raise ApiError(30140, f'文件 "{filename}" 已存在，请先删除或重命名后再上传', 409)

    save_path = Path(current_app.config["UPLOAD_VIDEOS"]) / filename
    file.save(str(save_path))

    video_id = extract_video_id(filename)
    file_size = save_path.stat().st_size
    duration = get_video_duration(str(save_path))
    already_watermarked = request.form.get("already_watermarked") in ("1", "true", "on")
    video_id_confirmed = 1 if already_watermarked and video_id else 0

    cur.execute(
        "INSERT INTO videos (filename, original_path, video_id, file_size, duration, video_id_confirmed) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (filename, str(save_path), video_id, file_size, duration, video_id_confirmed),
    )
    video_db_id = cur.lastrowid

    if already_watermarked:
        resolution = get_video_resolution(save_path)
        cur.execute(
            "INSERT INTO watermarked_videos (original_video_id, filename, output_path, file_size, resolution, duration) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (video_db_id, filename, str(save_path), file_size, resolution, duration),
        )
    db.commit()

    data = {
        "id": video_db_id,
        "filename": filename,
        "video_id": video_id,
        "has_watermark": already_watermarked,
        "duration": duration,
    }
    return created(data, location=f"/api/v1/videos/{video_db_id}")


# ── 打水印视频列表 ────────────────────────────────────────────────────────────

@bp.route("/videos/watermarked", methods=["GET"])
def list_watermarked():
    """打水印视频列表，支持 ?eval_set_id= 筛选、?q= 搜索、分页。"""
    page, page_size = _parse_pagination()
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
    return paginated(items, total, page, page_size)


# ── 单视频：详情/删除/部分更新/下载 ─────────────────────────────────────────────

@bp.route("/videos/<int:video_id>", methods=["GET"])
def get_video(video_id):
    db = get_db()
    cur = db.cursor()
    video = _fetch_video(cur, video_id)
    if not video:
        raise ApiError(20120, "视频不存在", 404)
    return ok(_video_with_watermark(cur, video))


@bp.route("/videos/<int:video_id>", methods=["DELETE"])
def delete_video(video_id):
    db = get_db()
    cur = db.cursor()
    video = _fetch_video(cur, video_id)
    if not video:
        raise ApiError(20120, "视频不存在", 404)

    try:
        Path(video["original_path"]).unlink(missing_ok=True)
    except Exception:
        pass
    cur.execute("SELECT output_path FROM watermarked_videos WHERE original_video_id = ?", (video_id,))
    for row in cur.fetchall():
        try:
            Path(row["output_path"]).unlink(missing_ok=True)
        except Exception:
            pass
    cur.execute("DELETE FROM videos WHERE id = ?", (video_id,))
    db.commit()
    return no_content()


@bp.route("/videos/<int:video_id>", methods=["PATCH"])
def update_video(video_id):
    """部分更新：body 含 filename→重命名；含 video_id→设置视频 ID（10 位数字）。"""
    data = request.get_json(silent=True) or {}
    db = get_db()
    cur = db.cursor()
    video = _fetch_video(cur, video_id)
    if not video:
        raise ApiError(20120, "视频不存在", 404)

    touched = False
    if "filename" in data:
        new_filename = (data.get("filename") or "").strip()
        if not new_filename:
            raise ApiError(10104, "文件名不能为空", 400)
        new_filename = safe_filename(new_filename)
        if not new_filename:
            raise ApiError(10105, "非法文件名", 400)
        if not allowed_file(new_filename, current_app.config["ALLOWED_VIDEO_EXTENSIONS"]):
            raise ApiError(10106, "不支持的文件格式", 400)
        cur.execute("SELECT id FROM videos WHERE filename = ? AND id != ?", (new_filename, video_id))
        if cur.fetchone():
            raise ApiError(30141, f'文件名 "{new_filename}" 已被其他视频使用', 409)
        old_path = Path(video["original_path"])
        new_path = old_path.parent / new_filename
        try:
            os.rename(str(old_path), str(new_path))
        except OSError as e:
            raise ApiError(40180, f"文件重命名失败: {e}", 500)
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
            raise ApiError(10107, "video_id 不能为空", 400)
        if not re.fullmatch(r"\d{10}", new_vid):
            raise ApiError(10108, "video_id 必须为恰好10位数字", 400)
        cur.execute("SELECT id FROM videos WHERE video_id = ? AND id != ?", (new_vid, video_id))
        if cur.fetchone():
            raise ApiError(10109, "该 video_id 已被其他视频使用，请使用不同的 ID", 400)
        cur.execute("UPDATE videos SET video_id = ? WHERE id = ?", (new_vid, video_id))
        touched = True

    if not touched:
        raise ApiError(10110, "没有可更新的字段（filename 或 video_id）", 400)

    db.commit()
    video = _fetch_video(cur, video_id)
    return ok(_video_with_watermark(cur, video))


@bp.route("/videos/<int:video_id>/download", methods=["GET"])
def download_video(video_id):
    """下载或播放视频。?type=original（默认）|watermarked；?inline=true 内联播放。"""
    download_type = request.args.get("type", "original")
    inline = request.args.get("inline", "false").lower() == "true"
    db = get_db()
    cur = db.cursor()
    video = _fetch_video(cur, video_id)
    if not video:
        raise ApiError(20120, "视频不存在", 404)

    if download_type == "watermarked":
        cur.execute(
            "SELECT output_path, filename FROM watermarked_videos "
            "WHERE original_video_id = ? ORDER BY created_at DESC LIMIT 1",
            (video_id,),
        )
        row = cur.fetchone()
        if not row:
            raise ApiError(20121, "尚未生成水印视频", 404)
        file_path = Path(row["output_path"])
        download_name = row["filename"]
    else:
        file_path = Path(video["original_path"])
        download_name = video["filename"]

    if not file_path.exists():
        raise ApiError(20122, "文件不存在于磁盘", 404)

    if inline:
        return send_file_with_cache(str(file_path), mimetype="video/mp4")
    return send_file_with_cache(str(file_path), as_attachment=True, download_name=download_name)


# ── 评测视频集 CRUD ────────────────────────────────────────────────────────────

@bp.route("/videos/eval-sets", methods=["GET"])
def list_eval_sets():
    page, page_size = _parse_pagination()
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id, name, notes, video_ids, created_at FROM eval_video_sets ORDER BY created_at DESC")
    rows = cur.fetchall()
    page_rows, total = _slice_page(rows, page, page_size)
    items = []
    for s in page_rows:
        s_dict = dict(s)
        if s_dict.get("video_ids"):
            try:
                s_dict["video_ids"] = json.loads(s_dict["video_ids"])
            except Exception:
                s_dict["video_ids"] = []
        else:
            s_dict["video_ids"] = []
        items.append(s_dict)
    return paginated(items, total, page, page_size)


@bp.route("/videos/eval-sets", methods=["POST"])
def create_eval_set():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        raise ApiError(10111, "评测集名称不能为空", 400)
    video_ids = data.get("video_ids", [])
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "INSERT INTO eval_video_sets (name, notes, video_ids) VALUES (?, ?, ?)",
        (name, data.get("notes", ""), json.dumps(video_ids)),
    )
    db.commit()
    new_id = cur.lastrowid
    return created(
        {"id": new_id, "name": name, "notes": data.get("notes", ""), "video_ids": video_ids},
        location=f"/api/v1/videos/eval-sets/{new_id}",
    )


@bp.route("/videos/eval-sets/<int:set_id>", methods=["PUT"])
def replace_eval_set(set_id):
    """整体替换评测集元数据；body 含 video_ids 时一并替换成员列表。"""
    data = request.get_json(silent=True) or {}
    new_name = (data.get("name") or "").strip()
    if not new_name:
        raise ApiError(10112, "名称不能为空", 400)
    new_notes = (data.get("notes") or "").strip()

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM eval_video_sets WHERE id = ?", (set_id,))
    if not cur.fetchone():
        raise ApiError(20123, "评测集不存在", 404)

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


@bp.route("/videos/eval-sets/<int:set_id>", methods=["DELETE"])
def delete_eval_set(set_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM eval_video_sets WHERE id = ?", (set_id,))
    if not cur.fetchone():
        raise ApiError(20123, "评测集不存在", 404)
    cur.execute("DELETE FROM eval_video_sets WHERE id = ?", (set_id,))
    db.commit()
    return no_content()
