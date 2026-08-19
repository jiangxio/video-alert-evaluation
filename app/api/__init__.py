"""REST API v1 命名空间注册入口。

在 create_app() 末尾调用 register_api(app)：注册 v1 蓝图 + 资源模块 +
全局 errorhandler（errors.py，按 path 分流）+ 弃用钩子（deprecation.py，旧 API 加头）。
L5b golden 基线已采集，盯防全局钩子对旧 API 响应的误伤。
"""


def register_api(app):
    """注册 /api/v1/* 蓝图及其资源模块、全局 errorhandler、弃用钩子。"""
    from app.api.v1 import v1_bp
    from app.api.v1 import videos  # noqa: F401  导入即注册 v1 videos 路由
    from app.api.v1 import alerts  # noqa: F401  导入即注册 v1 alerts 路由
    from app.api.v1 import alerts_ocr  # noqa: F401  导入即注册 v1 alerts OCR 路由

    app.register_blueprint(v1_bp)

    from app.api.v1.errors import register_error_handlers
    from app.api.v1.deprecation import register_deprecation

    register_error_handlers(app)
    register_deprecation(app)
