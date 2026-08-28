"""端到端验证 /api/v1/evaluation 端点（评测任务+测前分析+报告，36 端点）。

测试分层（对齐 plan §测试策略 + D3）：
- 原位重写端点（23）：seed eval_tasks/eval_video_sets/videos/eval_merged_events/eval_gt_events/
  pre_analysis_records/report_chat_sessions 行直测；复用 _legacy helper + service 纯函数。
- 委托端点（13）：execute 用 _fake_thread（patch threading.Thread 不跑 worker 闭包）；
  finalize/get_results/get_event_metrics 用 _stub_metrics（compute_task_metrics→canned，避开真指标计算）；
  detailed-report:preview/:chat 用 _stub_claude+_creds；report-image/detailed-report/pdf 测 404/409 错误路径
  （成功路径依赖 Pillow/Playwright，跳过）；confirm/unconfirm/eval_status/sync-gt 委托旧视图。

DB 为 conftest tmp 库。conftest 已 patch app.routes.evaluation.DATABASE_PATH→tmp（双绑定，execute worker 直连）。

最高原则：指标算法（命中判定/compute_task_metrics/get_effective_status 函数体）只调不改——委托端点
不断言指标值正确性，只验信封/状态码/错误码/委托真触发。#8/#18/#19/#20/低危 evaluated_at 由 bug-audit 另修。
"""
import json
from pathlib import Path

import pytest

from app.database import get_db
from app.routes import evaluation as _legacy


# ── 辅助 ────────────────────────────────────────────────────────────────────────

def _data(resp):
    assert 200 <= resp.status_code < 300, (resp.status_code, resp.get_json())
    body = resp.get_json()
    assert body["code"] == 0, body
    return body["data"]


def _err(resp, status):
    assert resp.status_code == status, (resp.status_code, resp.get_json())
    body = resp.get_json()
    assert body["code"] == status, body
    return body


def _insert_task(app, **fields):
    cols = {"name": "t1", "status": "created", "eval_set_id": 1}
    cols.update(fields)
    names, vals = list(cols.keys()), list(cols.values())
    placeholders = ",".join("?" * len(names))
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        cur.execute(f"INSERT INTO eval_tasks ({','.join(names)}) VALUES ({placeholders})", vals)
        db.commit()
        return cur.lastrowid


def _insert_eval_set(app, set_id=1, video_ids=None, name="set1"):
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        cur.execute("INSERT INTO eval_video_sets (id, name, video_ids) VALUES (?, ?, ?)",
                    (set_id, name, json.dumps(video_ids or [1])))
        db.commit()


def _insert_video(app, vid=1, video_id="046-001", duration=10.0):
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        cur.execute("INSERT INTO videos (id, filename, original_path, video_id, duration) VALUES (?, ?, ?, ?, ?)",
                    (vid, f"v{vid}.mp4", f"orig/v{vid}.mp4", video_id, duration))
        db.commit()


def _insert_merged(app, **fields):
    cols = {"task_id": 1, "video_id": "046-001", "event_type": "fight", "is_false_positive": 0,
            "manual_status": "auto", "image_ids": "[]", "ts_start": 5, "ts_end": 8}
    cols.update(fields)
    names, vals = list(cols.keys()), list(cols.values())
    placeholders = ",".join("?" * len(names))
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        cur.execute(f"INSERT INTO eval_merged_events ({','.join(names)}) VALUES ({placeholders})", vals)
        db.commit()
        return cur.lastrowid


def _insert_gt(app, **fields):
    cols = {"task_id": 1, "video_id": "046-001", "event_type": "fight",
            "start_sec": 0, "end_sec": 10, "expected_count": 1, "confirmed_count": 1, "actual_count": 1}
    cols.update(fields)
    names, vals = list(cols.keys()), list(cols.values())
    placeholders = ",".join("?" * len(names))
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        cur.execute(f"INSERT INTO eval_gt_events ({','.join(names)}) VALUES ({placeholders})", vals)
        db.commit()
        return cur.lastrowid


