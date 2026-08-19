"""L2 契约测试：alerts OCR 系列 v1 端点。

只测错误路径 + envelope 形状 + ocr-status 无任务语义修正，不真跑 EasyOCR
（真跑需 PIL 生成水印图 + easyocr，留作 slow 用例）。复用 app_client。
"""


def _body(resp):
    return resp.get_json()


def _create_dataset(client, name="d1"):
    r = client.post("/api/v1/alerts/datasets", json={"name": name})
    assert r.status_code == 200
    return _body(r)["data"]["dataset"]["id"]


class TestOcrSingle:
    def test_image_not_found(self, app_client):
        r = app_client.post("/api/v1/alerts/images/9999/ocr")
        assert r.status_code == 404
        assert _body(r)["code"] == 404


class TestOcrManual:
    def test_image_not_found(self, app_client):
        r = app_client.post("/api/v1/alerts/images/9999/ocr:manual",
                            json={"video_id": "v1", "timestamp": "00:01:30"})
        assert r.status_code == 404
        assert _body(r)["code"] == 404


class TestOcrBatch:
    def test_dataset_not_found(self, app_client):
        r = app_client.post("/api/v1/alerts/datasets/9999/ocr:batch", json={})
        assert r.status_code == 404
        assert _body(r)["code"] == 404

    def test_no_images_to_ocr(self, app_client):
        # 空数据集（无图）→ 旧版返 400「没有需要 OCR 的图片」
        did = _create_dataset(app_client)
        r = app_client.post(f"/api/v1/alerts/datasets/{did}/ocr:batch", json={})
        assert r.status_code == 400
        assert _body(r)["code"] == 400


class TestOcrStatus:
    def test_no_task_returns_empty_200(self, app_client):
        # 有意偏差：无任务旧版 404，v1 改 200 空进度
        did = _create_dataset(app_client)
        r = app_client.get(f"/api/v1/alerts/datasets/{did}/ocr-status")
        assert r.status_code == 200
        body = _body(r)
        assert body["code"] == 0
        data = body["data"]
        assert data["running"] is False
        assert data["total"] == 0
        assert data["done"] == 0
        assert data["results"] == []


class TestOcrCancel:
    def test_idempotent_when_not_running(self, app_client):
        # 未运行也返 200（幂等）
        did = _create_dataset(app_client)
        r = app_client.post(f"/api/v1/alerts/datasets/{did}/ocr-status:cancel")
        assert r.status_code == 200
        assert _body(r)["code"] == 0
