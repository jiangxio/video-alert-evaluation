"""/api/v1/extract 资源族端点（视频抽帧任务）。

委托 app/routes/extract.py 的 2 个高风险端点（start/status：起 daemon 线程
`_do_extract_batch` 跑真 ffmpeg/ffprobe + 模块级 `_extract_tasks`/`_extract_lock`
状态），只在新端点套统一信封 + 方案3 error_code（code = HTTP 状态）。旧视图在
同一个 request context 内运行，故 start 的请求体由旧视图自读，新端点透传。

纯查询/CRUD（download/delete/list）原位重写，复用 get_db。列表用 SQL 层
LIMIT/OFFSET + COUNT(*) 真分页（ok(paginate(...))）。

语义保持（新端点专属，旧不动）：DELETE→204；start 成功保 200（对齐 auto-annotation
start / OCR ocr:batch「200 不改 202」先例）。worker `_do_extract_batch` 被
`app.services.assistant_tools` 共享（`from app.routes.extract import _do_extract_batch`），
故委托不改不重测，只验信封/状态码/委托真触发。

委托边界盲区（bug-audit 另修）：`_fail_task` 死代码 + `_do_extract_batch` 单帧失败
`except:pass` 后无条件标 status='done' → 失败不可见；`float(interval_sec)` 传字符串
崩 500（旧 L38，v1 不修只标）。
"""
import io
import shutil
import zipfile
from pathlib import Path

from flask import request, send_file

from app.api.v1 import v1_bp
from app.api.v1.compat import _extract, _extract_message
from app.api.v1.responses import err, no_content, ok, paginate, parse_pagination
from app.database import get_db
from app.routes import extract as _legacy


@v1_bp.route("/extract/tasks", methods=["POST"])
def v1_create_extract_task():
    """提交批量抽帧任务（委托旧 start_extract：校验→建库→起线程）。
    请求体 {wm_ids, target_width?, interval_sec?, include_normal?} 由旧视图自读。成功 200。"""
    data, status = _extract(_legacy.start_extract())
    if status == 200:
        return ok({"task_id": data.get("task_id"), "video_count": data.get("video_count")})
    return err(status, _extract_message(data))


@v1_bp.route("/extract/tasks/<int:task_id>/status", methods=["GET"])
def v1_extract_status(task_id):
    """查询抽帧进度（委托旧 extract_status：先读模块态 _extract_tasks，miss 回退 DB）。
    任务不存在→404。成功 200。"""
    data, status = _extract(_legacy.extract_status(task_id))
    if status == 200:
        return ok({
            "status": data.get("status"),
            "done": data.get("done"),
            "total": data.get("total"),
            "frame_count": data.get("frame_count"),
            "video_count": data.get("video_count"),
            "output_dir": data.get("output_dir"),
            "error": data.get("error"),
        })
    return err(status, _extract_message(data))


@v1_bp.route("/extract/tasks", methods=["GET"])
def v1_list_extract_tasks():
    """历史抽帧任务列表（真分页，对齐旧 list_tasks 字段）。"""
    page, page_size = parse_pagination(request.args)
    db = get_db()
    cur = db.cursor()
    base = (
        "SELECT id, video_id, video_count, target_width, interval_sec, include_normal, "
        "status, frame_count, created_at FROM extracted_frames_tasks"
    )
    cur.execute(f"SELECT COUNT(*) FROM ({base}) _c")
    total = cur.fetchone()[0]
    cur.execute(f"{base} ORDER BY created_at DESC LIMIT ? OFFSET ?", (page_size, (page - 1) * page_size))
    items = [dict(r) for r in cur.fetchall()]
    return ok(paginate(items, total, page, page_size))


@v1_bp.route("/extract/tasks/<int:task_id>/download", methods=["GET"])
def v1_download_frames(task_id):
    """打包下载帧 zip（二进制，不走信封）。任务不存在→404；帧目录不存在→404。"""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT video_id, output_dir, frame_count FROM extracted_frames_tasks WHERE id = ?", (task_id,))
    row = cur.fetchone()
    if not row:
        return err(404, "任务不存在", error_code="EXTRACT_TASK_NOT_FOUND")
    output_dir = Path(row["output_dir"])
    if not output_dir.exists():
        return err(404, "帧目录不存在", error_code="EXTRACT_FRAMES_NOT_FOUND")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(output_dir.iterdir()):
            if f.is_file():
                zf.write(f, f.name)
    buf.seek(0)
    name = row["video_id"].split(",")[0] if row["video_id"] else "frames"
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"{name}_frames.zip")


@v1_bp.route("/extract/tasks/<int:task_id>", methods=["DELETE"])
def v1_delete_extract(task_id):
    """删除抽帧任务及帧目录。任务不存在→404；成功→204。"""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT output_dir FROM extracted_frames_tasks WHERE id = ?", (task_id,))
    row = cur.fetchone()
    if not row:
        return err(404, "任务不存在", error_code="EXTRACT_TASK_NOT_FOUND")
    output_dir = Path(row["output_dir"])
    if output_dir.exists():
        shutil.rmtree(output_dir, ignore_errors=True)
    cur.execute("DELETE FROM extracted_frames_tasks WHERE id = ?", (task_id,))
    db.commit()
    return no_content()
