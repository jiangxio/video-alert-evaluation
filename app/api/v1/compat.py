"""旧视图委托层。

新 /api/v1 端点对高风险旧视图（后台线程 / 锁 / 独立 sqlite 连接等 CLAUDE.md
警告区逻辑）只委托不改：在同一个 Flask request context 内直接调用旧视图函数，
拆其 `(jsonify(...)[, code])` 返回为 `(body_dict, status)`，由各新端点定制套信封。

旧视图在请求上下文内运行，request / get_db / current_app 均可用，无需额外注入。
仅适用于返回 jsonify 的视图；返回 send_file 的二进制端点不走本层（各端点直接复用）。
"""


def call_old_view(old_func, *args, **kwargs):
    """调用旧视图函数，返回 (body_dict, status)。

    旧视图返回形式：`jsonify({...})` 或 `jsonify({...}), <code>`。
    jsonify() 产生 flask.Response，支持 .get_json() 取回 body dict。
    """
    result = old_func(*args, **kwargs)
    if isinstance(result, tuple):
        resp, status = result[0], result[1]
    else:
        resp, status = result, 200
    return resp.get_json(), status
