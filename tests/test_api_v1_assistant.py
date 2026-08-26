"""端到端验证 /api/v1/assistant 端点（settings/tasks 重写 + chat/confirm/cancel/clear/history 委托）。

测试分层（对齐 plan §测试策略）：
- 委托端点（chat/confirm/cancel/clear/history）：mock 旧视图导入的 service 名
  （app.routes.assistant.chat/is_configured/confirm/cancel），避开真 LLM/worker；
  clear/history 用 session_transaction seed 会话，委托旧视图处理 session/过滤逻辑。
  核心约束：chat/confirm/cancel 的 200+{type:'error'} 契约保 200 套 ok(body)，不映射 4xx。
- 原位重写端点（settings GET/POST、tasks 列表、tasks/<id>）：mock service 纯读函数
  （app.api.v1.assistant.get_settings_for_display）+ seed assistant_tasks 行直测。

DB 为 conftest tmp 库。assistant 无需 conftest 双绑定 patch（全走 get_db() 或函数级导入）。

盲区（bug-audit 另修）：worker（confirm 触发的 batch_ocr/add_watermark 等）未覆盖；
#20 LLM 无 timeout、#21 execute_update_alert_status 空操作返成功、低危 list_alerts rowcount -1。
"""
import json

import pytest

from app.database import get_db
from app.routes import assistant as _legacy


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

@pytest.fixture
def _settings_stub(monkeypatch):
    """mock v1 模块导入的 settings service 名，测 settings 重写信封（不依赖 settings service 内部）。"""
    monkeypatch.setattr("app.api.v1.assistant.get_settings_for_display",
                        lambda: {"openai_api_key": "sk-***", "configured": True})
    monkeypatch.setattr("app.api.v1.assistant.update_assistant_settings", lambda data: None)


def _insert_task(app, **fields):
    """插一条 assistant_tasks 行（task_type NOT NULL）。返回 id。"""
    cols = {"task_type": "batch_ocr", "status": "done", "params": '{"a":1}'}
    cols.update(fields)
    names = list(cols.keys())
    placeholders = ",".join("?" * len(names))
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        cur.execute(
            f"INSERT INTO assistant_tasks ({','.join(names)}) VALUES ({placeholders})",
            [cols[c] for c in names],
        )
        db.commit()
        return cur.lastrowid


# ── 1. settings（原位重写）────────────────────────────────────────────────────

def test_get_settings(client, _settings_stub):
    data = _data(client.get("/api/v1/assistant/settings"))
    assert data["settings"]["openai_api_key"] == "sk-***"


def test_update_settings(client, _settings_stub):
    data = _data(client.post("/api/v1/assistant/settings", json={"openai_api_key": "sk-new"}))
    assert data["settings"]["configured"] is True


# ── 2. tasks 列表（原位重写·真分页）────────────────────────────────────────────

def test_list_tasks_empty(client):
    data = _data(client.get("/api/v1/assistant/tasks"))
    assert data["total"] == 0 and data["items"] == []


def test_list_tasks_with_data(app, client):
    _insert_task(app, task_type="batch_ocr", status="running", params='{"x":9}')
    data = _data(client.get("/api/v1/assistant/tasks"))
    assert data["total"] == 1
    t = data["items"][0]
    assert t["task_type"] == "batch_ocr"
    assert t["params"] == {"x": 9}  # JSON 已解析


def test_list_tasks_pagination(app, client):
    for i in range(25):
        _insert_task(app, task_type=f"t{i}")
    data = _data(client.get("/api/v1/assistant/tasks?page_size=20"))
    assert data["total"] == 25
    assert len(data["items"]) == 20
    assert data["has_next"] is True


# ── 3. tasks/<id>（原位重写）──────────────────────────────────────────────────

def test_get_task_not_found(client):
    _err(client.get("/api/v1/assistant/tasks/999"), 404, 21200)


def test_get_task_success(app, client):
    tid = _insert_task(app, task_type="batch_ocr", status="done", result_summary="ok")
    data = _data(client.get(f"/api/v1/assistant/tasks/{tid}"))
    assert data["task"]["id"] == tid
    assert data["task"]["status"] == "done"
    assert "progress" in data


# ── 4. chat（委托）────────────────────────────────────────────────────────────

