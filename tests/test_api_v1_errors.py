"""验证 app 级 errorhandler 的 /api/v1/ 分流。"""
import pytest


def test_api_v1_404_returns_envelope(client):
    """未命中的 /api/v1/ 路由 → 404 + 统一错误信封。"""
    resp = client.get("/api/v1/definitely-nonexistent")
    assert resp.status_code == 404
    body = resp.get_json()
    assert body["code"] == 20000
    assert "message" in body
    assert resp.content_type == "application/json"


def test_non_api_404_falls_back_to_default(client):
    """旧命名空间未命中路由 → 默认 HTML 404，不是信封。"""
    resp = client.get("/definitely-nonexistent-old-page-xyz")
    assert resp.status_code == 404
    assert "text/html" in resp.content_type


def test_api_v1_405_returns_envelope(client):
    """/api/v1/videos 不支持 PUT → 405 信封。"""
    resp = client.put("/api/v1/videos", json={})
    assert resp.status_code == 405
    body = resp.get_json()
    assert body["code"] == 10005


def test_api_error_envelope(client):
    """新端点 raise ApiError → 信封错误（资源不存在）。"""
    resp = client.get("/api/v1/videos/999999")
    assert resp.status_code == 404
    body = resp.get_json()
    assert body["code"] == 20120
    assert body["message"] == "视频不存在"
