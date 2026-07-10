"""算法版本管理路由

管理算法版本：增删改查 + 文件上传 + 配置解析 + 批量下载
"""

import json
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


# ── 页面路由 ────────────────────────────────────────────────────────────────

@bp.route("/")
def algorithms_page():
    return render_template("algorithms.html")


@bp.route("/types")
def event_types_page():
    return render_template("event_types.html")


# ── API 路由 ────────────────────────────────────────────────────────────────

@bp.route("/api/types")
def list_types():
    """获取所有算法类型列表"""
    return jsonify(get_event_types())


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

    if algorithm_type not in get_event_types():
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

    if algorithm_type and algorithm_type not in get_event_types():
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


# ── 事件类型（算法类型）管理 API ─────────────────────────────────────────────

@bp.route("/api/event-types", methods=["GET"])
def list_event_types():
    """获取所有事件类型详情（含中文名、描述、颜色、标签）"""
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT id, key, name, description, bg_color, fg_color, tags, sort_order "
        "FROM event_types ORDER BY sort_order, id"
    )
    rows = [dict(r) for r in cur.fetchall()]
    for row in rows:
        try:
            row["tags"] = json.loads(row["tags"] or "[]")
        except Exception:
            row["tags"] = []
    return jsonify(rows)


@bp.route("/api/event-types", methods=["POST"])
def create_event_type():
    """新增事件类型"""
    data = request.get_json() or {}
    key = data.get("key", "").strip()
    name = data.get("name", "").strip()
    description = data.get("description", "").strip()
    bg_color = data.get("bg_color", "#e0e0e0").strip() or "#e0e0e0"
    fg_color = data.get("fg_color", "#333333").strip() or "#333333"
    tags = data.get("tags", [])
    et_id = data.get("id")

    if not key:
        return jsonify({"error": "英文标识不能为空"}), 400
    if not name:
        return jsonify({"error": "中文名不能为空"}), 400
    if not all(c.isalnum() or c == "_" for c in key):
        return jsonify({"error": "英文标识只能包含字母、数字和下划线"}), 400

    if not isinstance(tags, list):
        return jsonify({"error": "标签必须是数组"}), 400
    tags = [str(t).strip() for t in tags if str(t).strip()]

    db = get_db()
    cur = db.cursor()

    # 检查 key 是否已存在
    cur.execute("SELECT id FROM event_types WHERE key = ?", (key,))
    if cur.fetchone():
        return jsonify({"error": f"英文标识 '{key}' 已存在"}), 409

    # 确定 id
    if et_id is None:
        cur.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM event_types")
        et_id = cur.fetchone()[0]
    else:
        try:
            et_id = int(et_id)
        except ValueError:
            return jsonify({"error": "ID 必须是整数"}), 400
        cur.execute("SELECT id FROM event_types WHERE id = ?", (et_id,))
        if cur.fetchone():
            return jsonify({"error": f"ID {et_id} 已存在"}), 409

    cur.execute(
        "INSERT INTO event_types (id, key, name, description, bg_color, fg_color, tags) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (et_id, key, name, description, bg_color, fg_color, json.dumps(tags, ensure_ascii=False)),
    )
    db.commit()

    from app.event_types import _sync_alert_types_json
    _sync_alert_types_json()

    return jsonify({"id": et_id, "key": key}), 201


@bp.route("/api/event-types/<int:et_id>", methods=["PATCH"])
def update_event_type(et_id):
    """修改事件类型（不允许修改英文标识 key）"""
    data = request.get_json() or {}
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM event_types WHERE id = ?", (et_id,))
    if not cur.fetchone():
        return jsonify({"error": "事件类型不存在"}), 404

    allowed_fields = {
        "name": ("name", str),
        "description": ("description", str),
        "bg_color": ("bg_color", str),
        "fg_color": ("fg_color", str),
        "sort_order": ("sort_order", int),
    }
    updates = []
    values = []

    for field, (col, converter) in allowed_fields.items():
        if field in data:
            value = data[field]
            if converter == str:
                value = str(value).strip()
            else:
                try:
                    value = converter(value)
                except (ValueError, TypeError):
                    return jsonify({"error": f"字段 {field} 格式错误"}), 400
            updates.append(f"{col} = ?")
            values.append(value)

    if "tags" in data:
        tags = data["tags"]
        if not isinstance(tags, list):
            return jsonify({"error": "标签必须是数组"}), 400
        tags = [str(t).strip() for t in tags if str(t).strip()]
        updates.append("tags = ?")
        values.append(json.dumps(tags, ensure_ascii=False))

    if not updates:
        return jsonify({"error": "没有要更新的字段"}), 400

    values.append(et_id)
    cur.execute(
        f"UPDATE event_types SET {', '.join(updates)} WHERE id = ?",
        values,
    )
    db.commit()

    from app.event_types import _sync_alert_types_json
    _sync_alert_types_json()

    return jsonify({"id": et_id})


@bp.route("/api/event-types/<int:et_id>/references", methods=["GET"])
def get_event_type_references(et_id):
    """获取事件类型被引用的情况"""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT key FROM event_types WHERE id = ?", (et_id,))
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "事件类型不存在"}), 404
    key = row["key"]

    refs = {}
    tables = [
        ("algorithm_versions", "algorithm_type"),
        ("events", "event_type"),
        ("auto_annotation_tasks", "event_type"),
        ("eval_merged_events", "event_type"),
        ("eval_gt_events", "event_type"),
    ]
    for table, col in tables:
        try:
            cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} = ?", (key,))
            refs[table] = cur.fetchone()[0]
        except Exception:
            refs[table] = 0

    total = sum(refs.values())
    return jsonify({"key": key, "total": total, "refs": refs})


@bp.route("/api/event-types/<int:et_id>", methods=["DELETE"])
def delete_event_type(et_id):
    """删除事件类型（有被引用时禁止删除）"""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT key FROM event_types WHERE id = ?", (et_id,))
    row = cur.fetchone()
    if not row:
        return jsonify({"error": "事件类型不存在"}), 404
    key = row["key"]

    # 检查引用
    tables = [
        ("algorithm_versions", "algorithm_type"),
        ("events", "event_type"),
        ("auto_annotation_tasks", "event_type"),
        ("eval_merged_events", "event_type"),
        ("eval_gt_events", "event_type"),
    ]
    total = 0
    for table, col in tables:
        try:
            cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} = ?", (key,))
            total += cur.fetchone()[0]
        except Exception:
            pass

    if total > 0:
        return jsonify({"error": f"事件类型 '{key}' 仍有 {total} 处引用，无法删除"}), 400

    cur.execute("DELETE FROM event_types WHERE id = ?", (et_id,))
    db.commit()

    from app.event_types import _sync_alert_types_json
    _sync_alert_types_json()

    return jsonify({"ok": True})