# ── fixtures ────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_eval():
    """autouse：清模块级 _eval_progress，防跨用例串。"""
    yield
    _legacy._eval_progress.clear()


@pytest.fixture
def _fake_thread(monkeypatch):
    """patch threading.Thread 不跑 execute 的 worker 闭包（worker 内联命中判定+compute_task_metrics，绝不跑）。"""
    class _Fake:
        def __init__(self, *a, **k):
            pass
        def start(self):
            pass
    monkeypatch.setattr("app.routes.evaluation.threading.Thread", _Fake)


@pytest.fixture
def _stub_metrics(monkeypatch):
    """compute_task_metrics→canned，避开真指标计算（#8 范畴）。"""
    canned = (0.9, 0.8, 1.5,
              [{"event_type": "fight", "alert_count": 1, "gt_count": 1, "correct_pred_count": 1,
                "false_positive_count": 0, "hit_count": 1, "missed_gt_count": 0}], 3600)
    monkeypatch.setattr("app.routes.evaluation.compute_task_metrics", lambda *a, **k: canned)
    monkeypatch.setattr("app.routes.evaluation.compute_overall_avg_fp", lambda em: 1.5)


@pytest.fixture
def _stub_claude(monkeypatch):
    """_call_claude/_call_claude_chat→canned + get_claude_creds→fake，避开真 LLM（#20 无 timeout 盲区）。
    注：detailed_report_preview/chat 在函数内 `from app.services import api_config_service`，
    故 patch 真模块的 get_claude_creds（函数内 import 读 patched 模块属性）。"""
    monkeypatch.setattr("app.services.api_config_service.get_claude_creds",
                        lambda: {"auth_token": "fake", "base_url": "http://x"})
    monkeypatch.setattr("app.services.eval_service._call_claude", lambda *a, **k: "canned summary")
    monkeypatch.setattr("app.services.eval_service._call_claude_chat",
                        lambda *a, **k: {"summary": "s", "conclusion": "c"})


@pytest.fixture
def seed(app):
    """eval_video_set(1, video_ids=[1]) + videos(1) + eval_tasks(1, eval_set_id=1, status=created)。"""
    _insert_eval_set(app)
    _insert_video(app)
    _insert_task(app, id=1, name="t1", status="created", eval_set_id=1)
    return {"task_id": 1, "set_id": 1, "video_id": "046-001"}


# ── 1. 任务查询（重写）────────────────────────────────────────────────────────

def test_list_tasks_empty(client):
    data = _data(client.get("/api/v1/evaluation/tasks"))
    assert data["total"] == 0


def test_list_tasks_with_data(client, seed):
    data = _data(client.get("/api/v1/evaluation/tasks"))
    assert data["total"] == 1
    assert data["items"][0]["name"] == "t1"
    assert "algorithm_versions" in data["items"][0]


def test_list_tasks_pagination(app, client, seed):
    for i in range(25):
        _insert_task(app, name=f"t{i}")
    data = _data(client.get("/api/v1/evaluation/tasks?page_size=20"))
    assert data["total"] == 26 and len(data["items"]) == 20 and data["has_next"] is True


def test_get_task_not_found(client):
    _err(client.get("/api/v1/evaluation/tasks/999"), 404)


def test_get_task_success(client, seed):
    data = _data(client.get("/api/v1/evaluation/tasks/1"))
    assert data["name"] == "t1"


def test_get_task_status_delegate(app, client, seed):
    """eval_status 委托：无运行→404(21311)。"""
    _err(client.get("/api/v1/evaluation/tasks/1/status"), 404)


def test_get_task_status_running(app, client, seed):
    _legacy._eval_progress[1] = {"total": 2, "done": 1, "running": True}
    data = _data(client.get("/api/v1/evaluation/tasks/1/status"))
    assert data["running"] is True and data["done"] == 1


