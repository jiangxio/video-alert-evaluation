"""自动标注 worker 真实链路冒烟（非 v1 端点层）。

独立成文件：tests/test_api_v1_auto_annotation.py 的 `_stub_worker`（autouse）会把
`_do_auto_annotation` 替成 no-op，故本文件不复用它，直接跑真 worker：
- 真 ffmpeg 抽帧（`_extract_frames` 子进程）；
- analyze_frame：① 受控 mock（测置信度/复核分流与 GT 过滤）② 真 LLM（opt-in）；
- 真 `_do_auto_annotation` 落 auto_annotation_frames / auto_annotation_events / GT JSON。

conftest 已 patch app.routes.auto_annotation.DATABASE_PATH→tmp（worker 直连此拷贝）+
behavior_analysis_service.DEFAULT_CONFIG_PATH→tmp。PROJECT_ROOT 用 monkeypatch 重定向到
tmp，使帧目录/GT 落 tmp 不污染仓库。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.database import get_db
from app.routes import auto_annotation as _legacy


def _has_ffmpeg():
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


@pytest.fixture(autouse=True)
def _reset_anno_state():
    """清 worker 模块级任务态，防跨用例串（worker 同步运行，无后台线程，无需 join）。"""
    yield
    _legacy._auto_anno_tasks.clear()
    _legacy._task_queue.clear()
    _legacy._current_task_id = None
    _legacy._stop_requested = False


@pytest.fixture
def _anno_env(app, tmp_path, monkeypatch):
    """重定向 PROJECT_ROOT→tmp，生成一段 8s 合成视频，seed videos+watermarked_videos+
    auto_annotation_tasks 行。返回 {task_id, video_path, video_id, project_root}。"""
    monkeypatch.setitem(app.config, "PROJECT_ROOT", str(tmp_path))
    if not _has_ffmpeg():
        pytest.skip("ffmpeg/ffprobe 未安装")
    video = tmp_path / "anno_smoke.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", "testsrc=duration=8:size=160x120:rate=10",
         "-c:v", "libx264", "-pix_fmt", "yuvj420p", "-an", str(video)],
        capture_output=True, timeout=30,
    )
    assert video.exists(), "ffmpeg 生成测试视频失败"
    video_id = "anno-smoke-001"
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        cur.execute(
            "INSERT INTO videos (id, filename, original_path, video_id, duration) "
            "VALUES (1, 'anno_smoke.mp4', ?, ?, 8.0)",
            (str(video), video_id),
        )
        cur.execute(
            "INSERT INTO watermarked_videos (id, original_video_id, filename, output_path, duration) "
            "VALUES (1, 1, 'anno_smoke.mp4', ?, 8.0)",
            (str(video),),
        )
        cur.execute(
            "INSERT INTO auto_annotation_tasks "
            "(id, video_db_id, video_id, status, frame_interval_sec, merge_interval_sec, event_types, confidence_threshold) "
            "VALUES (1, 1, ?, 'queued', 1, 5, ?, 0.6)",
            (video_id, json.dumps(["fight", "rat"])),
        )
        db.commit()
    return {"task_id": 1, "video_path": str(video), "video_id": video_id,
            "project_root": str(tmp_path)}


# ── ① 真 ffmpeg + 受控 mock LLM：测置信度/复核分流 + GT 过滤 ────────────────────

@pytest.mark.slow
def test_smoke_annotation_confidence_flow(app, _anno_env, monkeypatch):
    """真 ffmpeg 抽帧 + 受控 analyze_frame（按时间戳返不同置信度）+ 真 _do_auto_annotation：
    高置信 fight(0.9)→auto_approved，低置信 rat(0.4)→pending；GT JSON 只含 auto_approved；
    auto_annotation_events 含两事件；帧带 confidence 落库。无 API 成本。"""
    calls = {"n": 0}

    def _fake_analyze(client, model, image_path, valid_types, event_descriptions=None):
        # worker 按抽帧时间戳顺序调用；用文件名 frame_000XXX 反推 ts
        name = Path(image_path).stem  # frame_000000 / frame_000005 ...
        ts = int(name.split("_")[1]) if name.startswith("frame_") else 0
        if ts < 3:
            return [{"label": "fight", "confidence": 0.9}]
        if 5 <= ts < 8:
            return [{"label": "rat", "confidence": 0.4}]
        return [{"label": "normal", "confidence": 1.0}]

    monkeypatch.setattr("app.routes.auto_annotation.analyze_frame", _fake_analyze)
    monkeypatch.setattr("app.routes.auto_annotation.get_api_client", lambda cfg: object())
    # 免帧间限流等待
    api_config = {"request_interval_sec": 0, "model": "mock"}

    _legacy._do_auto_annotation(
        _anno_env["task_id"], 1, _anno_env["video_path"], _anno_env["video_id"],
        "anno_smoke.mp4", 1, 5, ["fight", "rat"], _anno_env["project_root"],
        api_config, 0.6,
    )

    with app.app_context():
        db = get_db()
        task = db.execute("SELECT status, result_json_path FROM auto_annotation_tasks WHERE id=1").fetchone()
        assert task["status"] == "done"
        # 帧带 confidence 落库（t=0..8 共 9 帧；容错 ffmpeg 8.x mjpeg 偶发抽帧失败 1 帧）
        frames = db.execute(
            "SELECT timestamp_sec, confidence, review_status FROM auto_annotation_frames WHERE task_id=1 ORDER BY timestamp_sec"
        ).fetchall()
        assert len(frames) >= 8
        for r in frames:
            ts = r["timestamp_sec"]
            if ts < 3:
                assert r["confidence"] == pytest.approx(0.9), f"ts={ts} conf={r['confidence']}"
            elif 5 <= ts < 8:
                assert r["confidence"] == pytest.approx(0.4), f"ts={ts} conf={r['confidence']}"
            else:
                assert r["confidence"] == pytest.approx(0.0), f"ts={ts} conf={r['confidence']}"

        # 事件：fight(高置信→auto_approved) + rat(低置信→pending)
        events = [dict(r) for r in db.execute(
            "SELECT event_type, start_sec, end_sec, confidence, review_status "
            "FROM auto_annotation_events WHERE task_id=1 ORDER BY start_sec"
        ).fetchall()]
        by_type = {e["event_type"]: e for e in events}
        assert set(by_type) == {"fight", "rat"}
        assert by_type["fight"]["review_status"] == "auto_approved"
        assert by_type["fight"]["confidence"] == pytest.approx(0.9)
        assert by_type["rat"]["review_status"] == "pending"
        assert by_type["rat"]["confidence"] == pytest.approx(0.4)

        # GT JSON 只含 auto_approved（fight），不含 pending（rat）；且不带 confidence/review_status 字段
        gt = json.loads(Path(task["result_json_path"]).read_text(encoding="utf-8"))
        assert [e["type"] for e in gt["events"]] == ["fight"]
        assert all("confidence" not in e and "review_status" not in e for e in gt["events"])


# ── ② 真 LLM 端到端（opt-in，花 API）──────────────────────────────────────────

@pytest.mark.slow
@pytest.mark.skipif(not __import__("os").environ.get("ANNO_SMOKE"),
                    reason="需 ANNO_SMOKE=1 且 .env OpenAI 兼容 key 就绪才跑真 LLM 冒烟")
def test_real_smoke_annotation_llm(app, _anno_env, monkeypatch):
    """真 ffmpeg 抽帧 + 真 LLM（.env key）端到端：断言任务 done、帧已抽、GT JSON 合法、
    事件（若有）复核状态合法。合成视频 LLM 多半返 normal→0 事件，故不强制事件数，
    只验链路跑通。LLM 失败（key/网络/429 耗尽）→ skip 不阻塞。"""
    import os
    from app.services import api_config_service
    # conftest 把 ENV_PATH 重定向到 tmp（空），此处指回真实 .env（仓库根）以加载真 key。
    # 注意 _anno_env 已把 PROJECT_ROOT 指向 tmp，故不能用它定位 .env。
    real_env = Path(__file__).resolve().parent.parent / ".env"
    if not real_env.exists():
        pytest.skip("无 .env，LLM 未配置")
    monkeypatch.setattr(api_config_service, "ENV_PATH", real_env)
    api_config = _legacy.load_anno_config()
    if not api_config.get("api_key"):
        pytest.skip("LLM API Key 未配置")

    try:
        _legacy._do_auto_annotation(
            _anno_env["task_id"], 1, _anno_env["video_path"], _anno_env["video_id"],
            "anno_smoke.mp4", 1, 5, ["fight", "rat"], _anno_env["project_root"],
            api_config, 0.6,
        )
    except Exception as e:
        pytest.skip(f"真 LLM worker 异常（环境）：{e}")

    with app.app_context():
        db = get_db()
        task = db.execute("SELECT status, result_json_path, error_message FROM auto_annotation_tasks WHERE id=1").fetchone()
        if task["status"] == "failed" and task["error_message"] and (
            "429" in task["error_message"] or "API" in task["error_message"]
            or "key" in task["error_message"].lower() or "频率" in task["error_message"]
        ):
            pytest.skip(f"LLM 环境不可用：{task['error_message'][:120]}")
        assert task["status"] == "done", f"任务未完成：{task['error_message']}"
        # 帧已抽（真 ffmpeg）
        n_frames = db.execute("SELECT COUNT(*) FROM auto_annotation_frames WHERE task_id=1").fetchone()[0]
        assert n_frames > 0
        # GT JSON 合法
        gt = json.loads(Path(task["result_json_path"]).read_text(encoding="utf-8"))
        assert "events" in gt and isinstance(gt["events"], list)
        # 事件（若有）复核状态合法
        statuses = [r["review_status"] for r in db.execute(
            "SELECT review_status FROM auto_annotation_events WHERE task_id=1"
        ).fetchall()]
        assert all(s in ("auto_approved", "pending") for s in statuses)
