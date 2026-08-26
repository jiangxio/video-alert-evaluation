"""端到端验证 /api/v1/event-types 系列端点（事件类型 CRUD + 引用计数）。

DB 为 conftest tmp 库（create_app→init_db 建全 schema + _seed_event_types 播种 ~17 条，
故 "rat" 等已存在，可用于重复 key/id 测试）。create/update/delete 后 _sync_alert_types_json
写到 tmp（conftest 把 ALERT_TYPES_CONFIG_PATH 重定向到 tmp），不碰真实 config/alert_types.json。
全快测，无 slow 用例。
"""
import io


def _data(resp):
    assert 200 <= resp.status_code < 300, resp.status_code
    body = resp.get_json()
    assert body["code"] == 0
    return body["data"]


def _err(resp, status, code):
    assert resp.status_code == status, resp.status_code
    assert resp.get_json()["code"] == code


def _create_et(client, key="new_type", name="新类型", **extra):
    body = {"key": key, "name": name}
    body.update(extra)
    return _data(client.post("/api/v1/event-types", json=body))


def _seeded_id(client, key="rat"):
    """取某已播种类型的 id（用于重复 id / references / delete 测试）。"""
    data = _data(client.get("/api/v1/event-types", query_string={"page_size": 100}))
    for item in data["items"]:
        if item["key"] == key:
            return item["id"]
    raise AssertionError(f"播种类型 {key} 未找到")


# ── 列表 ───────────────────────────────────────────────────────────────────────

def test_list_event_types(client):
    data = _data(client.get("/api/v1/event-types"))
    assert data["total"] > 0
    assert len(data["items"]) > 0
    assert isinstance(data["items"][0]["tags"], list)  # tags 已解析为数组


# ── 新增 ───────────────────────────────────────────────────────────────────────

def test_create_event_type(client):
    et = _create_et(client, key="my_test_type", name="我的测试类型")
    assert et["key"] == "my_test_type"
    assert isinstance(et["id"], int)
    data = _data(client.get("/api/v1/event-types", query_string={"page_size": 100}))
    assert any(i["key"] == "my_test_type" for i in data["items"])


def test_create_event_type_duplicate_key(client):
    """key=rat 已播种 → 409 / 30700。"""
    _err(client.post("/api/v1/event-types", json={"key": "rat", "name": "鼠"}), 409, 30700)


def test_create_event_type_duplicate_id(client):
    """显式指定已占用的 id → 409 / 30701。"""
    existing_id = _seeded_id(client)
    resp = client.post(
        "/api/v1/event-types",
        json={"key": "uniq_key_for_id_test", "name": "x", "id": existing_id},
    )
    _err(resp, 409, 30701)


def test_create_event_type_missing_key(client):
    _err(client.post("/api/v1/event-types", json={"name": "n"}), 400, 10700)


def test_create_event_type_missing_name(client):
    _err(client.post("/api/v1/event-types", json={"key": "k_only"}), 400, 10701)


def test_create_event_type_invalid_key(client):
    # 含 "-" 非法（只允许字母/数字/下划线）
    _err(client.post("/api/v1/event-types", json={"key": "bad-key", "name": "n"}), 400, 10702)


def test_create_event_type_tags_not_array(client):
    _err(
        client.post("/api/v1/event-types", json={"key": "tagbad", "name": "n", "tags": "notlist"}),
        400,
        10703,
    )


# ── 修改 ───────────────────────────────────────────────────────────────────────

def test_update_event_type(client):
    et = _create_et(client, key="to_update", name="原名")
    data = _data(client.patch(
        f"/api/v1/event-types/{et['id']}",
        json={"name": "新名", "bg_color": "#ffffff"},
    ))
    assert data["id"] == et["id"]
    items = _data(client.get("/api/v1/event-types", query_string={"page_size": 100}))["items"]
    item = next(i for i in items if i["id"] == et["id"])
    assert item["name"] == "新名"
    assert item["bg_color"] == "#ffffff"


def test_update_event_type_not_found(client):
    _err(client.patch("/api/v1/event-types/999999", json={"name": "x"}), 404, 20700)


def test_update_event_type_no_fields(client):
    et = _create_et(client, key="no_update", name="n")
    _err(client.patch(f"/api/v1/event-types/{et['id']}", json={}), 400, 10706)


# ── 引用计数 ───────────────────────────────────────────────────────────────────

def test_get_references(client):
    """建一个事件类型 + 引用它的算法版本 → references total>=1。"""
    et = _create_et(client, key="reftest", name="引用测试")
    client.post(
        "/api/v1/algorithms/versions",
        data={
            "algorithm_type": "reftest",
            "name": "refv",
            "version_date": "2026-01-01",
            "algorithm_file": (io.BytesIO(b"x"), "a.txt"),
        },
        content_type="multipart/form-data",
    )
    data = _data(client.get(f"/api/v1/event-types/{et['id']}/references"))
    assert data["key"] == "reftest"
    assert data["total"] >= 1
    assert data["refs"]["algorithm_versions"] >= 1


def test_get_references_not_found(client):
    _err(client.get("/api/v1/event-types/999999/references"), 404, 20700)


# ── 删除 ───────────────────────────────────────────────────────────────────────

def test_delete_event_type(client):
    et = _create_et(client, key="to_delete", name="待删")
    resp = client.delete(f"/api/v1/event-types/{et['id']}")
    assert resp.status_code == 204
    items = _data(client.get("/api/v1/event-types", query_string={"page_size": 100}))["items"]
    assert not any(i["id"] == et["id"] for i in items)


def test_delete_event_type_not_found(client):
    _err(client.delete("/api/v1/event-types/999999"), 404, 20700)


def test_delete_event_type_with_refs(client):
    """事件类型被算法版本引用 → 409 / 30702。"""
    et = _create_et(client, key="delreftest", name="引用删")
    client.post(
        "/api/v1/algorithms/versions",
        data={
            "algorithm_type": "delreftest",
            "name": "drv",
            "version_date": "2026-01-01",
            "algorithm_file": (io.BytesIO(b"x"), "a.txt"),
        },
        content_type="multipart/form-data",
    )
    _err(client.delete(f"/api/v1/event-types/{et['id']}"), 409, 30702)
