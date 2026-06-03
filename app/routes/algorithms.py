"""算法版本管理路由

管理算法版本：增删改查 + 文件上传
"""

import json
import os
from pathlib import Path

from flask import Blueprint, request, jsonify, render_template, current_app
from werkzeug.utils import secure_filename

from app.database import get_db, DATABASE_PATH

bp = Blueprint("algorithms", __name__, url_prefix="/algorithms")

EVENT_TYPES = ["rat", "smoke", "use_phone", "call_phone", "chef", "trash", "mask", "flame"]


def allowed_file(filename, allowed_extensions):
    """检查文件扩展名"""
    return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed_extensions


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
