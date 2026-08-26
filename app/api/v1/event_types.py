"""/api/v1/event-types 资源族端点（事件类型 CRUD + 引用计数）。

原位重写 app/routes/algorithms.py 的 5 个事件类型旧端点为 /api/v1/event-types/*，
统一信封 + 5 位错误码（FF=07 event-types，见 docs/rest-api-error-codes.md）。旧端点
为 /algorithms/api/event-types，保留并自动加弃用 header。

create/update/delete 后调 _sync_alert_types_json() 保持 config/alert_types.json 与 DB
同步（测试期由 conftest 把 ALERT_TYPES_CONFIG_PATH 重定向到 tmp，不碰真实配置）。

语义修正（新端点专属，旧端点不变）：DELETE→204；有引用无法删除 400→409（30702）。
"""
import json

from flask import Blueprint, request

from app.database import get_db
from app.event_types import _sync_alert_types_json
from .responses import ok, created, paginated, no_content, ApiError

bp = Blueprint("api_v1_event_types", __name__, url_prefix="/api/v1")

_ET_FIELDS = "id, key, name, description, bg_color, fg_color, tags, sort_order"

# 事件类型被引用的表（按 key 匹配）。references 查询与 delete 校验共用。
_REF_TABLES = [
    ("algorithm_versions", "algorithm_type"),
    ("events", "event_type"),
    ("auto_annotation_tasks", "event_type"),
    ("eval_merged_events", "event_type"),
    ("eval_gt_events", "event_type"),
]


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


def _require_et(cursor, et_id):
    cursor.execute("SELECT id FROM event_types WHERE id = ?", (et_id,))
    if not cursor.fetchone():
        raise ApiError(20700, "事件类型不存在", 404)


@bp.route("/event-types", methods=["GET"])
def list_event_types():
    """所有事件类型详情（含中文名、描述、颜色、标签），支持 ?page/&page_size= 分页。
    tags 字段已 json.loads 为数组。"""
    page, page_size = _parse_pagination()
    db = get_db()
    cur = db.cursor()
    cur.execute(f"SELECT {_ET_FIELDS} FROM event_types ORDER BY sort_order, id")
    rows = []
    for r in cur.fetchall():
        row = dict(r)
        try:
            row["tags"] = json.loads(row["tags"] or "[]")
        except Exception:
            row["tags"] = []
        rows.append(row)
    page_rows, total = _slice_page(rows, page, page_size)
    return paginated(page_rows, total, page, page_size)


@bp.route("/event-types", methods=["POST"])
def create_event_type():
    """新增事件类型。body: {key, name, description?, bg_color?, fg_color?, tags?, id?}。
    key 只含字母/数字/下划线；id 缺省 COALESCE(MAX(id),0)+1 或显式（校验整数+唯一）。
    create 后 _sync_alert_types_json()。"""
    data = request.get_json() or {}
    key = data.get("key", "").strip()
    name = data.get("name", "").strip()
    description = data.get("description", "").strip()
    bg_color = data.get("bg_color", "#e0e0e0").strip() or "#e0e0e0"
    fg_color = data.get("fg_color", "#333333").strip() or "#333333"
    tags = data.get("tags", [])
    et_id = data.get("id")

    if not key:
        raise ApiError(10700, "英文标识不能为空", 400)
    if not name:
        raise ApiError(10701, "中文名不能为空", 400)
    if not all(c.isalnum() or c == "_" for c in key):
        raise ApiError(10702, "英文标识只能包含字母、数字和下划线", 400)

    if not isinstance(tags, list):
        raise ApiError(10703, "标签必须是数组", 400)
    tags = [str(t).strip() for t in tags if str(t).strip()]

    db = get_db()
    cur = db.cursor()

    # 检查 key 是否已存在
    cur.execute("SELECT id FROM event_types WHERE key = ?", (key,))
    if cur.fetchone():
        raise ApiError(30700, f"英文标识 '{key}' 已存在", 409)

    # 确定 id
    if et_id is None:
        cur.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM event_types")
        et_id = cur.fetchone()[0]
    else:
        try:
            et_id = int(et_id)
        except (ValueError, TypeError):
            raise ApiError(10704, "ID 必须是整数", 400)
        cur.execute("SELECT id FROM event_types WHERE id = ?", (et_id,))
        if cur.fetchone():
            raise ApiError(30701, f"ID {et_id} 已存在", 409)

    cur.execute(
        "INSERT INTO event_types (id, key, name, description, bg_color, fg_color, tags) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (et_id, key, name, description, bg_color, fg_color, json.dumps(tags, ensure_ascii=False)),
    )
    db.commit()
    _sync_alert_types_json()
    return created({"id": et_id, "key": key}, location=f"/api/v1/event-types/{et_id}")


@bp.route("/event-types/<int:et_id>", methods=["PATCH"])
def update_event_type(et_id):
    """修改事件类型（不允许修改英文标识 key）。body 可含 name/description/bg_color/
    fg_color/sort_order/tags。空更新→10706。update 后 _sync。"""
    data = request.get_json() or {}
    db = get_db()
    cur = db.cursor()
    _require_et(cur, et_id)

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
                    raise ApiError(10705, f"字段 {field} 格式错误", 400)
            updates.append(f"{col} = ?")
            values.append(value)

    if "tags" in data:
        tags = data["tags"]
        if not isinstance(tags, list):
            raise ApiError(10703, "标签必须是数组", 400)
        tags = [str(t).strip() for t in tags if str(t).strip()]
        updates.append("tags = ?")
        values.append(json.dumps(tags, ensure_ascii=False))

    if not updates:
        raise ApiError(10706, "没有要更新的字段", 400)

    values.append(et_id)
    cur.execute(
        f"UPDATE event_types SET {', '.join(updates)} WHERE id = ?",
        values,
    )
    db.commit()
    _sync_alert_types_json()
    return ok({"id": et_id})


@bp.route("/event-types/<int:et_id>/references", methods=["GET"])
def get_event_type_references(et_id):
    """事件类型被引用的情况：跨 5 表按 key 计数 + 总数。"""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT key FROM event_types WHERE id = ?", (et_id,))
    row = cur.fetchone()
    if not row:
        raise ApiError(20700, "事件类型不存在", 404)
    key = row["key"]

    refs = {}
    for table, col in _REF_TABLES:
        try:
            cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} = ?", (key,))
            refs[table] = cur.fetchone()[0]
        except Exception:
            refs[table] = 0

    total = sum(refs.values())
    return ok({"key": key, "total": total, "refs": refs})


@bp.route("/event-types/<int:et_id>", methods=["DELETE"])
def delete_event_type(et_id):
    """删除事件类型。有被引用则拒绝（409，旧版 400）。delete 后 _sync。"""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT key FROM event_types WHERE id = ?", (et_id,))
    row = cur.fetchone()
    if not row:
        raise ApiError(20700, "事件类型不存在", 404)
    key = row["key"]

    # 检查引用
    total = 0
    for table, col in _REF_TABLES:
        try:
            cur.execute(f"SELECT COUNT(*) FROM {table} WHERE {col} = ?", (key,))
            total += cur.fetchone()[0]
        except Exception:
            pass

    if total > 0:
        raise ApiError(30702, f"事件类型 '{key}' 仍有 {total} 处引用，无法删除", 409)

    cur.execute("DELETE FROM event_types WHERE id = ?", (et_id,))
    db.commit()
    _sync_alert_types_json()
    return no_content()
