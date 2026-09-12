"""端到端验证 /api/v1/review 端点（复核 alerts/gt-context 重写 + ai-check/status 委托）。

测试分层（对齐 plan §测试策略）：
- 委托端点（ai-check/ai-check/status）：_stub_worker（autouse）把 _ai_check_worker 替成
  no-op，避开真 OpenAI 多模态调用/仓库写/Windows 文件锁，只测信封+状态码+错误码+委托真触发
  （batch 落 _ai_batches）。ai-check 成功需 mock get_openai_creds（否则旧视图 step3 返 400）。
- 原位重写端点（alerts/gt-context）：seed eval_tasks/eval_merged_events/eval_gt_events/
  alert_images 行直测；复用 eval_service.get_effective_status（纯读，不算指标）。

DB 为 conftest tmp 库（create_app→init_db 建全 schema 含 eval_merged_events 全列）。
conftest 已 patch app.routes.review.DATABASE_PATH→tmp（双绑定，worker _ai_check_worker
直连此拷贝）。

盲区（bug-audit 另修）：worker 端到端（_review_one 多模态审查/_parse_suggestion 解析）
未覆盖；review 是全仓唯一自带 LLM timeout=120 的调用点，不受 #20 影响。
"""
import json

import pytest

from app.database import get_db
from app.routes import review as _legacy


# ── 辅助 ────────────────────────────────────────────────────────────────────────

def _data(resp):
    assert 200 <= resp.status_code < 300, resp.status_code
    body = resp.get_json()
    assert body["code"] == 0, body
    return body["data"]


def _err(resp, status):
    assert resp.status_code == status, (resp.status_code, resp.get_json())
    body = resp.get_json()
    assert body["code"] == status, body
    return body


