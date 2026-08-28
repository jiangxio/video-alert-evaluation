"""端到端验证 /api/v1/config 端点（统一 API Token 配置：获取/保存/测试连接）。

DB 为 conftest tmp 库（create_app→init_db 建全 schema 含 api_config 表，但只建表
不预插行——首行由 save 的 INSERT OR IGNORE 创建）。save_config->_write_env 写到
conftest 重定向到 tmp 的 ENV_PATH，不碰真实 .env。load_dotenv 的 os.environ 副作用
由 autouse _isolate_env_keys 隔离（teardown 自动恢复）。

get/save 真走 service（本地 DB+文件 I/O）；test-connection mock test_openai/test_claude
（外部 LLM API，CI 不可控）。全快测，无 slow。
"""
import pytest

from app.services import api_config_service


# load_dotenv 会注入 os.environ 的凭据键；清空以隔离副作用，teardown 自动恢复。
_ENV_KEYS = (
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL",
    "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
)


@pytest.fixture(autouse=True)
def _isolate_env_keys(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


def _data(resp):
    assert 200 <= resp.status_code < 300, resp.status_code
    body = resp.get_json()
    assert body["code"] == 0
    return body["data"]


def _err(resp, status):
    assert resp.status_code == status, resp.status_code
    assert resp.get_json()["code"] == status


def _read_env_text():
    p = api_config_service.ENV_PATH
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


# ── 获取配置 ─────────────────────────────────────────────────────────────────

def test_get_config_defaults(client):
    """未保存时返回默认值（env 已清空，无 DB 行）。"""
    cfg = _data(client.get("/api/v1/config"))
    assert cfg["openai_base_url"] == "https://api.openai.com/v1"
    assert cfg["openai_model"] == "gpt-4o-mini"
    assert cfg["openai_request_interval_sec"] == 1
    assert cfg["openai_key_configured"] is False
    assert cfg["claude_base_url"] == ""
    assert cfg["claude_model"] == "claude-sonnet-5"
    assert cfg["claude_key_configured"] is False


def test_get_config_envelope(client):
    body = client.get("/api/v1/config").get_json()
    assert body["code"] == 0
    assert body["message"] == "ok"
    assert "data" in body


# ── 保存配置（PATCH）─────────────────────────────────────────────────────────

def test_update_config_partial(client):
    """PATCH 只传 openai_base_url，未传字段（openai_model）不被清空。"""
    _data(client.patch("/api/v1/config", json={"openai_model": "mymodel"}))
    data = _data(client.patch("/api/v1/config", json={"openai_base_url": "https://x.example/v1"}))
    assert data["openai_base_url"] == "https://x.example/v1"
    assert data["openai_model"] == "mymodel"  # 保持上次值，未被清空


def test_update_config_returns_config(client):
    data = _data(client.patch("/api/v1/config", json={"openai_model": "echoed"}))
    assert data["openai_model"] == "echoed"
    assert data["openai_key_configured"] is False  # 未传密钥


def test_update_config_writes_env_key(client):
    """传 openai_api_key 写入 tmp .env，且返回 key_configured=True。"""
    data = _data(client.patch("/api/v1/config", json={"openai_api_key": "sk-test-123"}))
    assert data["openai_key_configured"] is True
    assert "OPENAI_API_KEY=sk-test-123" in _read_env_text()


def test_update_config_blank_key_no_change(client):
    """空 openai_api_key=不改：先写 key，再传空串，.env 不被清空。"""
    _data(client.patch("/api/v1/config", json={"openai_api_key": "sk-first"}))
    _data(client.patch("/api/v1/config", json={"openai_api_key": ""}))
    assert "OPENAI_API_KEY=sk-first" in _read_env_text()


def test_update_config_failure(client, monkeypatch):
    """save_config 抛异常→40880/500，message 含「保存失败」。"""
    def _boom(_updates):
        raise OSError("disk full")
    monkeypatch.setattr(api_config_service, "_write_env", _boom)
    resp = client.patch("/api/v1/config", json={"openai_api_key": "sk-x"})
    _err(resp, 500)
    assert "保存失败" in resp.get_json()["message"]


# ── 测试连接（:test-connection）──────────────────────────────────────────────

def test_test_connection_unknown_provider(client):
    _err(client.post("/api/v1/config:test-connection", json={"provider": "xxx"}), 400)


def test_test_connection_missing_provider(client):
    _err(client.post("/api/v1/config:test-connection", json={}), 400)


def test_test_connection_openai(client, monkeypatch):
    monkeypatch.setattr(api_config_service, "test_openai",
                        lambda: {"ok": True, "msg": "连接成功（mock）"})
    data = _data(client.post("/api/v1/config:test-connection", json={"provider": "openai"}))
    assert data["ok"] is True
    assert "mock" in data["msg"]


def test_test_connection_claude_failure(client, monkeypatch):
    """连接失败是测试结果（200 + ok=False），非 HTTP 错误。"""
    monkeypatch.setattr(api_config_service, "test_claude",
                        lambda: {"ok": False, "msg": "连接失败（mock）"})
    data = _data(client.post("/api/v1/config:test-connection", json={"provider": "claude"}))
    assert data["ok"] is False
    assert "失败" in data["msg"]
