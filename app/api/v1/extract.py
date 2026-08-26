"""/api/v1/extract 资源族端点（视频抽帧任务）。

委托 app/routes/extract.py 的 2 个高风险端点（start/status：起 daemon 线程
`_do_extract_batch` 跑真 ffmpeg/ffprobe + 模块级 `_extract_tasks`/`_extract_lock`
状态），只在新端点套统一信封 + 5 位错误码（FF=11，见 docs/rest-api-error-codes.md）。
旧视图在同一个 request context 内运行，request.get_json/get_db/current_app 可用，
故 start 的请求体由旧视图自读，新端点透传。

纯查询/CRUD（download/delete/list）原位重写，复用 get_db。列表用 SQL 层
LIMIT/OFFSET + COUNT(*) 真分页（`_helpers.paginate`）。

语义修正（新端点专属，旧不动）：DELETE→204；start 成功保 200（对齐 auto-annotation
start / OCR ocr:batch「200 不改 202」先例）。worker `_do_extract_batch` 被
`app.services.assistant_tools` 共享（`from app.routes.extract import _do_extract_batch`），
故委托不改不重测，只验信封/状态码/错误码/委托真触发。

委托边界盲区（bug-audit 另修）：`_fail_task` 死代码（extract.py:319 从无调用）+
`_do_extract_batch` 单帧失败 `except:pass` 后无条件标 status='done' → 失败不可见；
`float(interval_sec)` 传字符串崩 500（旧 L38，v1 不修只标）。
"""
import io
import shutil
import zipfile
from pathlib import Path

from flask import Blueprint, send_file

from app.database import get_db
from app.routes import extract as _legacy
from ._helpers import parse_pagination, paginate, raise_msg
from .compat import call_old_view
from .responses import ok, no_content, ApiError

bp = Blueprint("api_v1_extract", __name__, url_prefix="/api/v1")

# 服务端错误兜底码（FF=11 SS=80 段）
_EXTRACT_FALLBACK = (41100, 500, "操作失败")

# start 的 error 文案 → (5 位码, http_status)
_START_MSG_CODE = {
    "缺少 wm_ids 列表": (11100, 400),
    "抽帧间隔必须大于0": (11101, 400),
    "均不可抽帧": (11102, 400),  # "选中的视频均不可抽帧（未设video_id或文件不存在）"
    "水印视频不存在": (21100, 404),
}


@bp.route("/extract/tasks", methods=["POST"])
def create_task():
    """提交批量抽帧任务（委托旧 start_extract：校验→建库→起线程）。
    请求体 {wm_ids, target_width?, interval_sec?, include_normal?} 由旧视图自读。成功 200。"""
    body, status = call_old_view(_legacy.start_extract)
    if status == 200:
        return ok({"task_id": body.get("task_id"), "video_count": body.get("video_count")})
    raise_msg(body, _START_MSG_CODE, fallback=_EXTRACT_FALLBACK)


@bp.route("/extract/tasks/<int:task_id>/status", methods=["GET"])
def extract_status(task_id):
    """查询抽帧进度（委托旧 extract_status：先读模块态 _extract_tasks，miss 回退 DB）。
    任务不存在→404(21101)。成功 200。"""
    body, status = call_old_view(_legacy.extract_status, task_id)
    if status == 200:
        return ok({
            "status": body.get("status"),
            "done": body.get("done"),
            "total": body.get("total"),
            "frame_count": body.get("frame_count"),
            "video_count": body.get("video_count"),
            "output_dir": body.get("output_dir"),
            "error": body.get("error"),
        })
    raise_msg(body, {"任务不存在": (21101, 404)}, fallback=_EXTRACT_FALLBACK)


@bp.route("/extract/tasks", methods=["GET"])
def list_tasks():
    """历史抽帧任务列表（真分页，对齐旧 list_tasks 字段）。"""
    page, page_size = parse_pagination()
    base = """
        SELECT id, video_id, video_count, target_width, interval_sec, include_normal,
               status, frame_count, created_at
        FROM extracted_frames_tasks
    """
    return paginate(get_db(), base, "ORDER BY created_at DESC", (), page, page_size, dict)


@bp.route("/extract/tasks/<int:task_id>/download", methods=["GET"])
def download_frames(task_id):
    """打包下载帧 zip（二进制，不走信封）。任务不存在→404(21101)；帧目录不存在→404(21102)。"""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT video_id, output_dir, frame_count FROM extracted_frames_tasks WHERE id = ?", (task_id,))
    row = cur.fetchone()
    if not row:
        raise ApiError(21101, "任务不存在", 404)
    output_dir = Path(row["output_dir"])
    if not output_dir.exists():
        raise ApiError(21102, "帧目录不存在", 404)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(output_dir.iterdir()):
            if f.is_file():
                zf.write(f, f.name)
    buf.seek(0)
    name = row["video_id"].split(",")[0] if row["video_id"] else "frames"
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"{name}_frames.zip")


@bp.route("/extract/tasks/<int:task_id>", methods=["DELETE"])
def delete_extract(task_id):
    """删除抽帧任务及帧目录。任务不存在→404(21101)；成功→204。"""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT output_dir FROM extracted_frames_tasks WHERE id = ?", (task_id,))
    row = cur.fetchone()
    if not row:
        raise ApiError(21101, "任务不存在", 404)
    output_dir = Path(row["output_dir"])
    if output_dir.exists():
        shutil.rmtree(output_dir, ignore_errors=True)
    cur.execute("DELETE FROM extracted_frames_tasks WHERE id = ?", (task_id,))
    db.commit()
    return no_content()
