"""/api/v1/config 资源端点（统一 API Token 配置：获取/保存/测试连接）。

原位重写 app/routes/api_config.py 的 3 个 JSON 端点为 /api/v1/config/*，统一信封 +
5 位错误码（FF=08 config，见 docs/rest-api-error-codes.md）。旧端点为
/api-config/api/config|save|test，保留并自动加弃用 header（deprecation.py 已预置
/api-config/api/ → /api/v1/config）。

config 是单例资源（api_config 表 CHECK(id=1)，全局唯一）。保存用 PATCH：save_config
为部分更新（存在性检测 + 密钥空串=不改 + 未传字段不动），PUT 的整体替换语义与之
矛盾。测试连接是 RPC 动作，用 :test-connection 后缀；连接失败是测试结果
（200 + {ok:false}），仅 provider 未知/缺失才是 400。

复用 app.services.api_config_service 的纯函数，不改 service、不改旧路由。
"""
from flask import Blueprint, request

from app.services import api_config_service

from .responses import ok, ApiError

bp = Blueprint("api_v1_config", __name__, url_prefix="/api/v1")


@bp.route("/config", methods=["GET"])
def get_config():
    """获取当前配置。密钥不返回，仅返回是否已配置的 *_key_configured 标记。"""
    return ok(api_config_service.get_config_for_display())


@bp.route("/config", methods=["PATCH"])
def update_config():
    """保存配置。密钥写 .env（空串/缺失=不改），非敏感项写 DB api_config 表。

    部分更新语义：只改 body 中出现的字段。空体（{}）不写 .env、不 UPDATE 字段，
    但建空行 + 同步 key_configured 标记，返回当前配置（200，与旧版一致）。
    save_config 抛异常→40880（保留旧版「保存失败：xxx」message，避免被 errorhandler
    吞成通用 4000）。
    """
    data = request.get_json() or {}
    try:
        config = api_config_service.save_config(data)
    except Exception as e:
        raise ApiError(40880, f"保存失败：{e}", 500)
    return ok(config)


@bp.route("/config:test-connection", methods=["POST"])
def test_connection():
    """测试 LLM 端点连通性。body: {provider: 'openai'|'claude'}。

    连接结果始终 200 + {ok, msg}（连接失败是测试结果，非 HTTP 错误）；
    provider 未知/缺失→10800/400。
    """
    data = request.get_json() or {}
    provider = data.get("provider")
    if provider == "openai":
        result = api_config_service.test_openai()
    elif provider == "claude":
        result = api_config_service.test_claude()
    else:
        raise ApiError(10800, "未知的 provider", 400)
    return ok(result)
