"""/api/v1/algorithms 资源族端点（算法版本 CRUD + 类型列表 + 下载）。

原位重写 app/routes/algorithms.py 的 8 个旧端点为 /api/v1/algorithms/*，统一信封 +
5 位错误码（FF=06 algorithm-versions，见 docs/rest-api-error-codes.md）。旧逻辑为
同步 CRUD + 文件 I/O，无后台线程/锁/进程（与 OCR 高风险区不同），故原位重写（与
alerts/videos 一致），复用 app.event_types.get_event_types、app.routes.send_file_with_cache、
app.services.config_parser.parse_config，不重复实现。二进制响应（下载）不走信封。

旧端点保留并自动加弃用 header（deprecation.py 的 /algorithms/api/ → /api/v1/algorithms）。

语义修正（新端点专属，旧端点不变）：DELETE→204（对齐 alerts/videos）；冲突 400→409
（30600，5 位码 H 位对齐 http_status）；非法路径 403→400（10605，规范无 403 的 H）。
version_detail 去掉 /detail 后缀（资源 GET 即详情），旧 /versions/<id>/detail 保留并弃用。
"""
import os
import tempfile
import zipfile
from pathlib import Path

from flask import Blueprint, request, current_app, after_this_request
from werkzeug.utils import secure_filename

from app.database import get_db
from app.event_types import get_event_types
from app.routes import send_file_with_cache
from .responses import ok, created, paginated, no_content, ApiError

bp = Blueprint("api_v1_algorithms", __name__, url_prefix="/api/v1")

_VERSION_FIELDS = (
    "id, algorithm_type, name, version_date, description, "
    "config_file_path, algorithm_file_path, created_at"
)


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


def _require_version(cursor, version_id):
    cursor.execute("SELECT id FROM algorithm_versions WHERE id = ?", (version_id,))
    if not cursor.fetchone():
        raise ApiError(20600, "算法版本不存在", 404)


def _version_datasets(cursor, version_id):
    """算法版本关联的启用中数据集（id+name）。"""
    cursor.execute(
        """
        SELECT d.id, d.name
        FROM dataset_algorithm_versions dav
        JOIN datasets d ON d.id = dav.dataset_id
        WHERE dav.algorithm_version_id = ? AND dav.is_active = 1
        """,
        (version_id,),
    )
    return [dict(v) for v in cursor.fetchall()]


# ── 算法类型列表 ───────────────────────────────────────────────────────────────

@bp.route("/algorithms/types", methods=["GET"])
def list_types():
    """所有算法类型 key 列表（= 事件类型 key，来自 get_event_types）。"""
    return ok(get_event_types())


# ── 算法版本 CRUD ───────────────────────────────────────────────────────────────

@bp.route("/algorithms/versions", methods=["GET"])
def list_versions():
    """算法版本列表（每行带关联数据集），支持 ?page/&page_size= 分页。"""
    page, page_size = _parse_pagination()
    db = get_db()
    cur = db.cursor()
    cur.execute(f"SELECT {_VERSION_FIELDS} FROM algorithm_versions ORDER BY created_at DESC")
    rows = []
    for r in cur.fetchall():
        row = dict(r)
        row["datasets"] = _version_datasets(cur, row["id"])
        rows.append(row)
    page_rows, total = _slice_page(rows, page, page_size)
    return paginated(page_rows, total, page, page_size)


