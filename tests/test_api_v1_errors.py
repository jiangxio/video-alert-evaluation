"""L3 错误分发测试：v1 路径返错误信封，其余回退默认；ApiError 转信封；413 保持旧格式。

对应 docs/rest-api-feasibility-and-test-plan.md Layer 3。断言聚焦分流行为，
不绑定旧视图具体业务返回值。复用 app_client fixture（完整 schema、隔离无副作用）。
"""


def _body(resp):
    return resp.get_json()


class TestV1ErrorEnvelope:
    def test_v1_404_returns_envelope(self, app_client):
        r = app_client.get("/api/v1/不存在")
        assert r.status_code == 404
        assert r.content_type.startswith("application/json")
        body = _body(r)
        assert body["code"] == 404
        assert "message" in body

    def test_v1_normal_not_affected(self, app_client):
        # 正常 v1 端点不被 errorhandler 误伤
        r = app_client.get("/api/v1/videos")
        assert r.status_code == 200
        assert _body(r)["code"] == 0


class TestLegacyFallback:
    def test_legacy_404_not_envelope(self, app_client):
        # 旧路径 404 回退 Flask 默认 HTML，非 v1 信封
        r = app_client.get("/videos/api/不存在端点")
        assert r.status_code == 404
        # 非 v1：content-type 非 json（默认 HTML 404 页）
        assert not r.content_type.startswith("application/json")
        body = _body(r)
        # 不是 v1 成功信封形状
        assert not (isinstance(body, dict) and body.get("code") == 0)


class TestApiErrorHandling:
    def test_api_error_becomes_envelope(self, app_client, monkeypatch):
        # v1 端点主动 raise ApiError → 转错误信封
        from app.api.v1.responses import ApiError

        app = app_client.application

        def boom(*args, **kwargs):
            raise ApiError(409, "冲突", errors=["x"])

        # v1 list videos 的 endpoint 全名：蓝图名 api_v1 + 函数名 v1_list_videos
        monkeypatch.setitem(app.view_functions, "api_v1.v1_list_videos", boom)

        r = app_client.get("/api/v1/videos")
        assert r.status_code == 409
        body = _body(r)
        assert body["code"] == 409
        assert body["message"] == "冲突"
        assert body["errors"] == ["x"]

    def test_api_error_error_code_propagates(self, app_client, monkeypatch):
        # 方案3：ApiError 携带 error_code，经 handler 转入错误信封
        from app.api.v1.responses import ApiError

        app = app_client.application

        def boom(*args, **kwargs):
            raise ApiError(409, "冲突", error_code="DATASET_EXISTS")

        monkeypatch.setitem(app.view_functions, "api_v1.v1_list_videos", boom)

        r = app_client.get("/api/v1/videos")
        assert r.status_code == 409
        body = _body(r)
        assert body["error_code"] == "DATASET_EXISTS"


class Test413Boundary:
    def test_413_keeps_legacy_format(self, app_client):
        # 已知边界：413 handler 未信封化，v1 上传超限仍返旧 {'error':...} 格式
        app = app_client.application
        app.config["MAX_CONTENT_LENGTH"] = 10
        r = app_client.post("/api/v1/videos", data="x" * 100,
                            content_type="application/json")
        assert r.status_code == 413
        body = _body(r)
        assert "error" in body  # 旧格式
        assert body.get("code") != 0  # 非 v1 成功信封
