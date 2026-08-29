"""L4 弃用钩子测试：旧 API 加 Deprecation+Link 头，新端点/页面不加，body 不变。

对应 docs/rest-api-feasibility-and-test-plan.md Layer 4。after_request 只加 header、
不动 body/status——golden 护栏（snapshot.py 不比 headers）不会因加头而失败，
本测试再显式断言旧端点 body 不被钩子改写。
"""


def _deprecation(resp):
    return resp.headers.get("Deprecation")


def _link(resp):
    return resp.headers.get("Link")


class TestDeprecatedHeaders:
    def test_videos_old_endpoint_marked(self, app_client):
        r = app_client.get("/videos/api/all")
        assert _deprecation(r) == "true"
        assert _link(r) == '</api/v1/videos>; rel="successor-version"'

    def test_alerts_old_endpoint_marked(self, app_client):
        r = app_client.get("/alerts/api/datasets")
        assert _deprecation(r) == "true"
        assert _link(r) == '</api/v1/alerts>; rel="successor-version"'

    def test_verification_old_endpoint_marked(self, app_client):
        # verification 蓝图无 url_prefix，端点 /api/alerts/<id>/results 是 GET
        # 即使资源不存在返 404，after_request 仍加头（钩子对所有响应跑）
        r = app_client.get("/api/alerts/1/results")
        assert _deprecation(r) == "true"
        assert _link(r) == '</api/v1/alerts>; rel="successor-version"'


class TestNotMarked:
    def test_v1_new_endpoint_no_header(self, app_client):
        r = app_client.get("/api/v1/videos")
        assert _deprecation(r) is None
        assert _link(r) is None

    def test_home_page_no_header(self, app_client):
        r = app_client.get("/")
        assert _deprecation(r) is None
        assert _link(r) is None


class TestBodyUntouched:
    def test_old_endpoint_body_not_rewritten(self, app_client):
        # 钩子只加 header，旧端点 body 逐字节不变（不是 v1 信封形状）
        r = app_client.get("/videos/api/all")
        body = r.get_json()
        # 旧端点返旧格式，不会被钩子改写成 v1 {code:0,...} 信封
        assert not (isinstance(body, dict) and body.get("code") == 0 and "data" in body)
