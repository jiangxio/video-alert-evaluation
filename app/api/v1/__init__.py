"""REST API v1 命名空间（/api/v1/*）。

register_api(app) 在 create_app 末尾调用：注册 v1 蓝图 + 资源模块 +
全局 errorhandler（errors.py）+ 弃用钩子（deprecation.py）。L5b golden 基线已就位，
可盯防全局钩子对旧 API 响应的误伤。
"""
from flask import Blueprint

v1_bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")