def test_check_updates_unknown_returns_false(client, seed):
    """eval_tasks 无 updated_at 列（ALTER 未加）→旧 `SELECT updated_at` 永抛 OperationalError→
    except 分支返 200 has_updates False（旧既有行为，404 不可达；忠实复刻，属 bug-audit 低危范畴）。"""
    data = _data(client.get("/api/v1/evaluation/tasks/999/check-updates"))
    assert data["has_updates"] is False


def test_check_updates_no_evaluated_at(app, client, seed):
    """updated_at 为空→has_updates:false。"""
    data = _data(client.get("/api/v1/evaluation/tasks/1/check-updates"))
    assert data["has_updates"] is False


# ── 2. 任务 CRUD（重写）────────────────────────────────────────────────────────

def test_create_task_no_name(client, seed):
    _err(client.post("/api/v1/evaluation/tasks", json={}), 400)


def test_create_task_no_dataset(app, client):
    _err(client.post("/api/v1/evaluation/tasks", json={"name": "x"}), 400)


def test_create_task_success(app, client, seed):
    """realtime 模式需 dataset；此处用普通模式：eval_set_id + alert_eval_set_id。"""
    with app.app_context():
        db = get_db()
        db.cursor().execute("INSERT INTO eval_alert_sets (id, name) VALUES (1, 'aes1')")
        db.commit()
    resp = client.post("/api/v1/evaluation/tasks", json={
        "name": "new", "eval_set_id": 1, "alert_eval_set_id": 1,
    })
    data = _data(resp)
    assert data["task"]["name"] == "new"
    assert resp.status_code == 201
    assert resp.headers["Location"].endswith(f"/api/v1/evaluation/tasks/{data['task']['id']}")


def test_create_task_eval_set_not_found(app, client, seed):
    _err(client.post("/api/v1/evaluation/tasks", json={"name": "x", "dataset_id": 1}), 404)


def test_clone_task_not_found(client, seed):
    _err(client.post("/api/v1/evaluation/tasks/999:clone", json={}), 404)


def test_clone_task_success(app, client, seed):
    resp = client.post("/api/v1/evaluation/tasks/1:clone", json={"name": "copy"})
    data = _data(resp)
    assert resp.status_code == 201
    assert data["task"]["name"] == "copy"


def test_update_task_patch(app, client, seed):
    data = _data(client.patch("/api/v1/evaluation/tasks/1", json={"trigger_rate": 0.7}))
    assert data["task"]["trigger_rate"] == 0.7


def test_update_task_not_found(client, seed):
    _err(client.patch("/api/v1/evaluation/tasks/999", json={"trigger_rate": 0.7}), 404)


def test_delete_task_success(app, client, seed):
    resp = client.delete("/api/v1/evaluation/tasks/1")
    assert resp.status_code == 204
    with app.app_context():
        cur = get_db().cursor()
        cur.execute("SELECT id FROM eval_tasks WHERE id = 1")
        assert cur.fetchone() is None


def test_delete_task_not_found(client, seed):
    _err(client.delete("/api/v1/evaluation/tasks/999"), 404)


def test_analyze_not_found(client, seed):
    _err(client.post("/api/v1/evaluation/tasks/999:analyze"), 404)


def test_analyze_success(app, client, seed, monkeypatch):
    """复用 analyze_merged_events 纯函数（mock 返 canned，不验内部）。"""
    monkeypatch.setattr("app.api.v1.evaluation.analyze_merged_events",
                        lambda tid, db: {"merged_count": 1})
    data = _data(client.post("/api/v1/evaluation/tasks/1:analyze"))
    assert data["merged_count"] == 1


# ── 3. 人工状态 / GT 计数（重写 PATCH）────────────────────────────────────────

def test_update_manual_status_invalid(app, client, seed):
    _err(client.patch("/api/v1/evaluation/tasks/1/merged-events/1/status",
                       json={"manual_status": "bad"}), 400)


