"""把旧视图函数包装成 /api/v1/* 信封端点的兼容层。

策略（对应 docs/rest-api-feasibility-and-test-plan.md 的 L1 矩阵）：
- JSON 成功响应 → 信封 {code:0, data:<旧body>}，保留原 HTTP 状态（200/201/202）
- JSON 错误响应 (jsonify({'error':...}), 4xx/5xx) → 错误信封 {code:status, message:...}
- 二进制（send_file，非 json content-type）/ 重定向（302）/ 页面 HTML → 原样透传
- None / (None, 204) → 204 无内容

重要约束：列表/分页端点不能靠包装补分页（total/has_next 需重查），
必须在新端点里用 paginate() 重实现。包装器只把列表原样放进 data。
"""
from functools import wraps

from flask import Response, make_response

from app.api.v1.responses import accepted, created, err, no_content, ok


def wrap_old_view(old_view):
    """返回一个包装视图：调用旧视图，把响应转成 v1 信封。"""

    @wraps(old_view)
    def wrapper(*args, **kwargs):
        return _envelope_response(old_view(*args, **kwargs))

    return wrapper


def _split_rv(rv):
    """把视图返回值拆成 (body, status, headers)，status 默认 200。

    支持 Flask 四种返回形态：rv / (rv, code) / (rv, code, headers) / (rv, headers)。
    """
    status, headers = 200, None
    if isinstance(rv, tuple):
        if len(rv) == 1:
            rv = rv[0]
        elif len(rv) == 2:
            # (body, headers) 还是 (body, status)？headers 是 dict/list，status 是 int/str
            if isinstance(rv[1], (dict, list, tuple)):
                rv, headers = rv
            else:
                rv, status = rv
        elif len(rv) >= 3:
            rv, status, headers = rv[0], rv[1], rv[2]
    return rv, status, headers


def _extract_message(data):
    """从旧错误 body 提取 message（兼容 error / message 字段）。"""
    if isinstance(data, dict):
        return data.get("error") or data.get("message") or "error"
    return "error"


def _success_envelope(data, status):
    """按状态码选对应的成功信封。"""
    if status == 201:
        return created(data)
    if status == 202:
        return accepted()
    return ok(data)


def _envelope_response(result):
    body, status, _headers = _split_rv(result)

    # 1) 旧视图直接返回 dict（Flask 会自动 jsonify，但包装层手动处理）
    if isinstance(body, dict):
        if status >= 400:
            return err(status, _extract_message(body))
        if status == 204:
            return no_content()
        return _success_envelope(body, status)

    # 2) Response 对象：jsonify / send_file / redirect / make_response
    if isinstance(body, Response):
        ctype = body.content_type or ""
        if not ctype.startswith("application/json"):
            return body  # 二进制/重定向/页面 → 原样透传
        # jsonify 的真实状态在 Response 上（外层 tuple 没给状态时取它）
        status = status if status != 200 else body.status_code
        data = body.get_json(silent=True)
        if status >= 400:
            return err(status, _extract_message(data))
        if status == 204 or data is None:
            return no_content()
        return _success_envelope(data, status)

    # 3) None / 空返回
    if body is None:
        return no_content()

    # 4) 字符串/其他：交给 make_response，按 content_type 决定透传还是套信封
    resp = make_response(body, status)
    ctype = resp.content_type or ""
    if ctype.startswith("application/json"):
        data = resp.get_json(silent=True)
        if status >= 400:
            return err(status, _extract_message(data))
        return _success_envelope(data, status)
    return resp  # 非 JSON（页面 HTML 等）透传


def paginate_old_list(old_call, list_key=None):
    """调用旧列表视图：成功 → v1 内存分页信封；错误 → 错误信封。

    用于裸 list（旧视图 jsonify([...])）或 {list_key: [...]} 形态的列表端点。
    wrap_old_view 补不回 total/has_next，列表端点必须走本函数重实现分页。

    - old_call: 无参可调用，返回旧视图响应（Response / tuple / dict）
    - list_key: None=裸 list；否则从 dict 的该键取列表
    - 旧视图返 4xx/5xx（如数据集不存在的 404）→ 透传为错误信封，不误当空分页
    """
    from flask import request

    from app.api.v1.responses import err, ok, paginate, parse_pagination

    body, status, _ = _split_rv(old_call())
    # 取 JSON：body 可能是 Response（jsonify）或裸 dict/list
    if isinstance(body, Response):
        data = body.get_json(silent=True)
        if status == 200:
            status = body.status_code
    else:
        data = body

    if status >= 400:
        return err(status, _extract_message(data))

    if isinstance(data, list):
        items = data
    elif isinstance(data, dict) and list_key is not None:
        items = data.get(list_key, []) or []
    else:
        items = []

    page, page_size = parse_pagination(request.args)
    total = len(items)
    start = (page - 1) * page_size
    page_items = items[start:start + page_size]
    return ok(paginate(page_items, total, page, page_size))
