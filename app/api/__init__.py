"""REST API 包入口。

register_api(app) 在 app/__init__.py 的 create_app() 末尾调用一次：
注册 /api/v1 命名空间各资源蓝图 + app 级错误处理器 + 旧端点弃用钩子。
"""
from .v1 import BLUEPRINTS
from .v1.errors import register_error_handlers
from .v1.deprecation import register_deprecation


def register_api(app):
    """在 app 上注册 /api/v1 REST API 及其横切关注点。"""
    for bp in BLUEPRINTS:
        app.register_blueprint(bp)
    register_error_handlers(app)
    register_deprecation(app)
