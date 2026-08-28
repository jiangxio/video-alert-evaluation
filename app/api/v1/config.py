"""/api/v1/config 资源端点（统一 API Token 配置：获取/保存/测试连接）。

原位重写 app/routes/api_config.py 的 3 个 JSON 端点，统一信封 + 方案3 error_code
（code = HTTP 状态）。旧端点 /api-config/api/* 保留并自动加弃用 header
（deprecation.py 已预置 /api-config/api/ → /api/v1/config）。

config 是单例资源（api_config 表 CHECK(id=1)，全局唯一）。保存用 PATCH：save_config
为部分更新（存在性检测 + 密钥空串=不改 + 未传字段不动），PUT 的整体替换语义与之
矛盾。测试连接是 RPC 动作，用 :test-connection 后缀；连接失败是测试结果
（200 + {ok:false}），仅 provider 未知/缺失才是 400。

复用 app.services.api_config_service 的纯函数，不改 service、不改旧路由。
"""
from flask import request

from app.api.v1 import v1_bp
from app.api.v1.responses import err, ok

from app.services import api_config_service


@v1_bp.route("/config", methods=["GET"])
def v1_get_config():
    """获取当前配置。密钥不返回，仅返回是否已配置的 *_key_configured 标记。"""
    return ok(api_config_service.get_config_for_display())


@v1_bp.route("/config", methods=["PATCH"])
def v1_update_config():
    """保存配置。密钥写 .env（空串/缺失=不改），非敏感项写 DB api_config 表。

    部分更新语义：只改 body 中出现的字段。空体（{}）不写 .env、不 UPDATE 字段，
    但建空行 + 同步 key_configured 标记，返回当前配置（200，与旧版一致）。
    save_config 抛异常→500 CONFIG_SAVE_FAILED（保留旧版「保存失败：xxx」message）。
    """
    data = request.get_json() or {}
    try:
        config = api_config_service.save_config(data)
    except Exception as e:
        return err(500, f"保存失败：{e}", error_code="CONFIG_SAVE_FAILED")
    return ok(config)


@v1_bp.route("/config:test-connection", methods=["POST"])
def v1_test_connection():
    """测试 LLM 端点连通性。body: {provider: 'openai'|'claude'}。

    连接结果始终 200 + {ok, msg}（连接失败是测试结果，非 HTTP 错误）；
    provider 未知/缺失→400 UNKNOWN_PROVIDER。
    """
    data = request.get_json() or {}
    provider = data.get("provider")
    if provider == "openai":
        result = api_config_service.test_openai()
    elif provider == "claude":
        result = api_config_service.test_claude()
    else:
        return err(400, "未知的 provider", error_code="UNKNOWN_PROVIDER")
    return ok(result)
