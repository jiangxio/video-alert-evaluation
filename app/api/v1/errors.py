"""app 级 errorhandler：按 request.path 分流，/api/v1/* 返信封，其余回退默认。

注册于 register_api(app)（create_app 末尾调用），不碰 create_app 本体、不动旧 413 handler。

分流规则（对应 docs/rest-api-feasibility-and-test-plan.md Layer 3）：
- 404：v1 路径 → 错误信封；其余 → return e 复刻 Flask 默认 HTML 404（页面原行为不变）
- 500：v1 路径 → 错误信封（防 HTML 500 泄漏给 API 客户端）；其余 → return e 回退默认
- ApiError：端点内主动抛出（responses.ApiError），转错误信封，让 v1 端点可 raise 替代 return err()
"""
from flask import request

from app.api.v1.responses import ApiError, err


def register_error_handlers(app):
    """注册 v1 分流 errorhandler。纯增量，不覆盖现有 413 handler。"""

    @app.errorhandler(404)
    def handle_404(e):
        if request.path.startswith("/api/v1/"):
            return err(404, "资源不存在")
        return e  # 复刻 Flask 默认 HTML 404，保持页面 404 原行为

    @app.errorhandler(500)
    def handle_500(e):
        if request.path.startswith("/api/v1/"):
            app.logger.exception("v1 内部错误")
            return err(500, "服务器内部错误")
        return e

    @app.errorhandler(ApiError)
    def handle_api_error(e):
        return err(e.code, e.message, e.errors, e.error_code)
