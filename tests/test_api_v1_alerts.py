"""端到端验证 /api/v1/alerts 资源族（datasets/images/eval-sets）。

OCR 系列端点（/ocr 等）不在本模块覆盖范围，留待后续阶段。
"""
import io

from werkzeug.datastructures import MultiDict


# 最小有效 PNG（1x1）
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c636000000000020001e221bc330000000049454e44ae426082"
)


def _envelope(resp):
    assert resp.status_code == 200, resp.status_code
    body = resp.get_json()
    assert body["code"] == 0
    return body["data"]


def _make_dataset(client, name="ds"):
    return client.post("/api/v1/alerts/datasets", json={"name": name}).get_json()["data"]["id"]


# ── datasets CRUD + PATCH(mode) ────────────────────────────────────────────────

def test_datasets_crud(client):
    assert _envelope(client.get("/api/v1/alerts/datasets"))["total"] == 0

    resp = client.post("/api/v1/alerts/datasets", json={"name": "ds1", "notes": "n", "mode": "normal"})
    assert resp.status_code == 201
    did = resp.get_json()["data"]["id"]
    assert resp.headers["Location"].endswith(f"/api/v1/alerts/datasets/{did}")

    data = _envelope(client.get(f"/api/v1/alerts/datasets/{did}"))
    assert data["name"] == "ds1"
    assert data["image_count"] == 0

    # PATCH 只支持 mode
    data = _envelope(client.patch(f"/api/v1/alerts/datasets/{did}", json={"mode": "realtime"}))
    assert data["mode"] == "realtime"

    # PATCH 不支持的字段 → 400
    bad = client.patch(f"/api/v1/alerts/datasets/{did}", json={"name": "x"})
    assert bad.status_code == 400
    assert bad.get_json()["code"] == 10210

    # 非法 mode
    assert client.patch(f"/api/v1/alerts/datasets/{did}", json={"mode": "bogus"}).status_code == 400

    # 不存在 → 404 信封
    nf = client.get("/api/v1/alerts/datasets/999999")
    assert nf.status_code == 404 and nf.get_json()["code"] == 20220

    assert client.delete(f"/api/v1/alerts/datasets/{did}").status_code == 204
    assert client.get(f"/api/v1/alerts/datasets/{did}").status_code == 404


# ── algorithm-versions（保持 POST） ────────────────────────────────────────────

def test_algorithm_versions_endpoints(client):
    did = _make_dataset(client)
    # GET 空列表
    assert _envelope(client.get(f"/api/v1/alerts/datasets/{did}/algorithm-versions")) == []
    # POST 设置（无算法版本时传空数组，校验通过）
    resp = client.post(f"/api/v1/alerts/datasets/{did}/algorithm-versions", json={"algorithm_version_ids": []})
    assert resp.status_code == 200
    assert resp.get_json()["data"] == []
    # 不存在的数据集 → 404
    assert client.get("/api/v1/alerts/datasets/999999/algorithm-versions").status_code == 404


# ── images 上传/列表/label/file/删除 ───────────────────────────────────────────