def test_update_manual_status_success(app, client, seed):
    mid = _insert_merged(app, task_id=1)
    data = _data(client.patch(f"/api/v1/evaluation/tasks/1/merged-events/{mid}/status",
                              json={"manual_status": "correct"}))
    assert data["manual_status"] == "correct"


def test_update_manual_status_record_not_found(app, client, seed):
    _err(client.patch("/api/v1/evaluation/tasks/1/merged-events/999/status",
                      json={"manual_status": "correct"}), 404)


def test_batch_status_invalid(app, client, seed):
    _err(client.patch("/api/v1/evaluation/tasks/1/merged-events:batch-status",
                      json={"manual_status": "bad", "merged_ids": [1]}), 400)


def test_batch_status_no_ids(app, client, seed):
    _err(client.patch("/api/v1/evaluation/tasks/1/merged-events:batch-status",
                      json={"manual_status": "correct", "merged_ids": []}), 400)


def test_batch_status_success(app, client, seed):
    m1 = _insert_merged(app, task_id=1)
    data = _data(client.patch("/api/v1/evaluation/tasks/1/merged-events:batch-status",
                              json={"manual_status": "false_positive", "merged_ids": [m1]}))
    assert data["updated_count"] == 1


def test_update_gt_counts_no_fields(app, client, seed):
    _err(client.patch("/api/v1/evaluation/tasks/1/gt-events/1", json={}), 400)


def test_update_gt_counts_success(app, client, seed):
    gid = _insert_gt(app, task_id=1)
    data = _data(client.patch(f"/api/v1/evaluation/tasks/1/gt-events/{gid}",
                              json={"confirmed_count": 2, "actual_count": 1}))
    assert data == {} or data is not None


def test_update_gt_counts_record_not_found(app, client, seed):
    _err(client.patch("/api/v1/evaluation/tasks/1/gt-events/999",
                      json={"confirmed_count": 2}), 404)


# ── 4. 状态变更（委托）──────────────────────────────────────────────────────

def test_execute_success(app, client, seed, _fake_thread):
    """execute 委托：_fake_thread 不跑 worker；200 + status=evaluating + _eval_progress.running。"""
    _insert_merged(app, task_id=1)
    _insert_gt(app, task_id=1)
    data = _data(client.post("/api/v1/evaluation/tasks/1:execute"))
    assert _legacy._eval_progress.get(1, {}).get("running") is True
    with app.app_context():
        cur = get_db().cursor()
        cur.execute("SELECT status FROM eval_tasks WHERE id = 1")
        assert cur.fetchone()["status"] == "evaluating"


def test_execute_conflict(app, client, seed, _fake_thread):
    _legacy._eval_progress[1] = {"running": True, "total": 0, "done": 0}
    _err(client.post("/api/v1/evaluation/tasks/1:execute"), 409)


def test_execute_not_found(client, seed, _fake_thread):
    _err(client.post("/api/v1/evaluation/tasks/999:execute"), 404)


def test_finalize_not_done(app, client, seed, _stub_metrics):
    """status≠done→409(31301)。"""
    _err(client.post("/api/v1/evaluation/tasks/1:finalize"), 409)


def test_finalize_success(app, client, seed, _stub_metrics):
    _insert_task(app, id=2, name="t2", status="done", eval_set_id=1)
    _insert_merged(app, task_id=2)
    data = _data(client.post("/api/v1/evaluation/tasks/2:finalize"))
    assert "accuracy" in data
    with app.app_context():
        cur = get_db().cursor()
        cur.execute("SELECT finalized FROM eval_tasks WHERE id = 2")
        assert cur.fetchone()["finalized"] == 1


def test_unconfirm_not_finalized(app, client, seed):
    _err(client.post("/api/v1/evaluation/tasks/1:unconfirm"), 409)


