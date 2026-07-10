"""算法版本管理路由

管理算法版本：增删改查 + 文件上传 + 配置解析 + 批量下载
"""

import os
import tempfile
import zipfile
from pathlib import Path

from flask import Blueprint, request, jsonify, render_template, current_app, after_this_request
from werkzeug.utils import secure_filename

from app.database import get_db
from app.routes import send_file_with_cache
from app.utils import allowed_file
from app.event_types import get_event_types

bp = Blueprint("algorithms", __name__, url_prefix="/algorithms")

EVENT_TYPES = get_event_types()


# ── 页面路由 ────────────────────────────────────────────────────────────────

@bp.route("/")
def algorithms_page():
    return render_template("algorithms.html")


# ── API 路由 ────────────────────────────────────────────────────────────────

@bp.route("/api/types")
def list_types():
    """获取所有算法类型列表"""
    return jsonify(EVENT_TYPES)


@bp.route("/api/versions", methods=["GET"])
def list_versions():
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT id, algorithm_type, name, version_date, description, "
        "config_file_path, algorithm_file_path, created_at "
        "FROM algorithm_versions ORDER BY created_at DESC"
    )
    rows = [dict(r) for r in cur.fetchall()]
    # 加载每个版本关联的数据集
    for row in rows:
        cur.execute(
            """
            SELECT d.id, d.name
            FROM dataset_algorithm_versions dav
            JOIN datasets d ON d.id = dav.dataset_id
            WHERE dav.algorithm_version_id = ? AND dav.is_active = 1
            """,
            (row["id"],),
        )
        row["datasets"] = [dict(v) for v in cur.fetchall()]
    return jsonify(rows)


@bp.route("/api/versions", methods=["POST"])
def create_version():
    algorithm_type = request.form.get("algorithm_type", "").strip()
    name = request.form.get("name", "").strip()
    version_date = request.form.get("version_date", "").strip()
    description = request.form.get("description", "").strip()

    if algorithm_type not in EVENT_TYPES:
        return jsonify({"error": "算法类型无效"}), 400
    if not name:
        return jsonify({"error": "算法名不能为空"}), 400
    if not version_date:
        return jsonify({"error": "算法日期不能为空"}), 400

    # 文件上传处理
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

    return jsonify({"id": version_id}), 201


@bp.route("/api/versions/<int:version_id>", methods=["PATCH"])
def update_version(version_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM algorithm_versions WHERE id = ?", (version_id,))
    if not cur.fetchone():
        return jsonify({"error": "算法版本不存在"}), 404

    algorithm_type = request.form.get("algorithm_type", "").strip()
    name = request.form.get("name", "").strip()
    version_date = request.form.get("version_date", "").strip()
    description = request.form.get("description", "").strip()

    if algorithm_type and algorithm_type not in EVENT_TYPES:
        return jsonify({"error": "算法类型无效"}), 400

    # 文件上传处理
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
    if description is not None:
        updates.append("description = ?")
        values.append(description)

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
        return jsonify({"error": "没有要更新的字段"}), 400

    values.append(version_id)
    cur.execute(
        f"UPDATE algorithm_versions SET {', '.join(updates)} WHERE id = ?",
        values,
    )
    db.commit()
    return jsonify({"id": version_id})


@bp.route("/api/versions/<int:version_id>", methods=["DELETE"])
def delete_version(version_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM algorithm_versions WHERE id = ?", (version_id,))
    if not cur.fetchone():
        return jsonify({"error": "算法版本不存在"}), 404

    # 检查是否有数据集正在引用此版本
    cur.execute(
        "SELECT COUNT(*) FROM dataset_algorithm_versions WHERE algorithm_version_id = ? AND is_active = 1",
        (version_id,),
    )
    count = cur.fetchone()[0]
    if count > 0:
        return jsonify({"error": f"有 {count} 个数据集正在使用此算法版本，无法删除"}), 400

    cur.execute("DELETE FROM algorithm_versions WHERE id = ?", (version_id,))
    db.commit()
    return jsonify({"ok": True})


# ── 文件下载 ────────────────────────────────────────────────────────────────

@bp.route("/api/download")
def download_file():
    """下载配置文件或算法文件"""
    file_path_str = request.args.get("path", "")
    if not file_path_str:
        return jsonify({"error": "缺少 path 参数"}), 400

    file_path = Path(file_path_str).resolve()
    upload_dir = Path(current_app.config.get("UPLOAD_FOLDER", "uploads")).resolve()

    # 安全检查：确保文件在 uploads 目录下
    if not str(file_path).startswith(str(upload_dir)):
        return jsonify({"error": "非法路径"}), 403

    if not file_path.exists():
        return jsonify({"error": "文件不存在"}), 404

    return send_file_with_cache(str(file_path), as_attachment=True)


@bp.route("/api/versions/<int:version_id>/detail")
def version_detail(version_id):
    """获取算法版本详情（含配置解析 + 关联数据集）"""
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT id, algorithm_type, name, version_date, description, "
        "config_file_path, algorithm_file_path, created_at "
        "FROM algorithm_versions WHERE id = ?",
        (version_id,),
    )
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "算法版本不存在"}), 404

    version = dict(row)

    # 关联数据集
    cur.execute(
        """
        SELECT d.id, d.name
        FROM dataset_algorithm_versions dav
        JOIN datasets d ON d.id = dav.dataset_id
        WHERE dav.algorithm_version_id = ? AND dav.is_active = 1
        """,
        (version_id,),
    )
    datasets = [dict(v) for v in cur.fetchall()]

    # 配置解析
    config_info = None
    if version["config_file_path"]:
        from app.services.config_parser import parse_config

        config_info = parse_config(version["config_file_path"])

    return jsonify({"version": version, "datasets": datasets, "config_info": config_info})


@bp.route("/api/download-batch", methods=["POST"])
def batch_download():
    """批量下载算法文件（打包为 ZIP）

    Body: {"ids": [1,2,3], "type": "config" | "algorithm" | "all"}
    """
    data = request.get_json() or {}
    ids = data.get("ids", [])
    dl_type = data.get("type", "all")

    if not ids:
        return jsonify({"error": "请选择要下载的版本"}), 400

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
        return jsonify({"error": "选中的版本不存在"}), 400

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
            return jsonify({"error": "没有可下载的文件"}), 404

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
    except Exception as e:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return jsonify({"error": f"打包失败: {e}"}), 500
