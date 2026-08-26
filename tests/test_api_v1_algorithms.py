"""端到端验证 /api/v1/algorithms 系列端点（算法版本 CRUD + 类型列表 + 下载）。

同步 CRUD + 文件 I/O，无 EasyOCR / 无后台线程 → 全快测，无 slow 用例。
multipart 用 io.BytesIO；DB 为 conftest tmp 库（create_app→init_db 建全 schema +
_seed_event_types 播种，故 algorithm_type="rat" 等已存在，可作合法类型）。
"""
import io
import zipfile


def _data(resp):
    """断言成功信封（2xx + code==0）并返回 data。"""
    assert 200 <= resp.status_code < 300, resp.status_code
    body = resp.get_json()
    assert body["code"] == 0
    return body["data"]


def _err(resp, status, code):
    assert resp.status_code == status, resp.status_code
    assert resp.get_json()["code"] == code


def _create_version(client, algorithm_type="rat", name="rat_v1", filename="test_algo.txt"):
    """建一个带 algorithm_file 的算法版本，返回 version_id。"""
    resp = client.post(
        "/api/v1/algorithms/versions",
        data={
            "algorithm_type": algorithm_type,
            "name": name,
            "version_date": "2026-01-01",
            "description": "test",
            "algorithm_file": (io.BytesIO(b"fake algo content"), filename),
        },
        content_type="multipart/form-data",
    )
    return _data(resp)["id"]


# ── 算法类型列表 ───────────────────────────────────────────────────────────────

def test_list_types(client):
    data = _data(client.get("/api/v1/algorithms/types"))
    assert isinstance(data, list)
    assert "rat" in data  # 播种的类型


# ── 算法版本 CRUD ───────────────────────────────────────────────────────────────

def test_create_version(client):
    vid = _create_version(client)
    assert isinstance(vid, int)


def test_create_version_invalid_type(client):
    resp = client.post(
        "/api/v1/algorithms/versions",
        data={
            "algorithm_type": "not_a_real_type",
            "name": "x",
            "version_date": "2026-01-01",
        },
        content_type="multipart/form-data",
    )
    _err(resp, 400, 10600)


def test_create_version_missing_name(client):
    resp = client.post(
        "/api/v1/algorithms/versions",
        data={"algorithm_type": "rat", "version_date": "2026-01-01"},
        content_type="multipart/form-data",
    )
    _err(resp, 400, 10601)


def test_create_version_missing_date(client):
    resp = client.post(
        "/api/v1/algorithms/versions",
        data={"algorithm_type": "rat", "name": "x"},
        content_type="multipart/form-data",
    )
    _err(resp, 400, 10602)


def test_list_versions(client):
    vid = _create_version(client, name="list_me")
    data = _data(client.get("/api/v1/algorithms/versions"))
    assert data["total"] >= 1
    assert any(v["id"] == vid for v in data["items"])
    assert "datasets" in data["items"][0]  # 每行带关联数据集


def test_get_version_detail(client):
    vid = _create_version(client)
    data = _data(client.get(f"/api/v1/algorithms/versions/{vid}"))
    assert data["version"]["id"] == vid
    assert "datasets" in data
    assert data["config_info"] is None  # 未上传 config_file
    assert data["version"]["algorithm_file_path"]  # algorithm_file 已落库


def test_get_version_not_found(client):
    _err(client.get("/api/v1/algorithms/versions/999999"), 404, 20600)


def test_update_version(client):
    vid = _create_version(client)
    data = _data(client.patch(
        f"/api/v1/algorithms/versions/{vid}",
        data={"name": "renamed", "description": "updated"},
        content_type="multipart/form-data",
    ))
    assert data["id"] == vid
    detail = _data(client.get(f"/api/v1/algorithms/versions/{vid}"))
    assert detail["version"]["name"] == "renamed"
    assert detail["version"]["description"] == "updated"


def test_update_version_not_found(client):
    resp = client.patch(
        "/api/v1/algorithms/versions/999999",
        data={"name": "x"},
        content_type="multipart/form-data",
    )
    _err(resp, 404, 20600)


def test_update_version_no_fields(client):
    """一个字段都不提供 → 10603（修复后可达：description 改存在性检测）。"""
    vid = _create_version(client)
    resp = client.patch(
        f"/api/v1/algorithms/versions/{vid}",
        data={},
        content_type="multipart/form-data",
    )
    _err(resp, 400, 10603)


def test_update_version_description_not_blanked(client):
    """PATCH 只传 name 时 description 不被误清空——锁定修复旧版 is-not-None 怪癖。
    （旧版会把未传的 description 重写为 ""，此用例在旧代码上会失败。）"""
    vid = _create_version(client, name="orig")  # _create_version 传 description="test"
    _data(client.patch(
        f"/api/v1/algorithms/versions/{vid}",
        data={"name": "renamed"},  # 只传 name，不传 description
        content_type="multipart/form-data",
    ))
    detail = _data(client.get(f"/api/v1/algorithms/versions/{vid}"))
    assert detail["version"]["name"] == "renamed"
    assert detail["version"]["description"] == "test"  # 未被清空


def test_delete_version(client):
    vid = _create_version(client)
    resp = client.delete(f"/api/v1/algorithms/versions/{vid}")
    assert resp.status_code == 204
    _err(client.get(f"/api/v1/algorithms/versions/{vid}"), 404, 20600)


def test_delete_version_in_use(client):
    """版本被数据集引用（is_active=1）→ 409 / 30600。"""
    vid = _create_version(client)
    did = client.post("/api/v1/alerts/datasets", json={"name": "ds"}).get_json()["data"]["id"]
    client.post(
        f"/api/v1/alerts/datasets/{did}/algorithm-versions",
        json={"algorithm_version_ids": [vid]},
    )
    _err(client.delete(f"/api/v1/algorithms/versions/{vid}"), 409, 30600)


# ── 文件下载 ───────────────────────────────────────────────────────────────────

def test_download_file(client, tmp_path):
    vid = _create_version(client)
    detail = _data(client.get(f"/api/v1/algorithms/versions/{vid}"))
    path = detail["version"]["algorithm_file_path"]
    resp = client.get("/api/v1/algorithms/download", query_string={"path": path})
    assert resp.status_code == 200
    assert resp.data  # 二进制内容非空


def test_download_missing_path(client):
    _err(client.get("/api/v1/algorithms/download"), 400, 10604)


def test_download_illegal_path(client, tmp_path):
    # tmp_path 自身在 uploads 目录之外 → 非法路径（relative_to 失败）
    _err(
        client.get("/api/v1/algorithms/download", query_string={"path": str(tmp_path / "evil.txt")}),
        400,
        10605,
    )


def test_download_not_exist(client, tmp_path):
    # uploads 内不存在的文件 → 20601
    uploads_algo = tmp_path / "uploads" / "algorithms"
    uploads_algo.mkdir(parents=True, exist_ok=True)
    _err(
        client.get("/api/v1/algorithms/download", query_string={"path": str(uploads_algo / "nope.txt")}),
        404,
        20601,
    )


def test_batch_download(client):
    vid = _create_version(client)
    resp = client.post(
        "/api/v1/algorithms/versions:batch-download",
        json={"ids": [vid], "type": "algorithm"},
    )
    assert resp.status_code == 200
    assert resp.mimetype == "application/zip"
    assert resp.data[:2] == b"PK"  # ZIP 魔术头
    with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
        assert zf.namelist()  # 合法 zip 且非空


def test_batch_download_no_ids(client):
    _err(
        client.post("/api/v1/algorithms/versions:batch-download", json={"ids": [], "type": "all"}),
        400,
        10606,
    )
