"""统一响应信封构造器与 API 错误异常。

所有 /api/v1/* 端点统一返回：
  成功 {"code":0,"message":"ok","data":{...}}
  错误 {"code":<int≠0>,"message":"...","errors":[...]?}

业务错误码 5 位 = H FF SS（H=HTTP 类，FF=资源族，SS=族内；详见 docs/rest-api-error-codes.md）：
  0       成功
  1xxxx  客户端请求错误（HTTP 400/405）
  2xxxx  资源不存在（HTTP 404）
  3xxxx  状态冲突（HTTP 409）
  4xxxx  服务端错误（HTTP 500）
  5xxxx  异步任务失败（HTTP 500/202）
"""
from flask import jsonify


def ok(data=None, message="ok"):
    """成功响应：{"code":0,"message":"ok","data":<data>}"""
    return jsonify({"code": 0, "message": message, "data": data}), 200


def created(data=None, location=None, message="created"):
    """创建成功：201，可选 Location header 指向新资源。"""
    resp = jsonify({"code": 0, "message": message, "data": data})
    resp.status_code = 201
    if location:
        resp.headers["Location"] = location
    return resp


def accepted(data=None, location=None, message="accepted"):
    """异步任务已接受：202 + Location 指向资源 GET。"""
    resp = jsonify({"code": 0, "message": message, "data": data})
    resp.status_code = 202
    if location:
        resp.headers["Location"] = location
    return resp


def no_content():
    """无内容响应：204，无 body。"""
    return "", 204


def paginated(items, total, page, page_size):
    """分页列表响应：data:{items,total,page,page_size,has_next}。"""
    has_next = page * page_size < total
    return ok({
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_next": has_next,
    })


class ApiError(Exception):
    """API 业务错误异常，由 errors.py 的 errorhandler 转为统一错误信封。

    新端点内 raise ApiError(code=2004, message='视频不存在', http_status=404)，
    旧端点不 raise，故不受影响。

    Attributes:
        code: 业务错误码（非 HTTP status）。
        message: 人类可读描述。
        http_status: 对应 HTTP 状态码。
        errors: 可选字段级错误列表 [{"field","reason"}]。
    """

    def __init__(self, code, message, http_status=400, errors=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.errors = errors
