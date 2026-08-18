"""L2 契约测试：alerts v1 端点结构（datasets / images / eval-sets / algorithm-versions）。

复用 app_client（完整 schema、空 DB、隔离）。断言聚焦信封结构与分流行为，
避开真实图片文件（只测错误路径 + envelope 形状），仿 test_api_v1_videos 哲学。
"""


def _body(resp):
    return resp.get_json()


def _create_dataset(client, name="d1", mode="normal"):
    r = client.post("/api/v1/alerts/datasets", json={"name": name, "mode": mode})
    assert r.status_code == 200
    return _body(r)["data"]["dataset"]["id"]


def _create_eval_set(client, name="e1"):
    r = client.post("/api/v1/alerts/eval-sets", json={"name": name})
    assert r.status_code == 200
    return _body(r)["data"]["id"]


# ===== 数据集 datasets =====
class TestDatasets:
    def test_create(self, app_client):
        r = app_client.post("/api/v1/alerts/datasets", json={"name": "d1", "mode": "realtime"})
        assert r.status_code == 200
        body = _body(r)
        assert body["code"] == 0
        ds = body["data"]["dataset"]
        assert ds["name"] == "d1"
        assert ds["mode"] == "realtime"
        assert isinstance(ds["id"], int)

    def test_create_empty_name(self, app_client):
        r = app_client.post("/api/v1/alerts/datasets", json={"name": ""})
        assert r.status_code == 400
        assert _body(r)["code"] == 400

    def test_list_envelope(self, app_client):
        _create_dataset(app_client, "d1")
        r = app_client.get("/api/v1/alerts/datasets")
        assert r.status_code == 200
        data = _body(r)["data"]
        assert set(data.keys()) >= {"items", "total", "page", "page_size", "has_next"}
        assert data["total"] >= 1
        assert data["items"][0]["name"] == "d1"

    def test_detail(self, app_client):
        did = _create_dataset(app_client, "d1")
        r = app_client.get(f"/api/v1/alerts/datasets/{did}")
        assert r.status_code == 200
        body = _body(r)
        assert body["code"] == 0
        assert body["data"]["id"] == did
        assert body["data"]["name"] == "d1"

    def test_detail_missing(self, app_client):
        r = app_client.get("/api/v1/alerts/datasets/9999")
        assert r.status_code == 404
        body = _body(r)
        assert body["code"] == 404
        assert body["error_code"] == "DATASET_NOT_FOUND"

    def test_patch_mode(self, app_client):
        did = _create_dataset(app_client, "d1")
        r = app_client.patch(f"/api/v1/alerts/datasets/{did}", json={"mode": "realtime"})
        assert r.status_code == 200
        assert _body(r)["data"]["mode"] == "realtime"

    def test_patch_unknown_field_rejected(self, app_client):
        did = _create_dataset(app_client, "d1")
        r = app_client.patch(f"/api/v1/alerts/datasets/{did}", json={"name": "x"})
        assert r.status_code == 400
        body = _body(r)
        assert body["code"] == 400
        assert body["error_code"] == "UNKNOWN_FIELD"

    def test_delete(self, app_client):
        did = _create_dataset(app_client, "d1")
        r = app_client.delete(f"/api/v1/alerts/datasets/{did}")
        assert r.status_code == 200
        assert _body(r)["code"] == 0
        # 删除后再查 → 404
        assert app_client.get(f"/api/v1/alerts/datasets/{did}").status_code == 404

    def test_delete_missing(self, app_client):
        r = app_client.delete("/api/v1/alerts/datasets/9999")
        assert r.status_code == 404
        assert _body(r)["code"] == 404


# ===== 算法版本 algorithm-versions =====
class TestAlgorithmVersions:
    def test_list_missing_dataset(self, app_client):
        r = app_client.get("/api/v1/alerts/datasets/9999/algorithm-versions")
        assert r.status_code == 404
        assert _body(r)["code"] == 404

    def test_list_empty_envelope(self, app_client):
        did = _create_dataset(app_client)
        r = app_client.get(f"/api/v1/alerts/datasets/{did}/algorithm-versions")
        assert r.status_code == 200
        data = _body(r)["data"]
        assert data["items"] == []
        assert data["total"] == 0


