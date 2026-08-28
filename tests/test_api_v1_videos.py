"""端到端验证 /api/v1/videos 资源族（信封/分页/CRUD）。"""
import io


def _envelope(resp):
    """断言响应为成功信封并返回 data。"""
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["code"] == 0
    assert body["message"] == "ok"
    return body["data"]


def test_list_empty(client):
    resp = client.get("/api/v1/videos")
    data = _envelope(resp)
    assert data["items"] == []
    assert data["total"] == 0
    assert data["page"] == 1
    assert data["page_size"] == 20
    assert data["has_next"] is False


def test_upload_then_list(client):
    resp = client.post(
        "/api/v1/videos",
        data={"video": (io.BytesIO(b"fake-video-bytes"), "0460000001_test.mp4")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["code"] == 0
    vid = body["data"]["video"]["id"]
    assert body["data"]["video"]["filename"] == "0460000001_test.mp4"
    assert body["data"]["video"]["video_id"] == "0460000001"


    # 列表应包含该视频
    data = _envelope(client.get("/api/v1/videos"))
    assert data["total"] == 1
    assert data["items"][0]["id"] == vid
    assert data["items"][0]["has_watermark"] is False


def test_get_single_and_not_found(client):
    resp = client.post(
        "/api/v1/videos",
        data={"video": (io.BytesIO(b"x"), "0460000002_t.mp4")},
        content_type="multipart/form-data",
    )
    vid = resp.get_json()["data"]["video"]["id"]

    data = _envelope(client.get(f"/api/v1/videos/{vid}"))
    assert data["id"] == vid

    not_found = client.get("/api/v1/videos/999999")
    assert not_found.status_code == 404
    assert not_found.get_json()["code"] == 404


def test_patch_video_id(client):
    resp = client.post(
        "/api/v1/videos",
        data={"video": (io.BytesIO(b"x"), "0460000003_t.mp4")},
        content_type="multipart/form-data",
    )
    vid = resp.get_json()["data"]["video"]["id"]

    data = _envelope(client.patch(f"/api/v1/videos/{vid}", json={"video_id": "1234567890"}))
    assert data["video_id"] == "1234567890"

    bad = client.patch(f"/api/v1/videos/{vid}", json={"video_id": "bad"})
    assert bad.status_code == 400
    assert bad.get_json()["code"] == 400


def test_delete_video(client):
    resp = client.post(
        "/api/v1/videos",
        data={"video": (io.BytesIO(b"x"), "0460000004_t.mp4")},
        content_type="multipart/form-data",
    )
    vid = resp.get_json()["data"]["video"]["id"]

    del_resp = client.delete(f"/api/v1/videos/{vid}")
    assert del_resp.status_code == 200

    # 删除后 GET → 404
    assert client.get(f"/api/v1/videos/{vid}").status_code == 404


def test_eval_sets_crud(client):
    # 列表为空
    data = _envelope(client.get("/api/v1/videos/eval-sets"))
    assert data["total"] == 0

    # 创建
    resp = client.post("/api/v1/videos/eval-sets", json={"name": "set1", "video_ids": [1, 2]})
    assert resp.status_code == 200
    sid = resp.get_json()["data"]["id"]


    # 替换（改名 + 换成员）
    data = _envelope(client.put(f"/api/v1/videos/eval-sets/{sid}", json={"name": "set2", "video_ids": [3]}))
    assert data["name"] == "set2"
    assert data["video_ids"] == [3]

    # 删除 → 204
    assert client.delete(f"/api/v1/videos/eval-sets/{sid}").status_code == 204
    data = _envelope(client.get("/api/v1/videos/eval-sets"))
    assert data["total"] == 0
