"""/api/v1 各资源模块共享的小工具：分页解析、SQL 真分页、旧视图错误映射。

供 extract/review/assistant/evaluation 等原位重写 + 委托端点复用。
（auto_annotation/streaming 建于本模块之前，各自有本地副本，暂不回改。）
"""
from flask import request

from .responses import paginated, ApiError


def parse_pagination():
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


def paginate(db, base_sql, order_sql, params, page, page_size, mapper=dict):
    """真分页：COUNT(*) 取 total，LIMIT/OFFSET 取当页，mapper 映射每行。
    base_sql 不含 ORDER BY / LIMIT（COUNT 子查询与 items 查询共用）。"""
    cur = db.cursor()
    cur.execute(f"SELECT COUNT(*) FROM ({base_sql}) _c", params)
    total = cur.fetchone()[0]
    offset = (page - 1) * page_size
    cur.execute(f"{base_sql} {order_sql} LIMIT ? OFFSET ?",
                (*params, page_size, offset))
    return paginated([mapper(r) for r in cur.fetchall()], total, page, page_size)


def raise_msg(body, msg_to_code, fallback=(40000, 500, "操作失败")):
    """旧视图非 200：按 error 文案子串匹配 (code, http_status)，无匹配走 fallback。
    fallback 默认通用 40000/500（资源族可在调用处传自己的服务端码，如 extract 41100）。"""
    msg = (body.get("error") if isinstance(body, dict) else None) or fallback[2]
    for key, (code, http_status) in msg_to_code.items():
        if key in msg:
            raise ApiError(code, msg, http_status)
    raise ApiError(fallback[0], msg, fallback[1])