# ── fixtures ────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _stub_worker(monkeypatch):
    """autouse：把真 worker _ai_check_worker 替成 no-op，避开真 OpenAI/仓库写/文件锁。"""
    monkeypatch.setattr(_legacy, "_ai_check_worker", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _reset_review_state():
    """autouse：每用例后清模块级 _ai_batches，防跨用例串。"""
    yield
    _legacy._ai_batches.clear()


@pytest.fixture
def _creds(monkeypatch):
    """mock OpenAI creds，使 ai_check 通过旧视图 step3（未配置→400）走到起线程分支。"""
    monkeypatch.setattr(
        "app.services.api_config_service.get_vision_creds",
        lambda: {"api_key": "fake-key", "base_url": "http://localhost/v1", "model": "m"},
    )


@pytest.fixture
def seed(app, tmp_path):
    """eval_tasks + alert_images + eval_merged_events(含 ai_suggestion) + eval_gt_events。"""
    img = tmp_path / "a1.png"
    img.write_bytes(b"fake img")
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        cur.execute("INSERT INTO eval_tasks (id, name, status) VALUES (1, 't1', 'done')")
        cur.execute("INSERT INTO alert_images (id, filename, file_path) VALUES (1, 'a1.png', ?)",
                    (str(img),))
        _insert_merged(cur, id=1, task_id=1, video_id="046-001", event_type="fight",
                       is_false_positive=0, manual_status="auto", image_ids="[1]",
                       representative_image_id=1,
                       ai_suggestion=json.dumps({"verdict": "correct", "reason": "看到打架"}),
                       ts_start=10, ts_end=12)
        cur.execute(
            "INSERT INTO eval_gt_events (id, task_id, video_id, event_type, start_sec, end_sec) "
            "VALUES (1, 1, '046-001', 'fight', 5, 15)"
        )
        db.commit()
    return {"task_id": 1, "merged_id": 1, "video_id": "046-001"}


def _insert_merged(cur, **fields):
    """动态插一条 eval_merged_events（只插提供列；video_id/event_type NOT NULL 必填）。"""
    cols = {"video_id": "046-001", "event_type": "fight"}
    cols.update(fields)
    names = list(cols.keys())
    placeholders = ",".join("?" * len(names))
    cur.execute(
        f"INSERT INTO eval_merged_events ({','.join(names)}) VALUES ({placeholders})",
        [cols[c] for c in names],
    )


# ── 1. alerts（原位重写）──────────────────────────────────────────────────────

def test_alerts_not_found(client, seed):
    _err(client.get("/api/v1/review/tasks/999/alerts"), 404)


def test_alerts_success(client, seed):
    data = _data(client.get("/api/v1/review/tasks/1/alerts"))
    assert data["count"] == 1
    a = data["alerts"][0]
    assert a["video_id"] == "046-001"
    assert a["image_ids"] == [1]                       # JSON 已解析
    assert a["ai_suggestion"] == {"verdict": "correct", "reason": "看到打架"}
    assert a["effective_status"] in ("correct", "false_positive", "ignored", "auto")


def test_alerts_empty(app, client, seed):
    """任务存在但无告警→200 空列表。"""
    with app.app_context():
        db = get_db()
        db.cursor().execute("DELETE FROM eval_merged_events WHERE id = 1")
        db.commit()
    data = _data(client.get("/api/v1/review/tasks/1/alerts"))
    assert data["count"] == 0 and data["alerts"] == []


# ── 2. gt-context（原位重写）──────────────────────────────────────────────────

def test_gt_context_missing_video_id(client, seed):
    _err(client.get("/api/v1/review/tasks/1/gt-context"), 400)


def test_gt_context_success(client, seed):
    data = _data(client.get("/api/v1/review/tasks/1/gt-context?video_id=046-001"))
    assert len(data["gt_events"]) == 1
    assert data["gt_events"][0]["event_type"] == "fight"
    assert data["gt_events"][0]["start_sec"] == 5
    assert len(data["alerts"]) == 1
    assert "effective_status" in data["alerts"][0]


def test_gt_context_task_not_exists_returns_empty(client, seed):
    """旧版不查任务存在性→任务不存在返空 200（对齐语义，不改 404）。"""
    data = _data(client.get("/api/v1/review/tasks/999/gt-context?video_id=046-001"))
    assert data["gt_events"] == [] and data["alerts"] == []


# ── 3. ai-check（委托）──────────────────────────────────────────────────────

def test_ai_check_no_merged_ids(client, seed, _creds):
    _err(client.post("/api/v1/review/tasks/1/ai-check", json={}), 400)


def test_ai_check_task_not_found(client, seed, _creds):
    _err(client.post("/api/v1/review/tasks/999/ai-check", json={"merged_ids": [1]}), 404)


def test_ai_check_no_creds(client, seed, monkeypatch):
    """显式 mock 无 creds→旧视图 step3 返 400 未配置 OpenAI→11402。
    （真实 get_openai_creds 可能读到 os.environ/api_config 表的值，故显式置空以确定性。）"""
    monkeypatch.setattr("app.services.api_config_service.get_vision_creds",
                        lambda: {"api_key": "", "base_url": "", "model": ""})
    _err(client.post("/api/v1/review/tasks/1/ai-check", json={"merged_ids": [1]}), 400)


def test_ai_check_merged_not_found(client, seed, _creds):
    """merged_ids 指向不存在记录→21401。"""
    _err(client.post("/api/v1/review/tasks/1/ai-check", json={"merged_ids": [999]}), 404)


def test_ai_check_success(client, seed, _creds):
    """有效 merged_ids + creds + stub worker→200 + batch_id + total，batch 落 _ai_batches。"""
    resp = client.post("/api/v1/review/tasks/1/ai-check", json={"merged_ids": [1]})
    data = _data(resp)
    assert data["total"] == 1
    batch_id = data["batch_id"]
    assert batch_id in _legacy._ai_batches
    assert _legacy._ai_batches[batch_id]["task_id"] == 1


# ── 4. ai-check/status（委托）────────────────────────────────────────────────

def test_ai_status_missing_batch_id(client, seed):
    _err(client.get("/api/v1/review/tasks/1/ai-check/status"), 400)


def test_ai_status_batch_not_found(client, seed):
    _err(client.get("/api/v1/review/tasks/1/ai-check/status?batch_id=nope"), 404)


def test_ai_status_batch_other_task(client, seed):
    """batch 属于别的 task→21402。"""
    _legacy._ai_batches["b1"] = {"task_id": 999, "status": "running", "total": 1,
                                "done": 0, "current_id": None, "results": [], "error": None}
    _err(client.get("/api/v1/review/tasks/1/ai-check/status?batch_id=b1"), 404)


def test_ai_status_success(client, seed):
    _legacy._ai_batches["b1"] = {"task_id": 1, "status": "done", "total": 2,
                                 "done": 2, "current_id": None,
                                 "results": [{"merged_id": 1, "suggestion": {"verdict": "correct"}}],
                                 "error": None}
    data = _data(client.get("/api/v1/review/tasks/1/ai-check/status?batch_id=b1"))
    assert data["status"] == "done"
    assert data["done"] == 2 and data["total"] == 2
    assert data["results"][0]["merged_id"] == 1