def test_unconfirm_success(app, client, seed):
    _insert_task(app, id=2, name="t2", status="done", finalized=1, eval_set_id=1)
    _data(client.post("/api/v1/evaluation/tasks/2:unconfirm"))
    with app.app_context():
        cur = get_db().cursor()
        cur.execute("SELECT finalized FROM eval_tasks WHERE id = 2")
        assert cur.fetchone()["finalized"] == 0


def test_confirm_success(app, client, seed):
    """confirm 委托：sync INSERT merged+gt，status=confirming。"""
    resp = client.post("/api/v1/evaluation/tasks/1:confirm", json={
        "merged_alerts": [{"video_id": "046-001", "event_type": "fight", "image_ids": [],
                            "ts_start": 5, "ts_end": 8}],
        "gt_events": [{"video_id": "046-001", "event_type": "fight",
                       "start_sec": 0, "end_sec": 10, "expected_count": 1}],
    })
    assert resp.status_code == 200
    with app.app_context():
        cur = get_db().cursor()
        cur.execute("SELECT status FROM eval_tasks WHERE id = 1")
        assert cur.fetchone()["status"] == "confirming"


# ── 5. 结果/指标（委托，stub metrics）────────────────────────────────────────

def test_get_results_not_found(client, seed):
    _err(client.get("/api/v1/evaluation/tasks/999/results"), 404)


def test_get_results_success(app, client, seed, _stub_metrics):
    _insert_merged(app, task_id=1)
    _insert_gt(app, task_id=1)
    data = _data(client.get("/api/v1/evaluation/tasks/1/results"))
    assert "alert_results" in data and "gt_results" in data


def test_get_event_metrics_success(app, client, seed, _stub_metrics):
    _insert_merged(app, task_id=1)
    data = _data(client.get("/api/v1/evaluation/tasks/1/event-metrics"))
    assert "event_metrics" in data and "overall" in data


# ── 6. 报告（委托）────────────────────────────────────────────────────────────

def test_report_image_not_found(client, seed):
    _err(client.get("/api/v1/evaluation/tasks/999/report/image"), 404)


def test_report_not_done(app, client, seed):
    """detailed-report：status 非 done/finalized→409(31303)。POST 须带 json={} 否则 get_json 抛 415。"""
    _err(client.post("/api/v1/evaluation/tasks/1/report", json={}), 409)


def test_report_pdf_not_done(app, client, seed):
    _err(client.post("/api/v1/evaluation/tasks/1/report/pdf", json={}), 409)


def test_report_preview_success(app, client, seed, _stub_claude, _stub_metrics):
    _insert_task(app, id=2, name="t2", status="done", finalized=1, eval_set_id=1,
                 accuracy=0.9, recall=0.8, avg_fp_per_hour=1.5, event_metrics="[]")
    data = _data(client.post("/api/v1/evaluation/tasks/2/report:preview", json={}))
    assert "summary" in data and "conclusion" in data


def test_report_preview_no_api_key(app, client, seed, monkeypatch):
    """无 creds 且 body 无 api_key→400(11310)。"""
    monkeypatch.setattr("app.services.api_config_service.get_claude_creds",
                        lambda: {"auth_token": None, "base_url": None})
    _err(client.post("/api/v1/evaluation/tasks/1/report:preview", json={}), 400)


def test_report_chat_success(app, client, seed, _stub_claude, _stub_metrics):
    _insert_task(app, id=2, name="t2", status="done", finalized=1, eval_set_id=1, event_metrics="[]")
    data = _data(client.post("/api/v1/evaluation/tasks/2/report:chat",
                              json={"messages": [], "current_summary": "", "current_conclusion": ""}))
    assert "summary" in data


# ── 7. GT 帧 / GT 同步（重写/委托）────────────────────────────────────────────

def test_serve_gt_frame_not_found(client, seed):
    _err(client.get("/api/v1/evaluation/gt-frames/999/file"), 404)