def test_chat_empty_message(client):
    _err(client.post("/api/v1/assistant/chat", json={"message": ""}), 400, 11200)


def test_chat_not_configured(client, monkeypatch):
    """is_configured()=False→200+type:error+NOT_CONFIGURED（保 200，前端契约）。"""
    monkeypatch.setattr("app.routes.assistant.is_configured", lambda: False)
    data = _data(client.post("/api/v1/assistant/chat", json={"message": "hi"}))
    assert data["type"] == "error"
    assert data["error_code"] == "NOT_CONFIGURED"


def test_chat_success(client, monkeypatch):
    """is_configured()=True + chat() 返 canned→200+type:response，套 ok 信封。"""
    monkeypatch.setattr("app.routes.assistant.is_configured", lambda: True)
    monkeypatch.setattr("app.routes.assistant.chat",
                        lambda msg, sid: {"type": "response", "message": {"role": "assistant", "content": "ok"}})
    data = _data(client.post("/api/v1/assistant/chat", json={"message": "hi"}))
    assert data["type"] == "response"
    assert data["message"]["content"] == "ok"


# ── 5. confirm（委托）──────────────────────────────────────────────────────────

def test_confirm_missing_id(client):
    _err(client.post("/api/v1/assistant/pending-confirmations:confirm", json={}), 400, 11201)


def test_confirm_success(client, monkeypatch):
    monkeypatch.setattr("app.routes.assistant.confirm",
                        lambda cid, sid: {"type": "executed", "result": {"ok": True}})
    data = _data(client.post("/api/v1/assistant/pending-confirmations:confirm",
                             json={"confirmation_id": "c1"}))
    assert data["type"] == "executed"


# ── 6. cancel（委托）────────────────────────────────────────────────────────────

def test_cancel_missing_id(client):
    _err(client.post("/api/v1/assistant/pending-confirmations:cancel", json={}), 400, 11202)


def test_cancel_success(client, monkeypatch):
    monkeypatch.setattr("app.routes.assistant.cancel",
                        lambda cid, sid: {"type": "cancelled"})
    data = _data(client.post("/api/v1/assistant/pending-confirmations:cancel",
                             json={"confirmation_id": "c1"}))
    assert data["type"] == "cancelled"


# ── 7. clear（委托·session）────────────────────────────────────────────────────

def test_clear_success(client):
    with client.session_transaction() as s:
        s["assistant_messages"] = [{"role": "user", "content": "hi"}]
    data = _data(client.post("/api/v1/assistant/sessions:clear"))
    assert data["message"] == "对话历史已清除"


# ── 8. history（委托·session·过滤）────────────────────────────────────────────

def test_history_filters_to_user_assistant(client):
    """api_history 只返回 user/assistant 的文本内容；tool/system 角色一律排除，
    且不向前端暴露 tool_calls（含工具函数名，违反规则7）；空内容（仅有 tool_calls
    无文本的 assistant 中间步骤）也不渲染。委托保留此过滤逻辑。"""
    with client.session_transaction() as s:
        s["assistant_messages"] = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "tool", "content": "tool-output"},          # 排除（tool 角色）
            {"role": "system", "content": "内部反馈"},            # 排除（system 角色）
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c", "function": {"name": "add_watermark"}}]},  # 排除（空内容，且 tool_calls 不暴露）
        ]
    data = _data(client.get("/api/v1/assistant/sessions/history"))
    roles = [m["role"] for m in data["messages"]]
    assert roles == ["user", "assistant"]
    assert data["messages"][0]["content"] == "hi"
    assert data["messages"][1]["content"] == "hello"
    # tool_calls 不出现在前端响应中（防内部工具名泄露）
    assert all("tool_calls" not in m for m in data["messages"])


def test_history_empty(client):
    data = _data(client.get("/api/v1/assistant/sessions/history"))
    assert data["messages"] == []


# ── 9. 阶段4：推流/标注 工具经真实 chat 工具循环（mock _call_openai 返 tool_call）──────

