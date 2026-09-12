"""od-dataset-manager API 契约测试：路由可用性 + 参数校验 + 404 路径。

用 od_client fixture（临时空 DB，隔离）。断言聚焦结构与状态码，不绑业务值。
覆盖刚提交的新功能路由：overview / qc / cross-qc / evaluate / classify。
"""


def _body(resp):
    return resp.get_json()


def _create_project(client, name="test", mode="detection"):
    r = client.post("/api/projects", json={"name": name, "mode": mode})
    assert r.status_code == 200
    return _body(r)["id"]


class TestPages:
    def test_home(self, od_client):
        assert od_client.get("/").status_code == 200


class TestProjects:
    def test_list_empty(self, od_client):
        r = od_client.get("/api/projects")
        assert r.status_code == 200
        assert _body(r) == []

    def test_create(self, od_client):
        r = od_client.post("/api/projects", json={"name": "test", "mode": "detection"})
        assert r.status_code == 200
        body = _body(r)
        assert "id" in body
        assert body["name"] == "test"
        assert body["mode"] == "detection"

    def test_create_no_name(self, od_client):
        r = od_client.post("/api/projects", json={})
        assert r.status_code == 400
        assert "error" in _body(r)

    def test_list_after_create(self, od_client):
        _create_project(od_client, "p1")
        r = od_client.get("/api/projects")
        assert r.status_code == 200
        assert len(_body(r)) == 1
        assert _body(r)[0]["name"] == "p1"


class TestVersions:
    def test_list_empty(self, od_client):
        pid = _create_project(od_client)
        r = od_client.get(f"/api/versions/{pid}")
        assert r.status_code == 200
        assert _body(r) == []


class TestOverview:
    def test_not_found(self, od_client):
        r = od_client.get("/api/overview/nonexistent")
        assert r.status_code == 404
        assert "error" in _body(r)


class TestQc:
    def test_not_found(self, od_client):
        r = od_client.get("/api/qc/nonexistent")
        assert r.status_code == 404
        assert "error" in _body(r)


class TestCrossQc:
    def test_missing_params(self, od_client):
        # 需 train_version_id + test_version_id
        r = od_client.get("/api/cross-qc/anyproject")
        assert r.status_code == 400
        assert "error" in _body(r)


class TestEvaluate:
    def test_evaluate_no_pred(self, od_client):
        r = od_client.post("/api/evaluate", json={})
        assert "error" in _body(r)

    def test_evaluate_classify_no_csv(self, od_client):
        r = od_client.post("/api/evaluate_classify", json={})
        assert "error" in _body(r)


class TestClassify:
    def test_no_project(self, od_client):
        r = od_client.post("/api/classify/somename", json={})
        assert "error" in _body(r)