# ===== 图片 images（避开真实文件，测错误路径 + envelope）=====
class TestImages:
    def test_list_empty_reshape(self, app_client):
        did = _create_dataset(app_client)
        r = app_client.get(f"/api/v1/alerts/datasets/{did}/images")
        assert r.status_code == 200
        data = _body(r)["data"]
        # reshape 信封：保留旧 total/page，page_size 来自旧 per_page
        assert set(data.keys()) >= {"items", "total", "page", "page_size", "has_next"}
        assert data["items"] == []
        assert data["total"] == 0
        assert data["has_next"] is False

    def test_list_missing_dataset(self, app_client):
        r = app_client.get("/api/v1/alerts/datasets/9999/images")
        assert r.status_code == 404
        assert _body(r)["error_code"] == "DATASET_NOT_FOUND"

    def test_logs_empty(self, app_client):
        did = _create_dataset(app_client)
        r = app_client.get(f"/api/v1/alerts/datasets/{did}/images/logs")
        assert r.status_code == 200
        assert _body(r)["data"]["items"] == []

    def test_image_detail_missing(self, app_client):
        r = app_client.get("/api/v1/alerts/images/9999")
        assert r.status_code == 404
        assert _body(r)["code"] == 404

    def test_image_file_missing(self, app_client):
        r = app_client.get("/api/v1/alerts/images/9999/file")
        assert r.status_code == 404
        assert _body(r)["code"] == 404

    def test_image_patch_unknown_field(self, app_client):
        # 白名单检查在委托前，即便图片不存在也先返 400 UNKNOWN_FIELD
        r = app_client.patch("/api/v1/alerts/images/9999", json={"filename": "x"})
        assert r.status_code == 400
        assert _body(r)["error_code"] == "UNKNOWN_FIELD"

    def test_image_patch_label_missing_image(self, app_client):
        r = app_client.patch("/api/v1/alerts/images/9999", json={"event_label": "fight"})
        assert r.status_code == 404
        assert _body(r)["code"] == 404

    def test_image_delete_missing(self, app_client):
        r = app_client.delete("/api/v1/alerts/images/9999")
        assert r.status_code == 404

    def test_batch_delete_no_match(self, app_client):
        did = _create_dataset(app_client)
        r = app_client.post(f"/api/v1/alerts/datasets/{did}/images:batch-delete", json={})
        assert r.status_code == 404
        assert _body(r)["code"] == 404

    def test_upload_no_file(self, app_client):
        did = _create_dataset(app_client)
        r = app_client.post(f"/api/v1/alerts/datasets/{did}/images")
        assert r.status_code == 400
        assert _body(r)["code"] == 400

    def test_upload_missing_dataset(self, app_client):
        r = app_client.post("/api/v1/alerts/datasets/9999/images")
        assert r.status_code == 404

    def test_import_no_file(self, app_client):
        did = _create_dataset(app_client)
        r = app_client.post(f"/api/v1/alerts/datasets/{did}/images:import")
        assert r.status_code == 400
        assert _body(r)["code"] == 400

    def test_download_missing_dataset(self, app_client):
        r = app_client.get("/api/v1/alerts/datasets/9999/download")
        assert r.status_code == 404
        assert _body(r)["code"] == 404

    def test_download_empty_dataset(self, app_client):
        did = _create_dataset(app_client)
        r = app_client.get(f"/api/v1/alerts/datasets/{did}/download")
        assert r.status_code == 404
        assert _body(r)["code"] == 404