def _seed_video_wm(app):
    """seed videos(1)+watermarked_videos(1)，供 start_stream/start_auto_annotation 解析。"""
    with app.app_context():
        db = get_db()
        db.execute("INSERT INTO videos (id, filename, original_path, video_id, duration) "
                   "VALUES (1, 'v.mp4', 'o', '046-001', 10.0)")
        db.execute("INSERT INTO watermarked_videos (id, original_video_id, filename, output_path, duration) "
                   "VALUES (1, 1, 'v_wm.mp4', '/tmp/v.mp4', 10.0)")
        db.commit()


def _fake_completion(tool_name, args):
    """构造 OpenAI completion：choices[0].message.tool_calls=[{id,function{name,arguments}}]。"""
    args_str = json.dumps(args, ensure_ascii=False)
    fn = type("F", (), {"name": tool_name, "arguments": args_str})()
    tc = type("TC", (), {
        "id": "call_1",
        "function": fn,
        "model_dump": lambda self: {"id": "call_1",
                                     "function": {"name": tool_name, "arguments": args_str},
                                     "type": "function"},
    })()
    msg = type("M", (), {"content": None, "tool_calls": [tc]})()
    return type("C", (), {"choices": [type("Ch", (), {"message": msg})()]})()


def _fake_text_completion(text):
    msg = type("M", (), {"content": text, "tool_calls": None})()
    return type("C", (), {"choices": [type("Ch", (), {"message": msg})()]})()


def _mock_openai_first_toolcall(monkeypatch, tool_name, args):
    """mock is_configured(True) + _call_openai：第1次返 tool_call，第2次（confirm 回复）返纯文本。"""
    monkeypatch.setattr("app.routes.assistant.is_configured", lambda: True)
    monkeypatch.setattr("app.services.assistant_service.is_configured", lambda: True)
    calls = {"n": 0}

    def _openai(msgs, settings):
        calls["n"] += 1
        return _fake_completion(tool_name, args) if calls["n"] == 1 else _fake_text_completion("已执行")
    monkeypatch.setattr("app.services.assistant_service._call_openai", _openai)