def test_image_lifecycle(client):
    did = _make_dataset(client)
    resp = client.post(
        f"/api/v1/alerts/datasets/{did}/images",
        data=MultiDict([
            ("image", (io.BytesIO(_PNG), "402_1774925112_103.png")),
            ("image", (io.BytesIO(_PNG), "402_1774925112_104.png")),
        ]),
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    uploaded = resp.get_json()["data"]["uploaded"]
    assert len(uploaded) == 2
    img_id = uploaded[0]["id"]

    # 列表
    data = _envelope(client.get(f"/api/v1/alerts/datasets/{did}/images"))
    assert data["total"] == 2
    assert data["items"][0]["ocr"] is None

    # 详情
    assert _envelope(client.get(f"/api/v1/alerts/images/{img_id}"))["id"] == img_id

    # PATCH label
    data = _envelope(client.patch(f"/api/v1/alerts/images/{img_id}", json={"event_label": "fight"}))
    assert data["event_label"] == "fight"
    # 不支持的字段 → 400
    assert client.patch(f"/api/v1/alerts/images/{img_id}", json={"filename": "x"}).status_code == 400

    # file（二进制 + 缩略图）
    f = client.get(f"/api/v1/alerts/images/{img_id}/file")
    assert f.status_code == 200 and f.content_type.startswith("image/")
    assert client.get(f"/api/v1/alerts/images/{img_id}/file?w=1&h=1").status_code == 200

    # 删除 → 204
    assert client.delete(f"/api/v1/alerts/images/{img_id}").status_code == 204
    assert client.get(f"/api/v1/alerts/images/{img_id}").status_code == 404


def test_image_upload_no_file(client):
    did = _make_dataset(client)
    assert client.post(f"/api/v1/alerts/datasets/{did}/images", data={}).status_code == 400


# ── :batch-delete action ───────────────────────────────────────────────────────

def test_batch_delete_action(client):
    did = _make_dataset(client)
    client.post(
        f"/api/v1/alerts/datasets/{did}/images",
        data=MultiDict([
            ("image", (io.BytesIO(_PNG), "a_103.png")),
            ("image", (io.BytesIO(_PNG), "b_104.png")),
        ]),
        content_type="multipart/form-data",
    )
    resp = client.post(f"/api/v1/alerts/datasets/{did}/images:batch-delete", json={})
    assert resp.status_code == 200
    assert resp.get_json()["data"]["deleted_count"] == 2
    # 再删 → 已空 404
    assert client.post(f"/api/v1/alerts/datasets/{did}/images:batch-delete", json={}).status_code == 404


# ── images/logs ────────────────────────────────────────────────────────────────

def test_image_logs(client):
    did = _make_dataset(client)
    client.post(
        f"/api/v1/alerts/datasets/{did}/images",
        data=MultiDict([("image", (io.BytesIO(_PNG), "c_103.png"))]),
        content_type="multipart/form-data",
    )
    data = _envelope(client.get(f"/api/v1/alerts/datasets/{did}/images/logs"))
    assert len(data) == 1
    assert data[0]["action"] == "upload" and data[0]["image_count"] == 1


# ── datasets/download（GET） ───────────────────────────────────────────────────

def test_dataset_download(client):
    did = _make_dataset(client, "zipsrc")
    client.post(
        f"/api/v1/alerts/datasets/{did}/images",
        data=MultiDict([("image", (io.BytesIO(_PNG), "d_103.png"))]),
        content_type="multipart/form-data",
    )
    resp = client.get(f"/api/v1/alerts/datasets/{did}/download")
    assert resp.status_code == 200
    assert resp.content_type == "application/zip"
    assert resp.data[:2] == b"PK"  # zip magic

    # 空数据集下载 → 404
    empty_did = _make_dataset(client, "empty")
    assert client.get(f"/api/v1/alerts/datasets/{empty_did}/download").status_code == 404


# ── eval-alert-sets CRUD + 成员 :batch-add/:batch-remove ───────────────────────

def test_eval_sets_crud_and_members(client):
    assert _envelope(client.get("/api/v1/alerts/eval-sets"))["total"] == 0

    # 创建（带 dataset_ids 初值）
    resp = client.post("/api/v1/alerts/eval-sets", json={"name": "es1", "dataset_ids": [1]})
    assert resp.status_code == 201
    sid = resp.get_json()["data"]["id"]

    # GET 单条
    data = _envelope(client.get(f"/api/v1/alerts/eval-sets/{sid}"))
    assert data["name"] == "es1"
    assert data["dataset_ids"] == [1]
    assert data["dataset_count"] == 1

    # PATCH 只改 name
    data = _envelope(client.patch(f"/api/v1/alerts/eval-sets/{sid}", json={"name": "es2"}))
    assert data["name"] == "es2"
    assert data["dataset_ids"] == [1]  # 未被触碰

    # PATCH 不支持字段 → 400
    assert client.patch(f"/api/v1/alerts/eval-sets/{sid}", json={"notes": "x"}).status_code == 400

    # 成员 :batch-add
    data = _envelope(client.post(
        f"/api/v1/alerts/eval-sets/{sid}/datasets:batch-add",
        json={"dataset_ids": [2, 3, 1]},  # 1 已存在，应去重
    ))
    assert data["added_count"] == 2
    assert data["dataset_ids"] == [1, 2, 3]

    # 成员 :batch-remove
    data = _envelope(client.post(
        f"/api/v1/alerts/eval-sets/{sid}/datasets:batch-remove",
        json={"dataset_ids": [2]},
    ))
    assert data["removed_count"] == 1
    assert data["dataset_ids"] == [1, 3]

    # 空数组 → 400
    assert client.post(f"/api/v1/alerts/eval-sets/{sid}/datasets:batch-add", json={"dataset_ids": []}).status_code == 400

    # 删除 → 204
    assert client.delete(f"/api/v1/alerts/eval-sets/{sid}").status_code == 204
    assert client.get(f"/api/v1/alerts/eval-sets/{sid}").status_code == 404


# ── 分页 ───────────────────────────────────────────────────────────────────────

def test_pagination(client):
    for i in range(5):
        _make_dataset(client, f"p{i}")
    data = _envelope(client.get("/api/v1/alerts/datasets?page=1&page_size=2"))
    assert data["total"] == 5 and len(data["items"]) == 2 and data["has_next"] is True
    data2 = _envelope(client.get("/api/v1/alerts/datasets?page=3&page_size=2"))
    assert len(data2["items"]) == 1 and data2["has_next"] is False


# ── 旧端点弃用标记 ─────────────────────────────────────────────────────────────

def test_legacy_alerts_deprecated(client):
    resp = client.get("/alerts/api/datasets")
    assert resp.headers.get("Deprecation") == "true"
    assert "/api/v1/alerts" in resp.headers.get("Link", "")
    assert 'rel="successor-version"' in resp.headers.get("Link", "")