# ===== 评测集 eval-sets =====
class TestEvalSets:
    def test_create(self, app_client):
        eid = _create_eval_set(app_client, "e1")
        assert isinstance(eid, int)

    def test_list_envelope(self, app_client):
        _create_eval_set(app_client, "e1")
        r = app_client.get("/api/v1/alerts/eval-sets")
        assert r.status_code == 200
        data = _body(r)["data"]
        assert set(data.keys()) >= {"items", "total", "page", "page_size", "has_next"}
        assert data["total"] >= 1
        assert data["items"][0]["name"] == "e1"

    def test_detail(self, app_client):
        eid = _create_eval_set(app_client, "e1")
        r = app_client.get(f"/api/v1/alerts/eval-sets/{eid}")
        assert r.status_code == 200
        assert _body(r)["data"]["id"] == eid

    def test_detail_missing(self, app_client):
        r = app_client.get("/api/v1/alerts/eval-sets/9999")
        assert r.status_code == 404
        assert _body(r)["error_code"] == "EVAL_SET_NOT_FOUND"

    def test_patch_name(self, app_client):
        eid = _create_eval_set(app_client, "e1")
        r = app_client.patch(f"/api/v1/alerts/eval-sets/{eid}", json={"name": "e2"})
        assert r.status_code == 200
        assert _body(r)["code"] == 0

    def test_patch_unknown_field(self, app_client):
        eid = _create_eval_set(app_client, "e1")
        r = app_client.patch(f"/api/v1/alerts/eval-sets/{eid}", json={"notes": "x"})
        assert r.status_code == 400
        assert _body(r)["error_code"] == "UNKNOWN_FIELD"

    def test_batch_add_dedup(self, app_client):
        eid = _create_eval_set(app_client)
        r = app_client.post(f"/api/v1/alerts/eval-sets/{eid}/datasets:batch-add",
                            json={"dataset_ids": [1, 2, 3]})
        assert r.status_code == 200
        assert _body(r)["data"]["added_count"] == 3
        # 重复添加 → 去重
        r2 = app_client.post(f"/api/v1/alerts/eval-sets/{eid}/datasets:batch-add",
                             json={"dataset_ids": [1, 2, 3]})
        assert _body(r2)["data"]["added_count"] == 0
        # 部分新增
        r3 = app_client.post(f"/api/v1/alerts/eval-sets/{eid}/datasets:batch-add",
                             json={"dataset_ids": [3, 4]})
        assert _body(r3)["data"]["added_count"] == 1

    def test_batch_add_missing_set(self, app_client):
        r = app_client.post("/api/v1/alerts/eval-sets/9999/datasets:batch-add",
                            json={"dataset_ids": [1]})
        assert r.status_code == 404
        assert _body(r)["error_code"] == "EVAL_SET_NOT_FOUND"

    def test_batch_add_empty(self, app_client):
        eid = _create_eval_set(app_client)
        r = app_client.post(f"/api/v1/alerts/eval-sets/{eid}/datasets:batch-add",
                            json={"dataset_ids": []})
        assert r.status_code == 400
        assert _body(r)["error_code"] == "VALIDATION_ERROR"

    def test_batch_remove(self, app_client):
        eid = _create_eval_set(app_client)
        app_client.post(f"/api/v1/alerts/eval-sets/{eid}/datasets:batch-add",
                        json={"dataset_ids": [1, 2]})
        r = app_client.post(f"/api/v1/alerts/eval-sets/{eid}/datasets:batch-remove",
                            json={"dataset_ids": [1, 2]})
        assert r.status_code == 200
        assert _body(r)["data"]["removed_count"] == 2
        # 重复移除 → 0
        r2 = app_client.post(f"/api/v1/alerts/eval-sets/{eid}/datasets:batch-remove",
                             json={"dataset_ids": [1]})
        assert _body(r2)["data"]["removed_count"] == 0

    def test_delete(self, app_client):
        eid = _create_eval_set(app_client)
        r = app_client.delete(f"/api/v1/alerts/eval-sets/{eid}")
        assert r.status_code == 200
        assert app_client.get(f"/api/v1/alerts/eval-sets/{eid}").status_code == 404