@bp.route("/algorithms/versions", methods=["POST"])
def create_version():
    """新增算法版本。multipart：algorithm_type/name/version_date/description 表单字段 +
    config_file/algorithm_file 文件（可选）。algorithm_type 必须在 get_event_types() 内。"""
    algorithm_type = request.form.get("algorithm_type", "").strip()
    name = request.form.get("name", "").strip()
    version_date = request.form.get("version_date", "").strip()
    description = request.form.get("description", "").strip()

    if algorithm_type not in get_event_types():
        raise ApiError(10600, "算法类型无效", 400)
    if not name:
        raise ApiError(10601, "算法名不能为空", 400)
    if not version_date:
        raise ApiError(10602, "算法日期不能为空", 400)

    upload_dir = Path(current_app.config.get("UPLOAD_FOLDER", "uploads")) / "algorithms"
    upload_dir.mkdir(parents=True, exist_ok=True)

    config_file_path = None
    algorithm_file_path = None

    config_file = request.files.get("config_file")
    if config_file and config_file.filename:
        cfg_name = secure_filename(config_file.filename)
        cfg_path = upload_dir / cfg_name
        config_file.save(str(cfg_path))
        config_file_path = str(cfg_path)

    algorithm_file = request.files.get("algorithm_file")
    if algorithm_file and algorithm_file.filename:
        algo_name = secure_filename(algorithm_file.filename)
        algo_path = upload_dir / algo_name
        algorithm_file.save(str(algo_path))
        algorithm_file_path = str(algo_path)

    db = get_db()
    cur = db.cursor()
    cur.execute(
        "INSERT INTO algorithm_versions (algorithm_type, name, version_date, description, "
        "config_file_path, algorithm_file_path) VALUES (?, ?, ?, ?, ?, ?)",
        (algorithm_type, name, version_date, description, config_file_path, algorithm_file_path),
    )
    db.commit()
    version_id = cur.lastrowid
    return created({"id": version_id}, location=f"/api/v1/algorithms/versions/{version_id}")


@bp.route("/algorithms/versions/<int:version_id>", methods=["GET"])
def get_version(version_id):
    """算法版本详情（含关联数据集 + 配置解析）。无 config_file → config_info=None。
    旧端点为 /versions/<id>/detail，此处去掉 /detail（资源 GET 即详情）。"""
    db = get_db()
    cur = db.cursor()
    cur.execute(f"SELECT {_VERSION_FIELDS} FROM algorithm_versions WHERE id = ?", (version_id,))
    row = cur.fetchone()
    if not row:
        raise ApiError(20600, "算法版本不存在", 404)
    version = dict(row)
    datasets = _version_datasets(cur, version_id)
    config_info = None
    if version["config_file_path"]:
        from app.services.config_parser import parse_config
        config_info = parse_config(version["config_file_path"])
    return ok({"version": version, "datasets": datasets, "config_info": config_info})


@bp.route("/algorithms/versions/<int:version_id>", methods=["PATCH"])
def update_version(version_id):
    """部分更新算法版本。multipart：提供哪个字段就更新哪个；config_file/algorithm_file
    提供则覆盖。algorithm_type 提供且非空则必须在 get_event_types() 内。

    description 用**存在性检测**（`if "description" in request.form`）：未传则不更新，
    传空串则显式清空——修正旧版 `request.form.get("description","")` + `is not None` 的
    怪癖（默认 "" 使该判断永真，每次 PATCH 都重写 description，会误清空未传的字段）。
    name/version_date/algorithm_type 维持 truthy 语义（非空才更新，因它们 NOT NULL 不应被空串清空）。
    一个字段都不提供 → 10603。"""
    db = get_db()
    cur = db.cursor()
    _require_version(cur, version_id)

    algorithm_type = request.form.get("algorithm_type", "").strip()
    name = request.form.get("name", "").strip()
    version_date = request.form.get("version_date", "").strip()

    if algorithm_type and algorithm_type not in get_event_types():
        raise ApiError(10600, "算法类型无效", 400)

    upload_dir = Path(current_app.config.get("UPLOAD_FOLDER", "uploads")) / "algorithms"
    upload_dir.mkdir(parents=True, exist_ok=True)

    updates = []
    values = []
    if algorithm_type:
        updates.append("algorithm_type = ?")
        values.append(algorithm_type)
    if name:
        updates.append("name = ?")
        values.append(name)
    if version_date:
        updates.append("version_date = ?")
        values.append(version_date)
    if "description" in request.form:
        updates.append("description = ?")
        values.append(request.form.get("description", "").strip())

    config_file = request.files.get("config_file")
    if config_file and config_file.filename:
        cfg_name = secure_filename(config_file.filename)
        cfg_path = upload_dir / cfg_name
        config_file.save(str(cfg_path))
        updates.append("config_file_path = ?")
        values.append(str(cfg_path))

    algorithm_file = request.files.get("algorithm_file")
    if algorithm_file and algorithm_file.filename:
        algo_name = secure_filename(algorithm_file.filename)
        algo_path = upload_dir / algo_name
        algorithm_file.save(str(algo_path))
        updates.append("algorithm_file_path = ?")
        values.append(str(algo_path))

    if not updates:
        raise ApiError(10603, "没有要更新的字段", 400)

    values.append(version_id)
    cur.execute(
        f"UPDATE algorithm_versions SET {', '.join(updates)} WHERE id = ?",
        values,
    )
    db.commit()
    return ok({"id": version_id})


