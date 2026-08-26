"""验证旧端点弃用标记 after_request 钩子。"""


def test_legacy_endpoint_marked_deprecated(client):
    """旧 /videos/api/all 响应带 Deprecation + Link 指向新资源。"""
    resp = client.get("/videos/api/all")
    assert resp.headers.get("Deprecation") == "true"
    link = resp.headers.get("Link", "")
    assert "/api/v1/videos" in link
    assert 'rel="successor-version"' in link


def test_new_api_not_marked_deprecated(client):
    """/api/v1/videos 不带弃用标记。"""
    resp = client.get("/api/v1/videos")
    assert resp.headers.get("Deprecation") is None


def test_page_not_marked_deprecated(client):
    """页面路由（非 /xxx/api/）不带弃用标记。"""
    resp_home = client.get("/")
    assert resp_home.headers.get("Deprecation") is None
