"""统一响应信封与分页工具（/api/v1/* 全模块复用，定一次）。

约定：
- 成功：HTTP 200/201/202，body {"code":0,"message":"ok|created|accepted","data":...}
- 错误：HTTP <code>，body {"code":<code>,"message":"...","errors":[...]?}
- 二进制响应（下载/缩略图/日志）不走信封，由端点直接 send_file。
- 列表分页：data = {items,total,page,page_size,has_next}
"""
from flask import jsonify

PAGE_SIZE_DEFAULT = 20
PAGE_SIZE_MAX = 100


def ok(data=None):
    """成功信封，HTTP 200。"""
    return jsonify({"code": 0, "message": "ok", "data": data})


def created(data=None, location=None):
    """创建成功，HTTP 201。可选 Location 头指向新资源。"""
    resp = jsonify({"code": 0, "message": "created", "data": data})
    resp.status_code = 201
    if location:
        resp.headers["Location"] = location
    return resp


def accepted(location=None):
    """异步动作已受理，HTTP 202。可选 Location 头指向状态查询端点。"""
    resp = jsonify({"code": 0, "message": "accepted", "data": None})
    resp.status_code = 202
    if location:
        resp.headers["Location"] = location
    return resp


def no_content():
    """无内容，HTTP 204（DELETE/PUT 空返回）。"""
    return ("", 204)


def err(code, message, errors=None, error_code=None):
    """错误信封，HTTP 状态 = code。

    错误码方案（方案3）：code 始终是标准 HTTP 状态码（语义层）；需精确区分同类
    错误时用 error_code 传业务码（字符串，如 "DATASET_NOT_FOUND"）。可选、不传
    则不出现该键——与旧的两参/三参调用完全向后兼容。
    """
    payload = {"code": code, "message": message}
    if error_code is not None:
        payload["error_code"] = error_code
    if errors is not None:
        payload["errors"] = errors
    return jsonify(payload), code


class ApiError(Exception):
    """端点内主动抛出，由 app 级 errorhandler 转成错误信封。

    error_code 可选，传则进入错误信封的 error_code 键（方案3 业务码）。
    """

    def __init__(self, code, message, errors=None, error_code=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.errors = errors
        self.error_code = error_code


def parse_pagination(args):
    """从 query 参数解析分页：?page=1&page_size=20。非法值容错到默认。"""
    page = _safe_int(args.get("page"), 1)
    page_size = _safe_int(args.get("page_size"), PAGE_SIZE_DEFAULT)
    page = max(1, page)
    page_size = max(1, min(page_size, PAGE_SIZE_MAX))
    return page, page_size


def paginate(items, total, page, page_size):
    """构造列表信封的 data 部分。"""
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_next": page * page_size < total,
    }


def _safe_int(value, default):
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def pick_fields(data, allowed):
    """PATCH 字段白名单：返回 (已知字段 dict, 未知字段名列表)。

    用于单字段更新端点拒绝未开放字段——未知字段非空时端点应返
    err(400, ..., error_code="UNKNOWN_FIELD")。
    """
    data = data or {}
    known = {k: v for k, v in data.items() if k in allowed}
    unknown = [k for k in data if k not in allowed]
    return known, unknown


def reject_unknown_fields(data, allowed):
    """PATCH 字段白名单便捷包装：有未知字段返 400 UNKNOWN_FIELD 错误信封，否则 None。

    端点用法：`resp = reject_unknown_fields(data, {"mode"}); if resp: return resp`。
    """
    _, unknown = pick_fields(data, allowed)
    if unknown:
        return err(400, f"不支持的字段: {unknown}", error_code="UNKNOWN_FIELD")
    return None