def test_chat_start_stream_flow(app, client, monkeypatch):
    """start_stream：chat→确认卡片→confirm→execute 创建 stream_tasks 行（_start_task_internal mock 免真 ffmpeg）。"""
    _seed_video_wm(app)
    _mock_openai_first_toolcall(monkeypatch, "start_stream",
                                {"video_id": "046-001", "stream_name": "demo", "loop_count": 5})
    monkeypatch.setattr("app.routes.streaming._start_task_internal",
                        lambda tid, use_resume: (True, {"status": "running", "pid": 123,
                                                         "rtsp_urls": [{"iface": "lo", "url": "rtsp://x/demo"}]}))

    resp = client.post("/api/v1/assistant/chat", json={"message": "把视频 046 推到流 demo"})
    data = _data(resp)
    assert data["type"] == "confirmation_required"
    assert data["confirmation"]["action"] == "start_stream"
    cid = data["confirmation"]["id"]

    resp2 = client.post("/api/v1/assistant/pending-confirmations:confirm",
                        json={"confirmation_id": cid})
    assert resp2.status_code == 200
    # 委托触发：stream_tasks 行已创建
    with app.app_context():
        row = get_db().execute(
            "SELECT stream_name, status FROM stream_tasks ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None and row["stream_name"] == "demo"


def test_confirm_feedback_not_leaked_to_history(app, client, monkeypatch):
    """回归（规则7）：confirm() 把「确认+执行结果」反馈作为 role=system 注入（非 role=user），
    故 history 不向前端渲染这条内部指令（含内部工具名 start_stream 和「请用中文…告知结果」），
    只展示用户提问 + 助手自然语言回复。曾因 role=user 导致「用户已确认执行操作：add_watermark…」
    被前端当成用户气泡泄露给用户。"""
    _seed_video_wm(app)
    _mock_openai_first_toolcall(monkeypatch, "start_stream",
                                {"video_id": "046-001", "stream_name": "demo", "loop_count": 5})
    monkeypatch.setattr("app.routes.streaming._start_task_internal",
                        lambda tid, use_resume: (True, {"status": "running", "pid": 123,
                                                         "rtsp_urls": [{"iface": "lo", "url": "rtsp://x/demo"}]}))
    # chat → 确认卡片
    r1 = _data(client.post("/api/v1/assistant/chat", json={"message": "把视频 046 推到流 demo"}))
    assert r1["type"] == "confirmation_required"
    cid = r1["confirmation"]["id"]
    # confirm → 执行 + LLM 自然语言回复
    r2 = _data(client.post("/api/v1/assistant/pending-confirmations:confirm",
                           json={"confirmation_id": cid}))
    assert r2["type"] == "message"
    # history 不应含内部反馈指令，也不应含 system 角色
    hist = _data(client.get("/api/v1/assistant/sessions/history"))["messages"]
    roles = [m["role"] for m in hist]
    assert "system" not in roles
    contents = [m["content"] for m in hist]
    assert not any(c.startswith("用户已确认执行操作") for c in contents)
    # 用户提问 + 助手自然语言回复都在
    assert any("推到流" in c for c in contents)
    assert "已执行" in contents


def test_chat_start_auto_annotation_flow(app, client, monkeypatch):
    """start_auto_annotation：chat→确认卡片→confirm→execute 委托旧 start_task
    （_do_auto_annotation stub 免真 ffmpeg/模型），断言 auto_annotation_tasks 行落库 + 模块态更新。"""
    _seed_video_wm(app)
    _mock_openai_first_toolcall(monkeypatch, "start_auto_annotation",
                                {"video_db_id": 1, "event_types": ["fight"]})
    # stub 真 worker（起线程但 target 是 no-op）
    monkeypatch.setattr("app.routes.auto_annotation._do_auto_annotation", lambda *a, **k: None)

    resp = client.post("/api/v1/assistant/chat", json={"message": "自动标注视频 046"})
    data = _data(resp)
    assert data["type"] == "confirmation_required"
    assert data["confirmation"]["action"] == "start_auto_annotation"
    cid = data["confirmation"]["id"]

    resp2 = client.post("/api/v1/assistant/pending-confirmations:confirm",
                        json={"confirmation_id": cid})
    assert resp2.status_code == 200
    with app.app_context():
        row = get_db().execute(
            "SELECT video_db_id, status, event_types FROM auto_annotation_tasks ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None and row["video_db_id"] == 1
    # 模块态更新（start_task 置 _current_task_id）
    from app.routes import auto_annotation as _aa
    assert _aa._current_task_id is not None


def test_list_and_get_assistant_tasks_real(app):
    """list_assistant_tasks / get_task_status 真调用（不经 service mock）：回归
    get_task_progress 未导入的 NameError（曾导致 /assistant/api/chat 返 HTML 500）。"""
    from app.services.assistant_tools import list_assistant_tasks, get_task_status
    tid = _insert_task(app, task_type="add_watermark", status="running")
    with app.app_context():
        r = list_assistant_tasks()
        assert r["count"] == 1
        t = r["tasks"][0]
        assert t["task_type"] == "add_watermark"
        assert "progress" in t  # get_task_progress 已导入，不再 NameError
        gt = get_task_status(t["id"])
        assert gt["task"]["id"] == tid
        assert "progress" in gt


def test_chat_list_stream_tasks_read(app, client, monkeypatch):
    """list_stream_tasks（只读）：chat→_call_openai 返 tool_call→直接返结果（无确认卡片）。"""
    _seed_video_wm(app)
    with app.app_context():
        db = get_db()
        db.execute("INSERT INTO stream_tasks (name, source_type, source_id, stream_name, "
                   "loop_count, status) VALUES ('推流-demo', 'single', 1, 'demo', 1, 'running')")
        db.commit()
    # 第1次返 tool_call（只读工具，_execute_tool 直接返结果→tool_result→第2次 _call_openai 返总结文本）
    monkeypatch.setattr("app.routes.assistant.is_configured", lambda: True)
    monkeypatch.setattr("app.services.assistant_service.is_configured", lambda: True)
    calls = {"n": 0}
    def _openai(msgs, settings):
        calls["n"] += 1
        if calls["n"] == 1:
            return _fake_completion("list_stream_tasks", {})
        return _fake_text_completion("找到 1 个推流任务")
    monkeypatch.setattr("app.services.assistant_service._call_openai", _openai)

    resp = client.post("/api/v1/assistant/chat", json={"message": "推流任务列表"})
    data = _data(resp)
    # 只读工具走完 tool 循环后返最终文本回复
    assert data["type"] == "message"
    assert "推流" in data["message"]["content"]