@bp.route("/algorithms/versions/<int:version_id>", methods=["DELETE"])
def delete_version(version_id):
    """删除算法版本。有数据集正在引用（is_active=1）则拒绝（409，旧版 400）。"""
    db = get_db()
    cur = db.cursor()
    _require_version(cur, version_id)
    cur.execute(
        "SELECT COUNT(*) FROM dataset_algorithm_versions WHERE algorithm_version_id = ? AND is_active = 1",
        (version_id,),
    )
    count = cur.fetchone()[0]
    if count > 0:
        raise ApiError(30600, f"有 {count} 个数据集正在使用此算法版本，无法删除", 409)
    cur.execute("DELETE FROM algorithm_versions WHERE id = ?", (version_id,))
    db.commit()
    return no_content()


# ── 文件下载 ───────────────────────────────────────────────────────────────────

@bp.route("/algorithms/download", methods=["GET"])
def download_file():
    """下载配置/算法文件（?path=）。二进制，不走信封。
    路径经 resolve + relative_to(upload_dir) 安全校验，防穿越（旧用此法，原样保留）。
    非法路径→400（旧 403，规范无 403 的 H）。"""
    file_path_str = request.args.get("path", "")
    if not file_path_str:
        raise ApiError(10604, "缺少 path 参数", 400)

    file_path = Path(file_path_str).resolve()
    upload_dir = Path(current_app.config.get("UPLOAD_FOLDER", "uploads")).resolve()
    try:
        file_path.relative_to(upload_dir)
    except ValueError:
        raise ApiError(10605, "非法路径", 400)

    if not file_path.exists():
        raise ApiError(20601, "文件不存在", 404)

    return send_file_with_cache(str(file_path), as_attachment=True)


@bp.route("/algorithms/versions:batch-download", methods=["POST"])
def batch_download():
    """批量下载算法文件（打包 ZIP）。body: {"ids":[...], "type":"config"|"algorithm"|"all"}。
    二进制，不走信封。旧端点 /algorithms/api/download-batch。"""
    data = request.get_json() or {}
    ids = data.get("ids", [])
    dl_type = data.get("type", "all")

    if not ids:
        raise ApiError(10606, "请选择要下载的版本", 400)

    db = get_db()
    cur = db.cursor()
    placeholders = ",".join("?" * len(ids))
    cur.execute(
        f"SELECT id, name, config_file_path, algorithm_file_path "
        f"FROM algorithm_versions WHERE id IN ({placeholders})",
        ids,
    )
    versions = [dict(r) for r in cur.fetchall()]
    if not versions:
        raise ApiError(10607, "选中的版本不存在", 400)

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".zip")
    os.close(tmp_fd)
    try:
        added = 0
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for v in versions:
                if dl_type in ("config", "all") and v.get("config_file_path"):
                    cfg = Path(v["config_file_path"])
                    if cfg.exists():
                        arcname = f"{v['name']}_config{cfg.suffix}"
                        zf.write(str(cfg), arcname)
                        added += 1
                if dl_type in ("algorithm", "all") and v.get("algorithm_file_path"):
                    algo = Path(v["algorithm_file_path"])
                    if algo.exists():
                        arcname = f"{v['name']}_algo{algo.suffix}"
                        zf.write(str(algo), arcname)
                        added += 1
        if added == 0:
            os.unlink(tmp_path)
            raise ApiError(20602, "没有可下载的文件", 404)

        @after_this_request
        def cleanup(response):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            return response

        type_label = {"config": "configs", "algorithm": "algorithms", "all": "all"}[dl_type]
        return send_file_with_cache(
            tmp_path,
            mimetype="application/zip",
            as_attachment=True,
            download_name=f"algorithms_{type_label}.zip",
        )
    except ApiError:
        raise
    except Exception as e:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise ApiError(40600, f"打包失败: {e}", 500)
