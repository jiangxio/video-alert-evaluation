"""L2 契约测试：videos v1 端点结构。

在 fresh 空 DB 上验证：列表分页信封、分页参数容错、?q= 过滤、
CRUD/二进制委托端点的错误信封与透传。断言聚焦结构（envelope 形状），
不绑定旧视图具体业务返回值，避免在无运行环境下写易碎断言。
"""


def _body(resp):
    return resp.get_json()


class TestVideosList:
    def test_list_empty_envelope(self, app_client):
        r = app_client.get("/api/v1/videos")
        assert r.status_code == 200
        assert r.content_type.startswith("application/json")
        body = _body(r)
        assert body["code"] == 0
        data = body["data"]
        assert set(data.keys()) >= {"items", "total", "page", "page_size", "has_next"}
        assert data["items"] == []
        assert data["total"] == 0
        assert data["page"] == 1
        assert data["page_size"] == 20
        assert data["has_next"] is False

    def test_pagination_params(self, app_client):
        r = app_client.get("/api/v1/videos?page=2&page_size=5")
        body = _body(r)
        assert body["data"]["page"] == 2
        assert body["data"]["page_size"] == 5

    def test_bad_pagination_clamps(self, app_client):
        r = app_client.get("/api/v1/videos?page=0&page_size=999")
        body = _body(r)
        assert body["data"]["page"] == 1
        assert body["data"]["page_size"] == 100

    def test_q_filter_envelope(self, app_client):
        r = app_client.get("/api/v1/videos?q=xyz")
        assert r.status_code == 200
        assert _body(r)["code"] == 0


class TestVideosDelegated:
    def test_delete_missing_returns_error_envelope(self, app_client):
        r = app_client.delete("/api/v1/videos/9999")
        # 不绑定具体状态码（旧视图可能 404/400），只验错误信封形状
        assert r.status_code >= 400
        body = _body(r)
        assert body["code"] == r.status_code
        assert "message" in body

    def test_download_missing_returns_error_envelope(self, app_client):
        r = app_client.get("/api/v1/videos/9999/download")
        assert r.status_code >= 400
        body = _body(r)
        assert body["code"] == r.status_code
        assert "message" in body

    def test_eval_sets_list_empty_envelope(self, app_client):
        r = app_client.get("/api/v1/videos/eval-sets")
        assert r.status_code == 200
        body = _body(r)
        assert body["code"] == 0
        data = body["data"]
        assert set(data.keys()) >= {"items", "total", "page", "page_size", "has_next"}
        assert data["total"] == 0
        assert data["items"] == []

    def test_create_eval_set_empty_name(self, app_client):
        # 旧 create_eval_set 对空名返 400 → 包装成错误信封
        r = app_client.post("/api/v1/videos/eval-sets", json={})
        assert r.status_code == 400
        body = _body(r)
        assert body["code"] == 400
        assert "message" in body
