"""app 级错误处理，仅对 /api/v1/ 命名空间返回统一错误信封。

Flask 的 404/405 errorhandler 是 app 级全局的，无法只挂在蓝图上。
本模块注册 app 级 errorhandler，handler 内按 request.path 是否以
/api/v1/ 开头分流：是→返回统一错误信封；否→return e 回退 Flask 默认
行为（HTML），不破坏旧端点与页面。

旧端点用 `return jsonify({'error': ...}), <code>` 主动返回（不 raise、
不 abort），errorhandler 只在抛异常/路由未命中时触发，故旧端点错误
格式不受影响。
"""
from flask import jsonify, request
from werkzeug.exceptions import HTTPException

from .responses import ApiError


def _is_api_v1():
    return request.path.startswith("/api/v1/")


def _envelope_error(code, message, http_status, errors=None):
    body = {"code": code, "message": message}
    if errors:
        body["errors"] = errors
    return jsonify(body), http_status


def register_error_handlers(app):
    """在 app 上注册 /api/v1/ 作用域的错误处理器。"""

    @app.errorhandler(ApiError)
    def handle_api_error(e):
        # ApiError 只在新端点内 raise，直接转信封
        return _envelope_error(e.code, e.message, e.http_status, e.errors)

    @app.errorhandler(400)
    def handle_400(e):
        if not _is_api_v1():
            return e  # 回退默认
        return _envelope_error(10000, "请求参数错误：{}".format(e.description), 400)

    @app.errorhandler(404)
    def handle_404(e):
        if not _is_api_v1():
            return e  # 回退默认 HTML 404
        return _envelope_error(20000, "资源不存在", 404)

    @app.errorhandler(405)
    def handle_405(e):
        if not _is_api_v1():
            return e
        return _envelope_error(10005, "HTTP 方法不被允许", 405)

    @app.errorhandler(409)
    def handle_409(e):
        if not _is_api_v1():
            return e
        return _envelope_error(30000, "状态冲突：{}".format(e.description), 409)

    @app.errorhandler(500)
    def handle_500(e):
        if not _is_api_v1():
            return e
        return _envelope_error(40000, "服务器内部错误", 500)

    @app.errorhandler(Exception)
    def handle_unexpected(e):
        # HTTPException 交给更具体的 handler（更具体的优先）
        if isinstance(e, HTTPException):
            return e
        if not _is_api_v1():
            return e  # 旧命名空间：回退默认 500
        app.logger.exception("Unhandled error in /api/v1")
        return _envelope_error(40000, "服务器内部错误", 500)