def test_serve_gt_frame_success(app, client, seed, tmp_path):
    img = tmp_path / "frame.jpg"
    img.write_bytes(b"fake jpg")
    with app.app_context():
        db = get_db()
        db.cursor().execute(
            "INSERT INTO gt_frames (id, video_db_id, event_type, timestamp_sec, file_path, filename) "
            "VALUES (1, 1, 'fight', 5.0, ?, 'frame.jpg')", (str(img),))
        db.commit()
    resp = client.get("/api/v1/evaluation/gt-frames/1/file")
    assert resp.status_code == 200


def test_sync_gt_no_video_id(client, seed):
    _err(client.post("/api/v1/evaluation/gt:sync", json={"direction": "db_to_gt"}), 400)


def test_sync_gt_bad_direction(app, client, seed):
    _err(client.post("/api/v1/evaluation/gt:sync", json={"video_db_id": 1, "direction": "bad"}), 400)


def test_sync_gt_video_not_found(client, seed):
    _err(client.post("/api/v1/evaluation/gt:sync", json={"video_db_id": 999, "direction": "db_to_gt"}), 404)


def test_sync_gt_db_to_gt_success(app, client, seed):
    """db_to_gt：以 DB events 生成 GT 文件（写 GROUND_TRUTH_DIR，已 patch tmp）。"""
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        cur.execute("INSERT INTO events (video_db_id, event_type, start_seconds, end_seconds) VALUES (1, 'fight', 0, 5)")
        db.commit()
    data = _data(client.post("/api/v1/evaluation/gt:sync", json={"video_db_id": 1, "direction": "db_to_gt"}))
    assert data["event_count"] == 1


# ── 8. 测前分析（重写 CRUD）────────────────────────────────────────────────────

def test_create_pre_analysis_no_set(client, seed):
    _err(client.post("/api/v1/evaluation/pre-analysis", json={}), 400)


def test_create_pre_analysis_success(app, client, seed, monkeypatch):
    """复用 _legacy._run_pre_analysis（mock 避开真分析逻辑）。"""
    monkeypatch.setattr("app.api.v1.evaluation._legacy._run_pre_analysis",
                        lambda *a, **k: {"event_type_stats": {}, "total_video_duration": 10.0})
    resp = client.post("/api/v1/evaluation/pre-analysis", json={"eval_video_set_id": 1})
    data = _data(resp)
    assert resp.status_code == 201
    assert "result" in data


def test_list_pre_analysis_empty(client, seed):
    data = _data(client.get("/api/v1/evaluation/pre-analysis"))
    assert data["total"] == 0


def test_list_pre_analysis_success(app, client, seed):
    with app.app_context():
        db = get_db()
        db.cursor().execute(
            "INSERT INTO pre_analysis_records (eval_video_set_id, merge_interval_sec, event_interval_sec, "
            "trigger_rate, min_event_duration_sec, result_json) VALUES (1, 5, 10, 0.5, 0, '{}')",
        )
        db.commit()
    data = _data(client.get("/api/v1/evaluation/pre-analysis"))
    assert data["total"] == 1
    assert "result" in data["items"][0]
    assert "result_json" not in data["items"][0]


def test_get_pre_analysis_not_found(client, seed):
    _err(client.get("/api/v1/evaluation/pre-analysis/999"), 404)


def test_get_pre_analysis_success(app, client, seed):
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        cur.execute(
            "INSERT INTO pre_analysis_records (eval_video_set_id, merge_interval_sec, event_interval_sec, "
            "trigger_rate, min_event_duration_sec, result_json) VALUES (1, 5, 10, 0.5, 0, '{}')",
        )
        db.commit()
        rid = cur.lastrowid
    data = _data(client.get(f"/api/v1/evaluation/pre-analysis/{rid}"))
    assert "result" in data


