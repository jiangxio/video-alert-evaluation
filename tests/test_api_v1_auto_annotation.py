"""端到端验证 /api/v1/auto-annotation 端点（任务 CRUD + 引擎状态 + 控制）。

测试分层（对齐 plan §测试策略）：
- 委托端点（start/stop/status/convert）：_stub_worker（autouse）把 _do_auto_annotation
  与 _batch_capture_gt_frames 替成 no-op，避开真 ffmpeg/模型 API/仓库写/Windows 文件锁，
  只测信封 + 状态码 + 错误码 + 委托真触发（task 落库、模块态更新）。worker 内部是旧
  生产代码职责，不重测。
- 原位重写端点（videos-without-events/tasks 列表/by-video/get-json/delete/clear）：
  seed 行直测；clear/delete 的 PROJECT_ROOT 用 _proj_root 重定向到 tmp（可造帧文件测删除）。

DB 为 conftest tmp 库（create_app→init_db 建全 schema 含 auto_annotation_tasks/
auto_annotation_frames/events/watermarked_videos）。conftest 已 patch
app.routes.auto_annotation.DATABASE_PATH→tmp（双绑定，后台线程直连此拷贝）+
behavior_analysis_service.DEFAULT_CONFIG_PATH→tmp（load/save_config 不碰仓库）。

盲区（已记明，不在本模块范围）：worker 端到端（抽帧/模型分析/生成 GT）未被覆盖；
auto-annotation 路由级低危/潜伏 bug（get_status 按 updated_at 取最近但任务 dict 无该字段、
frame_interval_sec 传字符串 cast 崩、stop_task 缺 `global _stop_requested` 致中断信号
传不到 worker）由 bug-audit 另行修。
"""
import json
from pathlib import Path

import pytest

from app.database import get_db
from app.routes import auto_annotation as _legacy


# ── 辅助 ────────────────────────────────────────────────────────────────────────

def _data(resp):
    assert 200 <= resp.status_code < 300, resp.status_code
    body = resp.get_json()
    assert body["code"] == 0, body
    return body["data"]


def _err(resp, status, code):
    assert resp.status_code == status, (resp.status_code, resp.get_json())
    body = resp.get_json()
    assert body["code"] == code, body
    return body


