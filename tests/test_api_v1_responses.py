"""L1 护栏：v1 信封 / 分页 / wrap_old_view 行为单元测试。

目的：在接入 create_app 之前，把 app.api.v1.responses 与 app.api.v1.compat.wrap_old_view
的行为钉死。后续任何模块都用这些基础件，先有护栏再有业务代码。

对应 docs/rest-api-feasibility-and-test-plan.md 的 Layer 1。
"""
import pytest
from flask import jsonify, make_response, redirect

from app.api.v1 import responses as R
from app.api.v1.compat import wrap_old_view


# jsonify / make_response 需要 request 上下文
@pytest.fixture
def req_ctx(app_ctx):
    with app_ctx.test_request_context():
        yield


def _resp(rv):
    """把视图返回值（Response / tuple / dict）统一成 Response 以便断言。"""
    return make_response(rv)


# ===== 信封形状 =====
class TestEnvelope:
    def test_ok(self, req_ctx):
        r = _resp(R.ok({"id": 1}))
        assert r.status_code == 200
        assert r.get_json() == {"code": 0, "message": "ok", "data": {"id": 1}}

    def test_ok_data_none(self, req_ctx):
        r = _resp(R.ok())
        body = r.get_json()
        assert body["code"] == 0
        assert body["data"] is None

    def test_created(self, req_ctx):
        r = _resp(R.created({"id": 5}, location="/api/v1/videos/5"))
        assert r.status_code == 201
        assert r.headers["Location"] == "/api/v1/videos/5"
        body = r.get_json()
        assert body["code"] == 0
        assert body["message"] == "created"
        assert body["data"] == {"id": 5}

    def test_accepted(self, req_ctx):
        r = _resp(R.accepted(location="/api/v1/tasks/9/status"))
        assert r.status_code == 202
        assert r.headers["Location"] == "/api/v1/tasks/9/status"

    def test_no_content(self, req_ctx):
        r = _resp(R.no_content())
        assert r.status_code == 204
        assert r.data == b""

    def test_err(self, req_ctx):
        r = _resp(R.err(404, "不存在"))
        assert r.status_code == 404
        assert r.get_json() == {"code": 404, "message": "不存在"}

    def test_err_with_errors(self, req_ctx):
        r = _resp(R.err(400, "参数错误", errors=[{"field": "page"}]))
        assert r.status_code == 400
        body = r.get_json()
        assert body["code"] == 400
        assert body["errors"] == [{"field": "page"}]

    def test_err_with_error_code(self, req_ctx):
        # 方案3：业务码进 error_code，HTTP 码保持标准
        r = _resp(R.err(400, "数据集名称不能为空", error_code="DATASET_NAME_EMPTY"))
        assert r.status_code == 400
        body = r.get_json()
        assert body["code"] == 400
        assert body["error_code"] == "DATASET_NAME_EMPTY"

    def test_err_without_error_code_backward_compat(self, req_ctx):
        # 不传 error_code 时 body 不含该键（旧两参/三参调用形态不变）
        r = _resp(R.err(404, "不存在"))
        body = r.get_json()
        assert "error_code" not in body
        assert body == {"code": 404, "message": "不存在"}


# ===== 分页解析 =====
class TestPagination:
    def _parse(self, **kw):
        return R.parse_pagination(kw)

    def test_defaults(self):
        assert self._parse() == (1, 20)

    def test_page_below_one_clamps(self):
        assert self._parse(page=0) == (1, 20)
        assert self._parse(page=-3) == (1, 20)

    def test_page_size_over_max_clamps(self):
        assert self._parse(page_size=999) == (1, 100)

    def test_page_size_zero_clamps(self):
        assert self._parse(page_size=0) == (1, 1)

    def test_non_numeric_falls_back(self):
        assert self._parse(page="abc", page_size="x") == (1, 20)

    def test_empty_string_falls_back(self):
        assert self._parse(page="", page_size="") == (1, 20)

    def test_valid(self):
        assert self._parse(page=3, page_size=50) == (3, 50)

    def test_paginate_shape(self):
        p = R.paginate([1, 2], total=10, page=1, page_size=2)
        assert p == {
            "items": [1, 2],
            "total": 10,
            "page": 1,
            "page_size": 2,
            "has_next": True,
        }

    def test_paginate_has_next_false_at_end(self):
        p = R.paginate([9, 10], total=10, page=5, page_size=2)
        assert p["has_next"] is False

    def test_paginate_empty(self):
        p = R.paginate([], total=0, page=1, page_size=20)
        assert p["has_next"] is False
        assert p["items"] == []


# ===== wrap_old_view 行为矩阵 =====
class TestWrapOldView:
    """对应可行性文档 L1 矩阵表。"""

    def _call(self, old_view):
        """包装并在 request 上下文内调用，返回 Response。"""
        wrapped = wrap_old_view(old_view)
        return _resp(wrapped())

    def test_json_dict_success(self, req_ctx):
        def old():
            return jsonify({"video": {"id": 1}})

        r = self._call(old)
        assert r.status_code == 200
        assert r.get_json() == {"code": 0, "message": "ok", "data": {"video": {"id": 1}}}

    def test_plain_dict_return(self, req_ctx):
        def old():
            return {"video": {"id": 1}}

        r = self._call(old)
        assert r.status_code == 200
        assert r.get_json()["data"] == {"video": {"id": 1}}

    def test_list_verbatim_under_data(self, req_ctx):
        # 列表原样放进 data——包装器不做假分页（分页须重实现）
        def old():
            return jsonify([1, 2, 3])

        r = self._call(old)
        assert r.status_code == 200
        assert r.get_json()["data"] == [1, 2, 3]

    def test_error_tuple(self, req_ctx):
        def old():
            return jsonify({"error": "不存在"}), 404

        r = self._call(old)
        assert r.status_code == 404
        assert r.get_json() == {"code": 404, "message": "不存在"}

    def test_error_message_field(self, req_ctx):
        def old():
            return jsonify({"message": "参数错误"}), 400

        r = self._call(old)
        assert r.status_code == 400
        assert r.get_json()["message"] == "参数错误"

    def test_created_status_preserved(self, req_ctx):
        def old():
            return jsonify({"id": 5}), 201

        r = self._call(old)
        assert r.status_code == 201
        body = r.get_json()
        assert body["code"] == 0
        assert body["data"] == {"id": 5}

    def test_binary_passthrough(self, req_ctx):
        def old():
            resp = make_response(b"\x00\x01binary")
            resp.headers["Content-Type"] = "video/mp4"
            return resp

        r = self._call(old)
        assert r.status_code == 200
        assert r.data == b"\x00\x01binary"
        assert r.content_type.startswith("video/mp4")

    def test_redirect_passthrough(self, req_ctx):
        def old():
            return redirect("/elsewhere")

        r = self._call(old)
        assert r.status_code == 302
        assert r.headers["Location"] == "/elsewhere"

    def test_none_returns_no_content(self, req_ctx):
        def old():
            return None

        r = self._call(old)
        assert r.status_code == 204
        assert r.data == b""

    def test_none_204_tuple(self, req_ctx):
        def old():
            return None, 204

        r = self._call(old)
        assert r.status_code == 204