def test_list_pre_analysis_by_set(app, client, seed):
    with app.app_context():
        db = get_db()
        db.cursor().execute(
            "INSERT INTO pre_analysis_records (eval_video_set_id, merge_interval_sec, event_interval_sec, "
            "trigger_rate, min_event_duration_sec, result_json) VALUES (1, 5, 10, 0.5, 0, '{}')",
        )
        db.commit()
    data = _data(client.get("/api/v1/evaluation/pre-analysis:by-set/1"))
    assert data["total"] == 1


def test_delete_pre_analysis_success(app, client, seed):
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        cur.execute(
            "INSERT INTO pre_analysis_records (eval_video_set_id, merge_interval_sec, event_interval_sec, "
            "trigger_rate, min_event_duration_sec, result_json) VALUES (1, 5, 10, 0.5, 0, '{}')",
        )
        db.commit()
        rid = cur.lastrowid
    assert client.delete(f"/api/v1/evaluation/pre-analysis/{rid}").status_code == 204


def test_delete_pre_analysis_not_found(client, seed):
    _err(client.delete("/api/v1/evaluation/pre-analysis/999"), 404)


# ── 9. 评测视频集（重写）──────────────────────────────────────────────────────

def test_list_eval_sets_success(client, seed):
    data = _data(client.get("/api/v1/evaluation/eval-sets"))
    assert data["total"] == 1
    assert data["items"][0]["video_count"] == 1


def test_list_eval_sets_with_analysis_count(client, seed):
    data = _data(client.get("/api/v1/evaluation/eval-sets:with-analysis-count"))
    assert data["total"] == 1
    assert data["items"][0]["analysis_count"] == 0


# ── 10. Chat 会话（重写 CRUD）──────────────────────────────────────────────────

def test_list_chat_sessions_task_not_found(client, seed):
    _err(client.get("/api/v1/evaluation/tasks/999/chat-sessions"), 404)


def test_list_chat_sessions_empty(client, seed):
    data = _data(client.get("/api/v1/evaluation/tasks/1/chat-sessions"))
    assert data["total"] == 0


def test_save_chat_session_create(app, client, seed):
    resp = client.post("/api/v1/evaluation/tasks/1/chat-sessions",
                       json={"name": "s1", "messages": [{"role": "user", "content": "hi"}]})
    data = _data(resp)
    assert resp.status_code == 201
    assert data["session_id"] >= 1


def test_save_chat_session_update(app, client, seed):
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        cur.execute("INSERT INTO report_chat_sessions (id, task_id, name, messages) VALUES (1, 1, 's', '[]')")
        db.commit()
    data = _data(client.post("/api/v1/evaluation/tasks/1/chat-sessions",
                            json={"session_id": 1, "name": "renamed", "messages": []}))
    assert data["session_id"] == 1


def test_save_chat_session_update_not_found(app, client, seed):
    _err(client.post("/api/v1/evaluation/tasks/1/chat-sessions",
                     json={"session_id": 999, "name": "x"}), 404)


def test_get_chat_session_success(app, client, seed):
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        cur.execute("INSERT INTO report_chat_sessions (id, task_id, name, messages) VALUES (1, 1, 's', '[{\"role\":\"user\",\"content\":\"hi\"}]')")
        db.commit()
    data = _data(client.get("/api/v1/evaluation/tasks/1/chat-sessions/1"))
    assert data["name"] == "s"
    assert data["messages"][0]["content"] == "hi"


def test_get_chat_session_not_found(client, seed):
    _err(client.get("/api/v1/evaluation/tasks/1/chat-sessions/999"), 404)


def test_delete_chat_session_success(app, client, seed):
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        cur.execute("INSERT INTO report_chat_sessions (id, task_id, name, messages) VALUES (1, 1, 's', '[]')")
        db.commit()
    assert client.delete("/api/v1/evaluation/tasks/1/chat-sessions/1").status_code == 204


def test_delete_chat_session_not_found(client, seed):
    _err(client.delete("/api/v1/evaluation/tasks/1/chat-sessions/999"), 404)