# ── fixtures ────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _stub_worker(monkeypatch):
    """autouse：把真 worker（_do_auto_annotation）+ convert 线程（_batch_capture_gt_frames）
    替成 no-op，避开真 ffmpeg/模型 API/仓库写/Windows tmp 库文件锁。autouse 防漏标导致
    start 成功用例起真 worker。monkeypatch 测试结束自动还原。"""
    monkeypatch.setattr(_legacy, "_do_auto_annotation", lambda *a, **k: None)
    monkeypatch.setattr(_legacy, "_batch_capture_gt_frames", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _reset_auto_anno_state():
    """autouse：每个用例后清模块级任务态，防跨用例串（stub 不开 sqlite 连接故无需等线程）。"""
    yield
    _legacy._auto_anno_tasks.clear()
    _legacy._task_queue.clear()
    _legacy._current_task_id = None
    _legacy._stop_requested = False


@pytest.fixture
def _proj_root(app, tmp_path, monkeypatch):
    """重定向 PROJECT_ROOT 到 tmp，clear/delete 可造帧文件测删除（不污染全局/仓库）。"""
    monkeypatch.setitem(app.config, "PROJECT_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture
def seed(app, tmp_path):
    """videos(id=1) + watermarked_videos(id=1, output_path 指向 tmp 真文件)。
    无 events → 该视频出现在 videos-without-events。"""
    wm_file = tmp_path / "wm1.mp4"
    wm_file.write_bytes(b"fake mp4 content")
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        cur.execute(
            "INSERT INTO videos (id, filename, original_path, video_id, duration) "
            "VALUES (1, 'v1.mp4', 'orig/v1.mp4', '046-001', 10.0)"
        )
        cur.execute(
            "INSERT INTO watermarked_videos (id, original_video_id, filename, output_path, duration) "
            "VALUES (1, 1, 'v1_wm.mp4', ?, 10.0)",
            (str(wm_file),),
        )
        db.commit()
    return {"video_db_id": 1, "wm_file": str(wm_file)}


def _insert_task(app, **fields):
    """直接插一条 auto_annotation_tasks 行（构造 done/带 result_json_path 等非默认状态）。
    动态拼列：只插 cols 里出现的列，其余用 DB 默认值。返回 id。"""
    cols = {
        "video_db_id": 1,
        "video_id": "046-001",
        "status": "done",
        "frame_interval_sec": 1,
        "merge_interval_sec": 5,
        "event_types": '["fight"]',
    }
    cols.update(fields)
    col_names = list(cols.keys())
    placeholders = ",".join("?" * len(col_names))
    col_list = ",".join(col_names)
    values = [cols[c] for c in col_names]
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        cur.execute(
            f"INSERT INTO auto_annotation_tasks ({col_list}) VALUES ({placeholders})",
            values,
        )
        db.commit()
        return cur.lastrowid


def _insert_frame(app, task_id, ts=0.0):
    """插一条 auto_annotation_frames 行。"""
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        cur.execute(
            "INSERT INTO auto_annotation_frames (task_id, timestamp_sec, frame_path, detected_event_types) "
            "VALUES (?, ?, ?, ?)",
            (task_id, ts, f"/tmp/frame_{ts}.jpg", "[]"),
        )
        db.commit()


def _seed_n_videos(app, tmp_path, n, start_id=1):
    """插 n 条 videos + watermarked_videos（无 events），用于分页测试。"""
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        for i in range(n):
            vid = start_id + i
            cur.execute(
                "INSERT INTO videos (id, filename, original_path, video_id, duration) "
                "VALUES (?, ?, ?, ?, 10.0)",
                (vid, f"v{vid}.mp4", f"orig/v{vid}.mp4", f"046-{vid:03d}"),
            )
            cur.execute(
                "INSERT INTO watermarked_videos (id, original_video_id, filename, output_path, duration) "
                "VALUES (?, ?, ?, ?, 10.0)",
                (vid, vid, f"v{vid}_wm.mp4", str(tmp_path / f"wm{vid}.mp4")),
            )
        db.commit()


def _seed_n_tasks(app, n):
    """插 n 条 auto_annotation_tasks（video_db_id=1），用于分页测试。"""
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        for i in range(n):
            cur.execute(
                "INSERT INTO auto_annotation_tasks (video_db_id, video_id, status, "
                "frame_interval_sec, merge_interval_sec, event_types) "
                "VALUES (1, '046-001', 'done', 1, 5, '[\"fight\"]')",
            )
        db.commit()


# ── 1. videos-without-events（原位重写·分页）──────────────────────────────────

def test_list_videos_without_events_empty(client):
    data = _data(client.get("/api/v1/auto-annotation/videos-without-events"))
    assert data["total"] == 0 and data["items"] == []


def test_list_videos_without_events_with_data(client, seed):
    data = _data(client.get("/api/v1/auto-annotation/videos-without-events"))
    assert data["total"] == 1
    item = data["items"][0]
    assert item["video_db_id"] == 1
    assert item["video_id"] == "046-001"
    assert item["id"] == 1  # wm_id
    assert item["duration"] == 10.0


def test_list_videos_without_events_excludes_with_events(app, client, seed):
    """有 events 的视频不出现（验证事件过滤 LEFT JOIN events + HAVING COUNT(e.id)=0）。"""
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        cur.execute(
            "INSERT INTO events (video_db_id, event_type, start_seconds, end_seconds, gt_frames_status) "
            "VALUES (1, 'fight', 0, 5, 'pending')"
        )
        db.commit()
    data = _data(client.get("/api/v1/auto-annotation/videos-without-events"))
    assert data["total"] == 0  # 该视频现在有事件，被过滤掉


def test_list_videos_without_events_pagination(app, client, tmp_path):
    _seed_n_videos(app, tmp_path, 25)
    data = _data(client.get("/api/v1/auto-annotation/videos-without-events?page_size=20"))
    assert data["total"] == 25
    assert len(data["items"]) == 20
    assert data["has_next"] is True
    data2 = _data(client.get("/api/v1/auto-annotation/videos-without-events?page=2&page_size=20"))
    assert len(data2["items"]) == 5
    assert data2["has_next"] is False


# ── 2. start（委托）──────────────────────────────────────────────────────────

def test_start_success_immediate(app, client, seed):
    """有效视频+水印，_current_task_id 为空→立即启动分支：200, queued:False。"""
    resp = client.post("/api/v1/auto-annotation/tasks", json={
        "video_db_id": 1, "event_types": ["fight"],
    })
    data = _data(resp)
    assert data["task_id"] >= 1
    assert data["queued"] is False
    # 委托真触发：task 行落 tmp 库
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT status FROM auto_annotation_tasks WHERE id = ?", (data["task_id"],))
        row = cur.fetchone()
        assert row is not None and row["status"] == "processing"
    # 模块态更新
    assert _legacy._current_task_id == data["task_id"]


def test_start_success_queued(app, client, seed):
    """预置 _current_task_id（模拟有运行任务）→ 排队分支：200, queued:True，不起线程。"""
    _legacy._current_task_id = 1
    _legacy._auto_anno_tasks[1] = {"task_id": 1, "video_id": "046-001"}
    resp = client.post("/api/v1/auto-annotation/tasks", json={
        "video_db_id": 1, "event_types": ["fight"],
    })
    data = _data(resp)
    assert data["queued"] is True
    assert data["task_id"] in _legacy._task_queue


def test_start_no_video(client, seed):
    _err(client.post("/api/v1/auto-annotation/tasks", json={"event_types": ["fight"]}), 400, 11000)


def test_start_bad_frame_interval(client, seed):
    _err(client.post("/api/v1/auto-annotation/tasks", json={
        "video_db_id": 1, "frame_interval_sec": 0, "event_types": ["fight"],
    }), 400, 11001)


def test_start_bad_merge_interval(client, seed):
    _err(client.post("/api/v1/auto-annotation/tasks", json={
        "video_db_id": 1, "merge_interval_sec": -1, "event_types": ["fight"],
    }), 400, 11002)


def test_start_no_event_types(client, seed):
    _err(client.post("/api/v1/auto-annotation/tasks", json={
        "video_db_id": 1, "event_types": [],
    }), 400, 11003)


def test_start_video_not_found(client, seed):
    _err(client.post("/api/v1/auto-annotation/tasks", json={
        "video_db_id": 999, "event_types": ["fight"],
    }), 404, 21021)


def test_start_no_watermark(app, client, tmp_path):
    """仅有视频无水印→21022(404，旧 400→404 对齐 videos 20121)。"""
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        cur.execute(
            "INSERT INTO videos (id, filename, original_path, video_id, duration) "
            "VALUES (2, 'v2.mp4', 'orig/v2.mp4', '046-002', 10.0)"
        )
        db.commit()
    _err(client.post("/api/v1/auto-annotation/tasks", json={
        "video_db_id": 2, "event_types": ["fight"],
    }), 404, 21022)


# ── 3. stop（委托）────────────────────────────────────────────────────────────

def test_stop_success(app, client):
    """预置 _current_task_id→stop 返回 200 + task_id。
    注：旧 stop_task 缺 `global _stop_requested`，`_stop_requested=True` 是局部赋值、
    模块全局不翻转（潜伏 bug，stop 信号实际传不到 worker），故只断响应契约，不断内部态。"""
    _legacy._current_task_id = 7
    _legacy._auto_anno_tasks[7] = {"task_id": 7}
    data = _data(client.post("/api/v1/auto-annotation/tasks:stop"))
    assert data["task_id"] == 7


def test_stop_no_running(client):
    """无运行任务→409(31040，旧 400→409)。"""
    _err(client.post("/api/v1/auto-annotation/tasks:stop"), 409, 31040)


# ── 4. status（委托）─────────────────────────────────────────────────────────

def test_status_idle(client):
    """空态：has_running_task:False。"""
    data = _data(client.get("/api/v1/auto-annotation/status"))
    assert data["has_running_task"] is False
    assert data["current_task"] is None
    assert data["queue_count"] == 0


def test_status_with_current(app, client):
    """预置 _current_task_id + queue→has_running_task:True。"""
    _legacy._current_task_id = 5
    _legacy._auto_anno_tasks[5] = {"task_id": 5, "video_id": "046-001",
                                   "status": "processing", "phase": "analyzing",
                                   "phase_progress": 40, "analyzed_frames": 3,
                                   "total_frames": 10, "video_db_id": 1}
    _legacy._task_queue.extend([6, 7])
    _legacy._auto_anno_tasks[6] = {"video_id": "046-002"}
    _legacy._auto_anno_tasks[7] = {"video_id": "046-003"}
    data = _data(client.get("/api/v1/auto-annotation/status"))
    assert data["has_running_task"] is True
    assert data["current_task"]["task_id"] == 5
    assert data["queue_count"] == 2


# ── 5. tasks 列表（原位重写·分页）────────────────────────────────────────────

def test_list_tasks_empty(client):
    data = _data(client.get("/api/v1/auto-annotation/tasks"))
    assert data["total"] == 0 and data["items"] == []


def test_list_tasks_with_data(app, client, seed):
    tid = _insert_task(app, status="processing")
    data = _data(client.get("/api/v1/auto-annotation/tasks"))
    assert data["total"] == 1
    assert data["items"][0]["id"] == tid
    assert data["items"][0]["video_filename"] == "v1.mp4"


def test_list_tasks_pagination(app, client, seed):
    _seed_n_tasks(app, 25)
    data = _data(client.get("/api/v1/auto-annotation/tasks?page_size=20"))
    assert data["total"] == 25
    assert len(data["items"]) == 20
    assert data["has_next"] is True


# ── 6. by-video（原位重写·分页）──────────────────────────────────────────────

def test_list_tasks_by_video_empty(client, seed):
    data = _data(client.get("/api/v1/auto-annotation/videos/1/tasks"))
    assert data["total"] == 0 and data["items"] == []


def test_list_tasks_by_video_done(app, client, seed):
    """done + result_json_path 才返回。"""
    _insert_task(app, status="done", result_json_path="/tmp/gt.json")
    data = _data(client.get("/api/v1/auto-annotation/videos/1/tasks"))
    assert data["total"] == 1


def test_list_tasks_by_video_excludes_non_done(app, client, seed):
    """processing 或无 result_json_path 的不返回。"""
    _insert_task(app, status="processing", result_json_path="/tmp/gt.json")
    _insert_task(app, status="done", result_json_path=None)
    data = _data(client.get("/api/v1/auto-annotation/videos/1/tasks"))
    assert data["total"] == 0


# ── 7. get-json（原位重写）────────────────────────────────────────────────────

def test_get_json_task_not_found(client):
    _err(client.get("/api/v1/auto-annotation/tasks/999/json"), 404, 21020)


def test_get_json_file_missing(app, client, seed):
    """task 存在但 result_json_path 指向不存在文件→21023。"""
    tid = _insert_task(app, status="done", result_json_path="/tmp/nonexistent.json")
    _err(client.get(f"/api/v1/auto-annotation/tasks/{tid}/json"), 404, 21023)


def test_get_json_success(app, client, seed, tmp_path):
    gt = tmp_path / "gt.json"
    gt.write_text(json.dumps({"file": "v1.mp4", "id": "046-001",
                              "events": [{"type": "fight", "start": 0, "end": 5}]}),
                  encoding="utf-8")
    tid = _insert_task(app, status="done", result_json_path=str(gt))
    data = _data(client.get(f"/api/v1/auto-annotation/tasks/{tid}/json"))
    assert data["id"] == "046-001"
    assert data["events"][0]["type"] == "fight"


# ── 8. delete（原位重写）──────────────────────────────────────────────────────

def test_delete_not_found(client):
    _err(client.delete("/api/v1/auto-annotation/tasks/999"), 404, 21020)


def test_delete_success(app, client, seed, _proj_root):
    tid = _insert_task(app, status="done")
    _insert_frame(app, tid)
    resp = client.delete(f"/api/v1/auto-annotation/tasks/{tid}")
    assert resp.status_code == 204
    # 行已删
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT id FROM auto_annotation_tasks WHERE id = ?", (tid,))
        assert cur.fetchone() is None
        cur.execute("SELECT id FROM auto_annotation_frames WHERE task_id = ?", (tid,))
        assert cur.fetchone() is None


def test_delete_clears_frame_files(app, client, seed, _proj_root):
    """帧文件目录被删。"""
    tid = _insert_task(app, status="done")
    frames_dir = _proj_root / "auto_annotation_frames" / str(tid)
    frames_dir.mkdir(parents=True)
    (frames_dir / "frame_000000.jpg").write_bytes(b"x")
    client.delete(f"/api/v1/auto-annotation/tasks/{tid}")
    assert not frames_dir.exists()


# ── 9. clear（原位重写）────────────────────────────────────────────────────────

def test_clear_success(app, client, seed, _proj_root):
    tid = _insert_task(app, status="processing")
    _insert_frame(app, tid)
    _insert_frame(app, tid, ts=1.0)
    data = _data(client.post(f"/api/v1/auto-annotation/tasks/{tid}:clear"))
    assert data["task_id"] == tid
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT COUNT(*) FROM auto_annotation_frames WHERE task_id = ?", (tid,))
        assert cur.fetchone()[0] == 0


def test_clear_no_dir_noop(app, client, seed, _proj_root):
    """帧目录不存在也 200（幂等，对齐旧 clear-intermediate 不校验任务存在性）。"""
    tid = _insert_task(app, status="done")
    resp = client.post(f"/api/v1/auto-annotation/tasks/{tid}:clear")
    assert resp.status_code == 200


def test_clear_idempotent(app, client, seed, _proj_root):
    tid = _insert_task(app, status="done")
    _insert_frame(app, tid)
    r1 = client.post(f"/api/v1/auto-annotation/tasks/{tid}:clear")
    r2 = client.post(f"/api/v1/auto-annotation/tasks/{tid}:clear")
    assert r1.status_code == 200 and r2.status_code == 200


# ── 10. convert-to-events（委托）──────────────────────────────────────────────

def test_convert_not_found(client):
    _err(client.post("/api/v1/auto-annotation/tasks/999:convert-to-events"), 404, 21020)


def test_convert_not_done(app, client, seed):
    """status≠done→409(31041，旧 400→409)。"""
    tid = _insert_task(app, status="processing", result_json_path="/tmp/gt.json")
    _err(client.post(f"/api/v1/auto-annotation/tasks/{tid}:convert-to-events"), 409, 31041)


def test_convert_json_missing(app, client, seed):
    """done 但结果 JSON 文件不存在→404(21023，旧 400→404)。"""
    tid = _insert_task(app, status="done", result_json_path="/tmp/nonexistent.json")
    _err(client.post(f"/api/v1/auto-annotation/tasks/{tid}:convert-to-events"), 404, 21023)


def test_convert_success(app, client, seed, tmp_path):
    """done + 有效 GT JSON→200，events 入库（_batch_capture_gt_frames 被 stub 不抓帧）。"""
    gt = tmp_path / "gt.json"
    gt.write_text(json.dumps({"file": "v1.mp4", "id": "046-001",
                              "events": [{"type": "fight", "start": 0, "end": 5}]}),
                  encoding="utf-8")
    tid = _insert_task(app, status="done", result_json_path=str(gt))
    data = _data(client.post(f"/api/v1/auto-annotation/tasks/{tid}:convert-to-events"))
    assert data["event_count"] == 1
    # events 入库
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT COUNT(*) FROM events WHERE video_db_id = 1")
        assert cur.fetchone()[0] == 1


# ── 阶段2：置信度解析（analyze_frame / _parse_label_confidence）──────────────────

def _make_fake_client(content: str):
    """构造 OpenAI 兼容假 client：chat.completions.create 返回固定 content。"""
    class _M:
        pass
    msg = _M()
    msg.content = content
    choice = type("C", (), {"message": msg})()
    completion = type("Comp", (), {"choices": [choice]})()

    class _Completions:
        @staticmethod
        def create(**kw):
            return completion
    chat = type("Chat", (), {"completions": _Completions()})()
    return type("Client", (), {"chat": chat})()


def test_parse_label_confidence_json():
    """模型返合法 JSON → [{label,confidence}]。"""
    from app.services import behavior_analysis_service as bas
    items = bas._parse_label_confidence(
        '{"labels":[{"label":"fight","confidence":0.82},{"label":"rat","confidence":0.3}]}',
        {"fight", "rat", "normal"})
    assert items == [{"label": "fight", "confidence": 0.82},
                     {"label": "rat", "confidence": 0.3}]


def test_parse_label_confidence_markdown_fenced():
    """模型把 JSON 包在 ```json ... ``` 里 → 截取后解析。"""
    from app.services import behavior_analysis_service as bas
    items = bas._parse_label_confidence(
        '```json\n{"labels":[{"label":"fight","confidence":0.7}]}\n```',
        {"fight", "normal"})
    assert items == [{"label": "fight", "confidence": 0.7}]


def test_parse_label_confidence_fallback():
    """非 JSON → 逗号分隔标签，confidence=1.0（向后兼容旧调用方）。"""
    from app.services import behavior_analysis_service as bas
    items = bas._parse_label_confidence("fight, rat", {"fight", "rat", "normal"})
    assert items == [{"label": "fight", "confidence": 1.0},
                     {"label": "rat", "confidence": 1.0}]


def test_parse_label_confidence_clamps_confidence():
    """confidence 越界夹到 [0,1]。"""
    from app.services import behavior_analysis_service as bas
    items = bas._parse_label_confidence(
        '{"labels":[{"label":"fight","confidence":1.5}]}', {"fight"})
    assert items == [{"label": "fight", "confidence": 1.0}]


def test_analyze_frame_json(tmp_path, monkeypatch):
    """analyze_frame 解析模型 JSON 返 [{label,confidence}]，normal 被去掉。"""
    from app.services import behavior_analysis_service as bas
    monkeypatch.setattr(bas, "build_prompt", lambda types, event_descriptions=None: "")
    img = tmp_path / "f.jpg"
    img.write_bytes(b"fake image bytes")
    client = _make_fake_client(
        '{"labels":[{"label":"fight","confidence":0.9},{"label":"normal","confidence":0.1}]}')
    result = bas.analyze_frame(client, "m", str(img), ["fight", "rat"])
    assert result == [{"label": "fight", "confidence": 0.9}]


def test_analyze_frame_fallback(tmp_path, monkeypatch):
    """模型返非 JSON → 回退逗号分隔，confidence=1.0。"""
    from app.services import behavior_analysis_service as bas
    monkeypatch.setattr(bas, "build_prompt", lambda types, event_descriptions=None: "")
    img = tmp_path / "f.jpg"
    img.write_bytes(b"x")
    client = _make_fake_client("fight, rat")
    result = bas.analyze_frame(client, "m", str(img), ["fight", "rat"])
    assert result == [{"label": "fight", "confidence": 1.0},
                      {"label": "rat", "confidence": 1.0}]


def test_analyze_frame_empty_returns_normal(tmp_path, monkeypatch):
    """模型返空 → 保底 [{normal,1.0}]。"""
    from app.services import behavior_analysis_service as bas
    monkeypatch.setattr(bas, "build_prompt", lambda types, event_descriptions=None: "")
    img = tmp_path / "f.jpg"
    img.write_bytes(b"x")
    client = _make_fake_client("")
    result = bas.analyze_frame(client, "m", str(img), ["fight"])
    assert result == [{"label": "normal", "confidence": 1.0}]


def test_build_prompt_includes_chinese_name(app):
    """build_prompt 每个类型同时给 key + 中文名，避免模型按 key 字面误解
    （如生产里 key='fight' 实际是「人员聚集」，裸给 'fight' 模型会找「打架」）。"""
    from app.services.behavior_analysis_service import build_prompt
    from app.event_types import get_type_names
    with app.app_context():
        prompt = build_prompt(["fight", "personAction"])
        names = get_type_names()
        # key（模型要返的 label）+ 中文名 都应在 prompt 里
        assert "fight" in prompt and "personAction" in prompt
        assert names["fight"] in prompt
        assert names["personAction"] in prompt


def test_build_prompt_event_descriptions_override(app):
    """用户动态注入的 event_descriptions 优先于 DB 描述；prompt 始终通用（按传入类型动态列）。"""
    from app.services.behavior_analysis_service import build_prompt
    with app.app_context():
        p = build_prompt(["personAction", "fight"], event_descriptions={
            "personAction": "自定义人员动作描述XYZ",
            "fight": "自定义聚集描述ABC",
        })
        assert "自定义人员动作描述XYZ" in p
        assert "自定义聚集描述ABC" in p
        # 不注入 → 用 DB 描述（不硬编码）
        p2 = build_prompt(["personAction"])
        assert "自定义" not in p2


def test_build_prompt_no_injection_uses_db_or_name(app):
    """不传 event_descriptions → 用 DB description；DB 也空 → 用中文名（通用，非硬编码）。"""
    from app.services.behavior_analysis_service import build_prompt
    from app.event_types import get_type_names, get_type_descriptions
    with app.app_context():
        names = get_type_names()
        descs = get_type_descriptions()
        p = build_prompt(["fight", "personAction"])  # fight 的 DB desc 空、personAction 有
        # fight：DB desc 空取中文名「人员聚集」；personAction：用 DB desc
        assert names["fight"] in p
        assert (descs.get("personAction") or "") in p
        # 没有任何「自定义」字样（未注入）
        assert "自定义" not in p


def test_build_prompt_partial_injection(app):
    """只给部分类型注入描述：被注入的用注入值，其余用 DB/中文名（各自独立）。"""
    from app.services.behavior_analysis_service import build_prompt
    with app.app_context():
        p = build_prompt(["fight", "personAction"],
                         event_descriptions={"fight": "只给fight注入XYZ"})
        assert "只给fight注入XYZ" in p              # fight 用注入
        # personAction 没被注入 → 用 DB 描述（不含 XYZ）
        person_line = [ln for ln in p.splitlines() if "personAction" in ln][0]
        assert "XYZ" not in person_line


def test_analyze_frame_passes_event_descriptions(tmp_path, monkeypatch):
    """analyze_frame 把 event_descriptions 透传给 build_prompt。"""
    from app.services import behavior_analysis_service as bas
    captured = {}
    def _capture(types, event_descriptions=None):
        captured["types"] = types
        captured["desc"] = event_descriptions
        return ""
    monkeypatch.setattr(bas, "build_prompt", _capture)
    img = tmp_path / "f.jpg"
    img.write_bytes(b"x")
    client = _make_fake_client('{"labels":[{"label":"normal","confidence":1.0}]}')
    bas.analyze_frame(client, "m", str(img), ["fight"], event_descriptions={"fight": "注入DESC"})
    assert captured["types"] == ["fight"]
    assert captured["desc"] == {"fight": "注入DESC"}


def test_start_auto_annotation_stores_descriptions(app, client, seed):
    """POST /auto-annotation/tasks 带 event_descriptions → 落库到 task 行（worker 已 stub）。"""
    data = _data(client.post("/api/v1/auto-annotation/tasks", json={
        "video_db_id": 1, "event_types": ["fight"],
        "event_descriptions": {"fight": "多人聚集停留"},
    }))
    tid = data["task_id"]
    with app.app_context():
        row = get_db().execute(
            "SELECT event_descriptions FROM auto_annotation_tasks WHERE id = ?", (tid,)
        ).fetchone()
        assert json.loads(row["event_descriptions"]) == {"fight": "多人聚集停留"}


# ── 阶段2：_merge_frame_results 带置信度 + 复核状态判定 ─────────────────────────

def test_merge_frame_results_with_confidence():
    """合并事件 confidence = 成员帧最高置信；间隔外断开新事件。"""
    frames = [
        {"timestamp_sec": 0, "detected_event_types": [{"type": "fight", "confidence": 0.9}]},
        {"timestamp_sec": 1, "detected_event_types": [{"type": "fight", "confidence": 0.7}]},
        {"timestamp_sec": 10, "detected_event_types": [{"type": "fight", "confidence": 0.5}]},
    ]
    events = _legacy._merge_frame_results(frames, 5, ["fight"])
    assert len(events) == 2
    assert events[0] == {"type": "fight", "start": 0, "end": 1, "confidence": 0.9}
    assert events[1] == {"type": "fight", "start": 10, "end": 10, "confidence": 0.5}


def test_merge_frame_results_legacy_string_labels():
    """旧格式字符串标签 → confidence=1.0（向后兼容）。"""
    frames = [
        {"timestamp_sec": 0, "detected_event_types": ["fight"]},
        {"timestamp_sec": 1, "detected_event_types": ["fight"]},
    ]
    events = _legacy._merge_frame_results(frames, 5, ["fight"])
    assert events == [{"type": "fight", "start": 0, "end": 1, "confidence": 1.0}]


def test_event_review_status_threshold():
    """≥阈值 auto_approved，<阈值 pending，边界(==)归 approved，None 按 1.0。"""
    assert _legacy._event_review_status(0.9, 0.6) == "auto_approved"
    assert _legacy._event_review_status(0.5, 0.6) == "pending"
    assert _legacy._event_review_status(0.6, 0.6) == "auto_approved"
    assert _legacy._event_review_status(None, 0.6) == "auto_approved"
    assert _legacy._event_review_status(0.0, 0.6) == "pending"  # 0.0 是合法低置信，非 None


def test_merge_frame_results_zero_confidence_not_bumped():
    """回归：confidence=0.0 不应被 `or 1.0` 提升为 1.0（坑6 同款 bug）。事件 confidence=0.0。"""
    frames = [
        {"timestamp_sec": 2, "detected_event_types": [{"type": "fight", "confidence": 0.0}]},
    ]
    events = _legacy._merge_frame_results(frames, 5, ["fight"])
    assert events == [{"type": "fight", "start": 2, "end": 2, "confidence": 0.0}]
    assert _legacy._event_review_status(events[0]["confidence"], 0.6) == "pending"


# ── 阶段2：复核端点（pending-events / review / batch-approve）──────────────────

def _insert_anno_event(app, task_id, **fields):
    """直接插一条 auto_annotation_events 行（默认 pending 低置信）。返回 id。"""
    cols = {"task_id": task_id, "video_db_id": 1, "event_type": "fight",
            "start_sec": 0.0, "end_sec": 5.0, "confidence": 0.3,
            "review_status": "pending"}
    cols.update(fields)
    col_names = list(cols.keys())
    placeholders = ",".join("?" * len(col_names))
    col_list = ",".join(col_names)
    values = [cols[c] for c in col_names]
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        cur.execute(
            f"INSERT INTO auto_annotation_events ({col_list}) VALUES ({placeholders})",
            values,
        )
        db.commit()
        return cur.lastrowid


def _events_count(app, video_db_id=1):
    with app.app_context():
        return get_db().execute(
            "SELECT COUNT(*) FROM events WHERE video_db_id = ?", (video_db_id,)
        ).fetchone()[0]


def test_list_pending_events(app, client, seed):
    tid = _insert_task(app, status="done")
    _insert_anno_event(app, tid, start_sec=0)
    _insert_anno_event(app, tid, start_sec=10)
    _insert_anno_event(app, tid, review_status="auto_approved", start_sec=20)  # 排除
    data = _data(client.get(f"/api/v1/auto-annotation/tasks/{tid}/pending-events"))
    assert data["total"] == 2
    assert all(e["review_status"] == "pending" for e in data["items"])


def test_list_pending_events_task_not_found(client, seed):
    _err(client.get("/api/v1/auto-annotation/tasks/999/pending-events"), 404, 21020)


def test_review_approve(app, client, seed):
    """approve pending → 写 DB events + 状态 approved（_batch_capture_gt_frames stub 不抓帧）。"""
    tid = _insert_task(app, status="done")
    eid = _insert_anno_event(app, tid, review_status="pending", confidence=0.3)
    data = _data(client.post(f"/api/v1/auto-annotation/events/{eid}:review",
                             json={"action": "approve"}))
    assert data["review_status"] == "approved"
    assert data["db_event_id"] >= 1
    assert _events_count(app) == 1
    with app.app_context():
        row = get_db().execute(
            "SELECT review_status FROM auto_annotation_events WHERE id=?", (eid,)
        ).fetchone()
        assert row["review_status"] == "approved"


def test_review_approve_with_edit(app, client, seed):
    """approve 可带 type/start/end 编辑后入库。"""
    tid = _insert_task(app, status="done")
    eid = _insert_anno_event(app, tid, event_type="fight", start_sec=0, end_sec=5)
    data = _data(client.post(f"/api/v1/auto-annotation/events/{eid}:review",
                             json={"action": "approve", "type": "rat", "start": 1, "end": 6}))
    assert data["review_status"] == "approved"
    with app.app_context():
        ev = get_db().execute(
            "SELECT event_type,start_sec,end_sec FROM auto_annotation_events WHERE id=?", (eid,)
        ).fetchone()
        assert ev["event_type"] == "rat" and ev["start_sec"] == 1 and ev["end_sec"] == 6
        # DB events 用编辑后的字段
        row = get_db().execute(
            "SELECT event_type FROM events WHERE video_db_id=1"
        ).fetchone()
        assert row["event_type"] == "rat"


def test_review_reject(app, client, seed):
    """reject → 标 rejected，不写 DB events。"""
    tid = _insert_task(app, status="done")
    eid = _insert_anno_event(app, tid, review_status="pending")
    data = _data(client.post(f"/api/v1/auto-annotation/events/{eid}:review",
                             json={"action": "reject"}))
    assert data["review_status"] == "rejected"
    assert _events_count(app) == 0
    with app.app_context():
        row = get_db().execute(
            "SELECT review_status FROM auto_annotation_events WHERE id=?", (eid,)
        ).fetchone()
        assert row["review_status"] == "rejected"


def test_review_edit_keeps_pending(app, client, seed):
    """edit → 改字段，状态仍 pending，不写 DB events。"""
    tid = _insert_task(app, status="done")
    eid = _insert_anno_event(app, tid, event_type="fight", start_sec=0, end_sec=5)
    data = _data(client.post(f"/api/v1/auto-annotation/events/{eid}:review",
                             json={"action": "edit", "type": "rat", "start": 1, "end": 6}))
    assert data["review_status"] == "pending"
    assert _events_count(app) == 0
    with app.app_context():
        ev = get_db().execute(
            "SELECT event_type,start_sec,end_sec,review_status FROM auto_annotation_events WHERE id=?",
            (eid,)
        ).fetchone()
        assert ev["event_type"] == "rat" and ev["review_status"] == "pending"


def test_review_not_found(client, seed):
    _err(client.post("/api/v1/auto-annotation/events/999:review",
                     json={"action": "approve"}), 404, 21024)


def test_review_non_pending(app, client, seed):
    """已 approved 的事件不可再复核→409(31042)。"""
    tid = _insert_task(app, status="done")
    eid = _insert_anno_event(app, tid, review_status="approved")
    _err(client.post(f"/api/v1/auto-annotation/events/{eid}:review",
                     json={"action": "approve"}), 409, 31042)


def test_review_bad_action(app, client, seed):
    tid = _insert_task(app, status="done")
    eid = _insert_anno_event(app, tid, review_status="pending")
    _err(client.post(f"/api/v1/auto-annotation/events/{eid}:review",
                     json={"action": "bogus"}), 400, 11004)


def test_batch_approve_all(app, client, seed):
    """不传 event_ids → 通过该任务全部 pending（auto_approved 不含）。"""
    tid = _insert_task(app, status="done")
    _insert_anno_event(app, tid, review_status="pending")
    _insert_anno_event(app, tid, review_status="pending")
    _insert_anno_event(app, tid, review_status="auto_approved")  # 排除
    data = _data(client.post(f"/api/v1/auto-annotation/tasks/{tid}:batch-approve", json={}))
    assert data["approved_count"] == 2
    assert _events_count(app) == 2


def test_batch_approve_subset(app, client, seed):
    """传 event_ids → 仅通过指定子集。"""
    tid = _insert_task(app, status="done")
    e1 = _insert_anno_event(app, tid, review_status="pending")
    e2 = _insert_anno_event(app, tid, review_status="pending")
    data = _data(client.post(f"/api/v1/auto-annotation/tasks/{tid}:batch-approve",
                             json={"event_ids": [e1]}))
    assert data["approved_count"] == 1
    assert _events_count(app) == 1


def test_batch_approve_task_not_found(client, seed):
    _err(client.post("/api/v1/auto-annotation/tasks/999:batch-approve", json={}), 404, 21020)


# ── 11. 质量评估（阶段3·只读）────────────────────────────────────────────────

def _insert_frame_conf(app, task_id, ts, confidence):
    """插一条带 confidence 的 auto_annotation_frames 行。"""
    with app.app_context():
        db = get_db()
        db.execute(
            "INSERT INTO auto_annotation_frames (task_id, timestamp_sec, frame_path, "
            "detected_event_types, confidence, review_status) VALUES (?, ?, ?, '[]', ?, 'auto')",
            (task_id, ts, f"/tmp/f{ts}.jpg", confidence),
        )
        db.commit()


def _insert_anno_event_status(app, task_id, event_type, review_status, confidence=0.3):
    with app.app_context():
        db = get_db()
        db.execute(
            "INSERT INTO auto_annotation_events (task_id, video_db_id, event_type, start_sec, "
            "end_sec, confidence, review_status) VALUES (?, 1, ?, 0, 5, ?, ?)",
            (task_id, event_type, confidence, review_status),
        )
        db.commit()


def test_get_quality(app, client, seed):
    """置信度分布/覆盖率/复核拒绝率，无下游评测→null。"""
    tid = _insert_task(app, status="done")
    _insert_frame_conf(app, tid, 0, 0.9)
    _insert_frame_conf(app, tid, 1, 0.4)
    _insert_frame_conf(app, tid, 2, 0.0)  # normal 无检测
    _insert_anno_event_status(app, tid, "fight", "auto_approved", 0.9)
    _insert_anno_event_status(app, tid, "rat", "pending", 0.4)
    _insert_anno_event_status(app, tid, "misc", "rejected", 0.2)

    data = _data(client.get(f"/api/v1/auto-annotation/tasks/{tid}/quality"))
    assert data["task_id"] == tid
    assert data["confidence"]["count"] == 3
    assert data["confidence"]["mean"] == pytest.approx((0.9 + 0.4 + 0.0) / 3, abs=1e-3)
    assert data["confidence"]["max"] == 0.9 and data["confidence"]["min"] == 0.0
    assert data["confidence"]["bins"]["0.8-1.0"] == 1
    assert data["confidence"]["bins"]["0-0.2"] == 1  # 0.0 落 0-0.2
    assert data["coverage_rate"] == pytest.approx(2 / 3, abs=1e-3)  # 2 帧有检测
    assert data["review"]["approved"] == 1
    assert data["review"]["pending"] == 1
    assert data["review"]["rejected"] == 1
    assert data["review"]["rejection_rate"] == pytest.approx(1 / 2, abs=1e-3)  # 1/(1+1)
    assert data["downstream_eval"] is None  # 无 eval_task


def test_get_quality_task_not_found(client, seed):
    _err(client.get("/api/v1/auto-annotation/tasks/999/quality"), 404, 21020)


def test_get_quality_empty_task(app, client, seed):
    """无帧无事件→全 0，不崩。"""
    tid = _insert_task(app, status="done")
    data = _data(client.get(f"/api/v1/auto-annotation/tasks/{tid}/quality"))
    assert data["confidence"]["count"] == 0
    assert data["coverage_rate"] == 0
    assert data["review"]["rejection_rate"] == 0
    assert data["downstream_eval"] is None


def test_get_quality_downstream(app, client, seed):
    """本任务视频被已 final 评测任务引用→downstream_eval 带存储指标（只读）。"""
    tid = _insert_task(app, status="done", video_db_id=1)
    _insert_frame_conf(app, tid, 0, 0.9)
    with app.app_context():
        db = get_db()
        db.execute("INSERT INTO eval_video_sets (id, name, video_ids) VALUES (1, 'vs1', '[1]')")
        db.execute(
            "INSERT INTO eval_tasks (id, name, eval_set_id, status, finalized, "
            "accuracy, recall, avg_fp_per_hour) VALUES (5, 'et5', 1, 'done', 1, 0.8, 0.9, 1.5)"
        )
        db.commit()
    data = _data(client.get(f"/api/v1/auto-annotation/tasks/{tid}/quality"))
    assert data["downstream_eval"]["eval_task_id"] == 5
    assert data["downstream_eval"]["accuracy"] == 0.8
    assert data["downstream_eval"]["avg_fp_per_hour"] == 1.5


# ── 12. GT 版本管理（阶段3：版本端点 + get_task_json ?version=）─────────────

def _versions_dir(app):
    return Path(app.config["GROUND_TRUTH_DIR"]).parent / "ground_truth_versions"


def _make_version(app, vid, events, task_id=None):
    from app.routes.videos import _snapshot_gt_version
    return _snapshot_gt_version(vid, {"id": vid, "events": events}, _versions_dir(app), task_id=task_id)


def test_gt_versions_lifecycle(app, client, seed):
    """两版本→list 倒序→get v1 内容→restore v1→当前 GT 变 v1 内容 + 新版本 v3。"""
    vid = "046-001"
    v1 = _make_version(app, vid, [{"type": "fight", "start": 0, "end": 5}])
    v2 = _make_version(app, vid, [{"type": "fight", "start": 0, "end": 5},
                                   {"type": "rat", "start": 10, "end": 15}])
    assert v1 == 1 and v2 == 2

    data = _data(client.get(f"/api/v1/auto-annotation/videos/{vid}/gt-versions"))
    assert data["total"] == 2
    assert data["items"][0]["version_no"] == 2  # 倒序

    v1_id = [it for it in data["items"] if it["version_no"] == 1][0]["id"]
    g = _data(client.get(f"/api/v1/auto-annotation/gt-versions/{v1_id}"))
    assert g["version"]["version_no"] == 1
    assert g["content"]["events"] == [{"type": "fight", "start": 0, "end": 5}]  # v1 只 fight

    r = _data(client.post(f"/api/v1/auto-annotation/gt-versions/{v1_id}:restore"))
    assert r["restored_from_version"] == 1
    assert r["new_version_no"] == 3
    cur_gt = Path(app.config["GROUND_TRUTH_DIR"]) / f"{vid}.json"
    assert json.loads(cur_gt.read_text(encoding="utf-8"))["events"] == \
        [{"type": "fight", "start": 0, "end": 5}]


def test_list_gt_versions_empty(app, client, seed):
    data = _data(client.get("/api/v1/auto-annotation/videos/no-such/gt-versions"))
    assert data["total"] == 0 and data["items"] == []


def test_get_gt_version_not_found(client, seed):
    _err(client.get("/api/v1/auto-annotation/gt-versions/999"), 404, 21024)


def test_get_gt_version_snapshot_missing(app, client, seed):
    """版本记录在但快照文件丢失→21023。"""
    vid = "046-001"
    with app.app_context():
        db = get_db()
        db.execute(
            "INSERT INTO gt_versions (video_id, version_no, path) VALUES (?, 1, ?)",
            (vid, "/nonexistent/v1.json"),
        )
        db.commit()
        vid_row = db.execute(
            "SELECT id FROM gt_versions WHERE video_id = ?", (vid,)
        ).fetchone()
    _err(client.get(f"/api/v1/auto-annotation/gt-versions/{vid_row['id']}"), 404, 21023)


def test_get_task_json_with_version(app, client, seed):
    """?version=1 取历史版本快照（而非当前 result_json_path）。"""
    tid = _insert_task(app, status="done", video_id="046-001")
    _make_version(app, "046-001", [{"type": "fight", "start": 0, "end": 5}])
    data = _data(client.get(f"/api/v1/auto-annotation/tasks/{tid}/json?version=1"))
    assert data["events"] == [{"type": "fight", "start": 0, "end": 5}]


def test_get_task_json_version_not_found(app, client, seed):
    tid = _insert_task(app, status="done", video_id="046-001")
    _err(client.get(f"/api/v1/auto-annotation/tasks/{tid}/json?version=999"), 404, 21024)


def test_get_annotation_result_tool(app, tmp_path):
    """助手 get_annotation_result 工具：读 GT JSON 并给事件补中文名（不再返空）。"""
    from app.services.assistant_tools import get_annotation_result
    gt = tmp_path / "gt.json"
    gt.write_text(json.dumps({"file": "v.mp4", "id": "046-001",
                              "events": [{"type": "fight", "start": 5, "end": 5}]}),
                  encoding="utf-8")
    tid = _insert_task(app, status="done", video_id="046-001", result_json_path=str(gt))
    with app.app_context():
        r = get_annotation_result(task_id=tid)
        assert r["status"] == "done"
        assert r["events"][0]["type"] == "fight"
        assert r["events"][0]["name"]  # 补了中文名
        # 按 video_id 查最新 done
        r2 = get_annotation_result(video_id="046-001")
        assert r2["task_id"] == tid
        assert r2["events"][0]["name"]
    # 不存在的任务
    with app.app_context():
        assert "error" in get_annotation_result(task_id=999)
