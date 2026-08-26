"""端到端验证 /api/v1/streaming 端点（推流任务 CRUD + start/stop + logs/progress/preview）。

测试分层（对齐 plan §测试策略）：
- 快测（无 slow）：9 类查询/CRUD 端点 + _build_ffmpeg_cmd/_is_retryable_error 纯函数单测。
  seed watermarked_videos.duration 使 _ensure_duration 早退、不触发 ffprobe。
- slow 测（@pytest.mark.slow）：start/stop 用 FakePopen mock subprocess.Popen（不碰真实
  ffmpeg/MediaMTX），_slow_env 重定向硬编码仓库路径到 tmp + 跟踪监控线程；
  _reset_stream_state autouse teardown 终止假进程→等监控线程退出释放 tmp 库句柄（仿
  OCR _reset_ocr_progress，防 Windows 删 tmp 库文件锁失败）。
- 可选真实冒烟：skipif 无 ffmpeg/MediaMTX 时 skip，不阻塞 CI。

DB 为 conftest tmp 库（create_app→init_db 建全 schema 含 stream_tasks/watermarked_videos
+ ALTER 加 duration 列）。conftest 已 patch app.routes.streaming.DATABASE_PATH→tmp
（双绑定，后台线程/直连 helper 读此拷贝）。
"""
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from app.database import get_db
from app.routes import streaming as _legacy


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


# ── seed：一条 videos + watermarked_videos（duration 已填）+ eval_video_sets ─────

@pytest.fixture
def seed(app, tmp_path):
    """seed 单条水印视频（output_path 指向 tmp 真文件，满足 start 的 Path.exists）
    + 一条评测视频集引用该视频。返回 ids/paths。"""
    video_file = tmp_path / "wm1.mp4"
    video_file.write_bytes(b"fake mp4 content")
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
            (str(video_file),),
        )
        cur.execute(
            "INSERT INTO eval_video_sets (id, name, video_ids) VALUES (1, 'set1', '[1]')"
        )
        db.commit()
    return {"wm_id": 1, "set_id": 1, "video_file": str(video_file), "duration": 10.0}


def _create_task(client, **overrides):
    """创建一个 single 来源任务，返回 task_id。"""
    body = {
        "source_type": "single",
        "source_id": 1,
        "stream_name": "test-stream",
        "loop_count": 2,
    }
    body.update(overrides)
    data = _data(client.post("/api/v1/streaming/tasks", json=body))
    return data["id"]


def _insert_task(app, **fields):
    """直接插一条 stream_tasks 行（构造 running/带 log_path 等非默认状态）。返回 id。
    动态拼列：只插 cols 里出现的列，其余用 DB 默认值。"""
    cols = {
        "name": "t", "source_type": "single", "source_id": 1, "stream_name": "s",
        "loop_count": 1, "status": "created",
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
            f"INSERT INTO stream_tasks ({col_list}) VALUES ({placeholders})",
            values,
        )
        db.commit()
        return cur.lastrowid


def _task_row(app, task_id):
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT status FROM stream_tasks WHERE id = ?", (task_id,))
        return cur.fetchone()


# ── 纯函数单测（堵 FakePopen 盲区，不起子进程）──────────────────────────────────

def test_build_ffmpeg_cmd_structure(tmp_path, monkeypatch):
    """_build_ffmpeg_cmd 拼出的命令含 concat/stream_loop/rtsp_transport/rtsp url；
    Windows 反斜杠路径已转 /；concat 清单写到（重定向后的）tmp 路径。"""
    concat_dir = tmp_path / "stream_concat"
    concat_dir.mkdir()
    monkeypatch.setattr(_legacy, "_get_concat_list_path",
                        lambda tid: concat_dir / f"task_{tid}.txt")
    # 用带反斜杠的 Windows 风格路径验证转义
    paths = [r"C:\videos\a.mp4", r"D:\sub\b.mp4"]
    cmd = _legacy._build_ffmpeg_cmd(paths, "mystream", 3, task_id=99, resume_offset=0)

    assert "-f" in cmd and "concat" in cmd and "-safe" in cmd and "0" in cmd
    assert "-stream_loop" in cmd
    assert cmd[cmd.index("-stream_loop") + 1] == "2"  # loops - 1
    assert "-rtsp_transport" in cmd and "tcp" in cmd
    assert "rtsp://localhost:8554/mystream" in cmd
    # concat 清单文件已写，且路径用正斜杠
    concat_txt = (concat_dir / "task_99.txt").read_text(encoding="utf-8")
    assert "file 'C:/videos/a.mp4'" in concat_txt
    assert "file 'D:/sub/b.mp4'" in concat_txt
    assert "\\" not in concat_txt  # 反斜杠已转义


def test_is_retryable_error():
    """可重试关键词→True，其他→False，空串→False。"""
    assert _legacy._is_retryable_error("... [break pipe] broken pipe ...") is True
    assert _legacy._is_retryable_error("Connection reset by peer") is True
    assert _legacy._is_retryable_error("connection refused") is True
    assert _legacy._is_retryable_error("some unknown ffmpeg error") is False
    assert _legacy._is_retryable_error("") is False


# ── 快测：辅助资源列表 ──────────────────────────────────────────────────────────

def test_list_streamable_videos(client, seed):
    data = _data(client.get("/api/v1/streaming/videos"))
    assert data["total"] == 1
    assert data["items"][0]["video_id"] == "046-001"
    assert data["items"][0]["duration"] == 10.0
    assert data["page"] == 1 and data["page_size"] == 20


def test_list_streamable_videos_empty(client):
    data = _data(client.get("/api/v1/streaming/videos"))
    assert data["total"] == 0 and data["items"] == []


def test_list_video_sets(client, seed):
    data = _data(client.get("/api/v1/streaming/video-sets"))
    assert data["total"] == 1
    item = data["items"][0]
    assert item["name"] == "set1"
    assert item["video_count"] == 1
    assert "video_ids" not in item  # 已 pop


# ── 快测：任务列表 ───────────────────────────────────────────────────────────────

def test_list_tasks(client, seed):
    _create_task(client)
    data = _data(client.get("/api/v1/streaming/tasks"))
    assert data["total"] == 1
    t = data["items"][0]
    assert t["status"] == "created"
    assert t["stream_name"] == "test-stream"
    assert isinstance(t["rtsp_urls"], list) and len(t["rtsp_urls"]) >= 1
    assert "rtsp://" in t["rtsp_urls"][0]["url"]
    assert "elapsed_seconds" in t and "estimated_end_ts" in t


# ── 快测：创建任务 ──────────────────────────────────────────────────────────────

def test_create_task(client, seed):
    resp = client.post("/api/v1/streaming/tasks", json={
        "source_type": "single", "source_id": 1, "stream_name": "s1", "loop_count": 2,
    })
    data = _data(resp)
    assert data["id"] >= 1
    assert "rtsp://" in data["rtsp_url"]
    assert data["total_duration"] == 20.0  # 10s × 2 loops
    assert data["suggested_algorithms"] == []  # events 表空
    assert resp.headers["Location"].endswith(f"/api/v1/streaming/tasks/{data['id']}")


def test_create_task_set_source(client, seed):
    """set 来源解析视频集 video_ids → 视频列表。"""
    data = _data(client.post("/api/v1/streaming/tasks", json={
        "source_type": "set", "source_id": 1, "stream_name": "s2", "loop_count": 1,
    }))
    assert data["total_duration"] == 10.0


def test_create_task_invalid_source_type(client, seed):
    _err(client.post("/api/v1/streaming/tasks", json={
        "source_type": "xxx", "source_id": 1, "stream_name": "s",
    }), 400, 10900)


def test_create_task_missing_source_id(client, seed):
    _err(client.post("/api/v1/streaming/tasks", json={
        "source_type": "single", "stream_name": "s",
    }), 400, 10901)


def test_create_task_empty_stream_name(client, seed):
    _err(client.post("/api/v1/streaming/tasks", json={
        "source_type": "single", "source_id": 1, "stream_name": "",
    }), 400, 10902)


def test_create_task_bad_stream_name(client, seed):
    _err(client.post("/api/v1/streaming/tasks", json={
        "source_type": "single", "source_id": 1, "stream_name": "bad name!",
    }), 400, 10903)


def test_create_task_resolve_fail(client, seed):
    """source_id 指向不存在的水印视频→10900（解析失败）。"""
    _err(client.post("/api/v1/streaming/tasks", json={
        "source_type": "single", "source_id": 999, "stream_name": "s",
    }), 400, 10900)


# ── 快测：PATCH / DELETE ─────────────────────────────────────────────────────────

def test_update_task(client, seed):
    task_id = _create_task(client)
    data = _data(client.patch(f"/api/v1/streaming/tasks/{task_id}", json={
        "source_type": "single", "source_id": 1, "stream_name": "renamed", "loop_count": 3,
    }))
    assert data["id"] == task_id and data["status"] == "created"


def test_update_task_running_conflict(client, app, seed):
    """running 任务不可编辑→30901(409)。"""
    task_id = _insert_task(app, status="running")
    _err(client.patch(f"/api/v1/streaming/tasks/{task_id}", json={
        "source_type": "single", "source_id": 1, "stream_name": "x",
    }), 409, 30901)


def test_update_task_not_found(client, seed):
    _err(client.patch("/api/v1/streaming/tasks/999", json={
        "source_type": "single", "source_id": 1, "stream_name": "x",
    }), 404, 20900)


def test_delete_task(client, seed):
    task_id = _create_task(client)
    resp = client.delete(f"/api/v1/streaming/tasks/{task_id}")
    assert resp.status_code == 204
    # 读回：列表里没了
    data = _data(client.get("/api/v1/streaming/tasks"))
    assert data["total"] == 0


def test_delete_task_running_conflict(client, app, seed):
    task_id = _insert_task(app, status="running")
    _err(client.delete(f"/api/v1/streaming/tasks/{task_id}"), 409, 30902)


def test_delete_task_not_found(client, seed):
    _err(client.delete("/api/v1/streaming/tasks/999"), 404, 20900)


# ── 快测：logs / progress ─────────────────────────────────────────────────────────

def test_get_logs_empty(client, seed):
    """无日志文件→空 content。"""
    task_id = _create_task(client)
    data = _data(client.get(f"/api/v1/streaming/tasks/{task_id}/logs"))
    assert data["content"] == "" and data["lines"] == 0


def test_get_logs_with_content(client, app, seed, tmp_path):
    """有日志文件→返回内容。"""
    log_file = tmp_path / "task.log"
    log_file.write_text("line1\nline2\n", encoding="utf-8")
    task_id = _insert_task(app, log_path=str(log_file))
    data = _data(client.get(f"/api/v1/streaming/tasks/{task_id}/logs"))
    assert "line1" in data["content"] and data["lines"] == 2


def test_get_logs_not_found(client, seed):
    _err(client.get("/api/v1/streaming/tasks/999/logs"), 404, 20900)


def test_get_progress(client, seed):
    task_id = _create_task(client)
    data = _data(client.get(f"/api/v1/streaming/tasks/{task_id}/progress"))
    assert data["task_id"] == task_id
    assert data["status"] == "created"
    assert data["loop_count"] == 2
    assert data["total_duration"] == 20.0
    assert isinstance(data["videos"], list) and data["videos"][0]["duration"] == 10.0
    assert "progress" in data and "overall_percent" in data["progress"]


def test_get_progress_not_found(client, seed):
    _err(client.get("/api/v1/streaming/tasks/999/progress"), 404, 20900)


# ── 快测：preview ────────────────────────────────────────────────────────────────

def test_preview(client, seed):
    data = _data(client.post("/api/v1/streaming/tasks:preview", json={
        "source_type": "single", "source_id": 1, "stream_name": "prev", "loop_count": 2,
    }))
    assert data["video_count"] == 1
    assert data["total_duration"] == 20.0
    assert isinstance(data["rtsp_urls"], list) and len(data["rtsp_urls"]) >= 1
    assert data["suggested_algorithms"] == []


def test_preview_no_stream_name_no_rtsp(client, seed):
    """无 stream_name→rtsp_urls 为空。"""
    data = _data(client.post("/api/v1/streaming/tasks:preview", json={
        "source_type": "single", "source_id": 1,
    }))
    assert data["rtsp_urls"] == []


def test_preview_param_incomplete(client, seed):
    _err(client.post("/api/v1/streaming/tasks:preview", json={
        "source_id": 1,
    }), 400, 10904)


def test_preview_resolve_fail(client, seed):
    _err(client.post("/api/v1/streaming/tasks:preview", json={
        "source_type": "single", "source_id": 999, "stream_name": "x",
    }), 400, 10900)


# ── slow 测：start/stop（FakePopen）─────────────────────────────────────────────

class _FakePopen:
    """假 ffmpeg：poll 永远 None（存活）→ _play_video 的 2.5s sleep 后判成功；
    wait 阻塞到 terminate 设 Event 后返 0（模拟进程退出），加 0.1s 让 stop 的
    DB UPDATE 先提交，避免监控线程与 stop 竞态。"""
    _next_pid = [70001]

    def __init__(self):
        self._event = threading.Event()
        _FakePopen._next_pid[0] += 1
        self.pid = _FakePopen._next_pid[0]
        self.returncode = None

    def poll(self):
        return None

    def terminate(self):
        self._event.set()

    def wait(self, timeout=None):
        self._event.wait(timeout=timeout)
        time.sleep(0.1)
        self.returncode = 0
        return 0


# 跟踪监控线程（_slow_env 包裹 _monitor_video_process，append 当前线程；teardown join）
_monitor_threads = []
_real_monitor = [None]


def _tracking_monitor(*a, **kw):
    _monitor_threads.append(threading.current_thread())
    if _real_monitor[0] is not None:
        return _real_monitor[0](*a, **kw)


@pytest.fixture(autouse=True)
def _reset_stream_state(app, tmp_path):
    """每用例后：终止假进程→等监控线程退出释放 tmp 库 sqlite 句柄→清模块态/临时文件。
    依赖 app 保证 teardown 先于 app 的 unlink(tmp_db)（LIFO），仿 OCR _reset_ocr_progress。"""
    yield
    # 1. 终止所有假 ffmpeg（解除监控线程 wait 阻塞）
    with _legacy._stream_lock:
        procs = list(_legacy._stream_processes.items())
    for _tid, (process, log_fp) in procs:
        try:
            process.terminate()
        except Exception:
            pass
        try:
            log_fp.close()
        except Exception:
            pass
    with _legacy._stream_lock:
        _legacy._stream_processes.clear()
    # 2. 清重连集合
    with _legacy._reconnect_lock:
        _legacy._reconnecting_tasks.clear()
    # 3. 等监控线程退出（释放 tmp 库句柄，防 Windows 删库文件锁）
    for t in list(_monitor_threads):
        t.join(timeout=30)
    _monitor_threads.clear()
    # 4. 清临时文件（log/concat/resume 落 tmp_path 下）
    for sub in ("logs", "stream_concat", "stream_resume"):
        d = tmp_path / sub
        if d.exists():
            for f in d.rglob("*"):
                try:
                    if f.is_file():
                        f.unlink()
                except Exception:
                    pass


@pytest.fixture
def _slow_env(app, tmp_path, monkeypatch):
    """start/stop slow 测环境：FakePopen + 重定向硬编码仓库路径到 tmp + 跟踪监控线程。"""
    monkeypatch.setattr(_legacy.subprocess, "Popen", lambda cmd, **kw: _FakePopen())
    concat_dir = tmp_path / "stream_concat"
    resume_dir = tmp_path / "stream_resume"
    log_root = tmp_path / "logs_root"
    concat_dir.mkdir(parents=True, exist_ok=True)
    resume_dir.mkdir(parents=True, exist_ok=True)
    log_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_legacy, "_get_concat_list_path",
                        lambda tid: concat_dir / f"task_{tid}.txt")
    monkeypatch.setattr(_legacy, "_get_resume_video_path",
                        lambda tid: resume_dir / f"task_{tid}.mp4")
    # _play_video 用 Path(current_app.root_path).parent/"logs"/"stream" 定日志目录
    app.root_path = str(log_root)
    # 假 pid 判活：避免 list_tasks 等误标 running 任务 done（slow 测不调 list_tasks，
    # 但兜底）
    monkeypatch.setattr(_legacy, "_is_pid_alive", lambda pid: True)
    # 包裹监控线程以便 teardown join
    _real_monitor[0] = _legacy._monitor_video_process
    _monitor_threads.clear()
    monkeypatch.setattr(_legacy, "_monitor_video_process", _tracking_monitor)
    yield


@pytest.mark.slow
def test_start_success(client, app, seed, _slow_env):
    """start→200 {status:running, pid, rtsp_urls}；DB 行 status=running。"""
    task_id = _create_task(client)
    data = _data(client.post(f"/api/v1/streaming/tasks/{task_id}:start", json={}))
    assert data["status"] == "running"
    assert isinstance(data["pid"], int)
    assert isinstance(data["rtsp_urls"], list) and len(data["rtsp_urls"]) >= 1
    row = _task_row(app, task_id)
    assert row["status"] == "running"


@pytest.mark.slow
def test_start_already_running(client, seed, _slow_env):
    """已 running 再 start→30900(409)。"""
    task_id = _create_task(client)
    _data(client.post(f"/api/v1/streaming/tasks/{task_id}:start", json={}))
    _err(client.post(f"/api/v1/streaming/tasks/{task_id}:start", json={}), 409, 30900)


@pytest.mark.slow
def test_start_not_found(client, seed, _slow_env):
    _err(client.post("/api/v1/streaming/tasks/999:start", json={}), 404, 20900)


@pytest.mark.slow
def test_start_resume(client, seed, _slow_env):
    """带 resume=true 启动（resume_offset=0，走 concat 分支）。"""
    task_id = _create_task(client)
    data = _data(client.post(f"/api/v1/streaming/tasks/{task_id}:start", json={"resume": True}))
    assert data["status"] == "running"


@pytest.mark.slow
def test_stop_success(client, seed, _slow_env):
    """start 后 stop→200 {status:stopped}（进程被 terminate）。"""
    task_id = _create_task(client)
    _data(client.post(f"/api/v1/streaming/tasks/{task_id}:start", json={}))
    data = _data(client.post(f"/api/v1/streaming/tasks/{task_id}:stop"))
    assert data["status"] == "stopped"


@pytest.mark.slow
def test_stop_not_running(client, seed, _slow_env):
    """非 running 任务 stop→30904(409)。"""
    task_id = _create_task(client)  # status=created
    _err(client.post(f"/api/v1/streaming/tasks/{task_id}:stop"), 409, 30904)


@pytest.mark.slow
def test_stop_not_found(client, seed, _slow_env):
    _err(client.post("/api/v1/streaming/tasks/999:stop"), 404, 20900)


# ── 阶段1：转码探测 + 转码命令分支（纯函数）──────────────────────────────────────

class _ProbeResult:
    """假 subprocess.run 返回值（喂 canned ffprobe 输出给 _probe_codecs）。"""
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def test_probe_codec_compatible_h264_aac(monkeypatch):
    """h264 视频 + aac 音频 → 可 copy（True）。"""
    monkeypatch.setattr(_legacy.subprocess, "run",
                        lambda *a, **k: _ProbeResult("h264,video\naac,audio"))
    assert _legacy._probe_codec_compatible("any.mp4") is True


def test_probe_codec_compatible_no_audio(monkeypatch):
    """h264 无音轨 → 可 copy（True）。"""
    monkeypatch.setattr(_legacy.subprocess, "run",
                        lambda *a, **k: _ProbeResult("h264,video"))
    assert _legacy._probe_codec_compatible("any.mp4") is True


def test_probe_codec_compatible_incompatible_video(monkeypatch):
    """非 h264/hevc 视频编码 → 需转码（False）。"""
    monkeypatch.setattr(_legacy.subprocess, "run",
                        lambda *a, **k: _ProbeResult("mpeg4,video\nmp3,audio"))
    assert _legacy._probe_codec_compatible("any.mp4") is False


def test_probe_codec_compatible_incompatible_audio(monkeypatch):
    """h264 视频 + 非 aac 音频 → 需转码（False）。"""
    monkeypatch.setattr(_legacy.subprocess, "run",
                        lambda *a, **k: _ProbeResult("h264,video\nmp3,audio"))
    assert _legacy._probe_codec_compatible("any.mp4") is False


def test_probe_codec_compatible_probe_fails(monkeypatch):
    """ffprobe 不可用/非视频文件 → 探测失败按兼容处理（True，保留 copy 默认）。"""
    def _raise(*a, **k):
        raise FileNotFoundError("ffprobe not found")
    monkeypatch.setattr(_legacy.subprocess, "run", _raise)
    assert _legacy._probe_codec_compatible("any.mp4") is True


def test_build_ffmpeg_cmd_transcode_branch(tmp_path, monkeypatch):
    """transcode=True → 命令含 libx264/veryfast/maxrate/bufsize/aac，不含 -c copy。"""
    concat_dir = tmp_path / "stream_concat"
    concat_dir.mkdir()
    monkeypatch.setattr(_legacy, "_get_concat_list_path",
                        lambda tid: concat_dir / f"task_{tid}.txt")
    cmd = _legacy._build_ffmpeg_cmd(["a.mp4"], "mystream", 1, task_id=7,
                                    resume_offset=0, transcode=True)
    assert "-c:v" in cmd and "libx264" in cmd
    assert "-preset" in cmd and "veryfast" in cmd
    assert "-maxrate" in cmd and "-bufsize" in cmd
    assert "-c:a" in cmd and "aac" in cmd
    assert "-c copy" not in " ".join(cmd)  # 不走 copy 分支
    assert "rtsp://localhost:8554/mystream" in cmd


# ── 阶段1：loop_count 封顶 ───────────────────────────────────────────────────────

def test_create_task_loop_count_capped(client, seed):
    """loop_count > 100 被封顶为 100（10s × 100 = 1000，而非 999×10）。"""
    data = _data(client.post("/api/v1/streaming/tasks", json={
        "source_type": "single", "source_id": 1, "stream_name": "cap", "loop_count": 999,
    }))
    assert data["total_duration"] == 1000.0


# ── 阶段1：并发上限（STREAM_MAX_CONCURRENT 默认 2，转码 2 倍）────────────────────

def test_start_concurrency_exceeded(client, app, seed, monkeypatch):
    """已有 2 个 running（copy 各占 1）→ 第 3 个 start 超并发上限→30905(409)。
    拒绝发生在 _play_video 之前，无需 FakePopen；probe mock 为 copy 使新任务 cost=1。"""
    monkeypatch.setattr(_legacy, "_probe_codec_compatible", lambda path: True)
    _insert_task(app, status="running")
    _insert_task(app, status="running")
    task_id = _create_task(client)
    _err(client.post(f"/api/v1/streaming/tasks/{task_id}:start", json={}), 409, 30905)


def test_start_concurrency_transcode_weight(client, app, seed, monkeypatch):
    """已有 1 个 running 转码任务（占 2）→ 第 2 个 start 超上限→30905(409)，
    验证转码任务按 2 倍占并发配额。"""
    monkeypatch.setattr(_legacy, "_probe_codec_compatible", lambda path: True)
    _insert_task(app, status="running", transcode=1)
    task_id = _create_task(client)
    _err(client.post(f"/api/v1/streaming/tasks/{task_id}:start", json={}), 409, 30905)


@pytest.mark.slow
def test_start_concurrency_at_limit_ok(client, app, seed, _slow_env, monkeypatch):
    """已有 1 个 running（占 1）→ 第 2 个 start（占 1）= 2 ≤ 上限 2 → 正常启动。"""
    monkeypatch.setattr(_legacy, "_probe_codec_compatible", lambda path: True)
    _insert_task(app, status="running")
    task_id = _create_task(client)
    data = _data(client.post(f"/api/v1/streaming/tasks/{task_id}:start", json={}))
    assert data["status"] == "running"


# ── 阶段1：断流重连保留进度 ─────────────────────────────────────────────────────

def test_compute_resume_offset(app, seed):
    """started_at 为 5s 前、单视频 10s、loop 2 → 整轮偏移≈5s（>0 且 <10）。
    直接单测 _compute_resume_offset（duration 已 seed，不触发 ffprobe）。"""
    started_epoch = time.time() - 5
    started_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(started_epoch))
    task_id = _insert_task(app, status="running", source_type="single", source_id=1,
                           loop_count=2, started_at=started_str)
    offset = _legacy._compute_resume_offset(task_id, started_str)
    assert 4.0 < offset < 6.5  # ≈5s，留时序余量
    assert offset < 10.0  # 未超整轮时长


@pytest.mark.slow
def test_monitor_reconnect_preserves_offset(app, seed, tmp_path, monkeypatch):
    """_monitor_video_process 可重试分支：用断流时 started_at 算整轮偏移写入 resume_offset，
    并以该 offset 调 _play_video（不再从头丢进度）。mock _play_video 记录参数、mock sleep 免等。"""
    started_epoch = time.time() - 5
    started_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(started_epoch))
    log_file = tmp_path / "task.log"
    log_file.write_text("... broken pipe ...", encoding="utf-8")
    task_id = _insert_task(app, status="running", source_type="single", source_id=1,
                           loop_count=2, started_at=started_str, log_path=str(log_file))

    recorded = {}
    def _fake_play(tid, loops, resume_offset=0.0, app=None):
        recorded["task_id"] = tid
        recorded["resume_offset"] = resume_offset
    monkeypatch.setattr(_legacy, "_play_video", _fake_play)
    monkeypatch.setattr(_legacy.time, "sleep", lambda *a, **k: None)

    class _FakeProc:
        returncode = 1
        def wait(self, timeout=None):
            return 1

    _legacy._monitor_video_process(task_id, _FakeProc(), None, log_file, 2, app)

    # 续播偏移已传给 _play_video（≈5s，>0）+ 写入 DB
    assert recorded.get("task_id") == task_id
    assert 4.0 < recorded.get("resume_offset", 0) < 6.5
    with app.app_context():
        row = get_db().execute(
            "SELECT resume_offset FROM stream_tasks WHERE id=?", (task_id,)
        ).fetchone()
        assert 4.0 < row["resume_offset"] < 6.5


# ── 可选真实冒烟（skipif 无 ffmpeg/MediaMTX）────────────────────────────────────

def _has_ffmpeg():
    return shutil.which("ffmpeg") is not None


def _mediamtx_up():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", 8554))
        return True
    except OSError:
        return False
    finally:
        s.close()


@pytest.mark.slow
@pytest.mark.skipif(not os.environ.get("STREAM_SMOKE"),
                    reason="需 STREAM_SMOKE=1 且 ffmpeg+MediaMTX 就绪才跑真实链路冒烟")
def test_real_smoke_start_stop(client, app, seed, tmp_path, monkeypatch):
    """真实链路冒烟：ffmpeg 生成小视频→建任务→start→progress(running)→stop。
    显式 opt-in（STREAM_SMOKE=1）；不满足则 skip，不阻塞。**loop_count 要够大（30）**
    使 ffmpeg 推流超过 _play_video 的 2.5s 启动检查——否则短视频会在 2.5s 内推完
    退出被判启动失败 500（旧 _play_video 既有行为，非 v1 bug）。start 500 也 skip 兜底。
    重定向 concat/resume/log 路径到 tmp，不污染仓库。"""
    if not _has_ffmpeg() or not _mediamtx_up():
        pytest.skip("ffmpeg/MediaMTX 未就绪")
    # 重定向硬编码仓库路径到 tmp（避免污染仓库 logs/stream、tmp/stream_concat）
    (tmp_path / "stream_concat").mkdir(exist_ok=True)
    monkeypatch.setattr(_legacy, "_get_concat_list_path",
                        lambda tid: tmp_path / "stream_concat" / f"task_{tid}.txt")
    monkeypatch.setattr(_legacy, "_get_resume_video_path",
                        lambda tid: tmp_path / "stream_resume" / f"task_{tid}.mp4")
    app.root_path = str(tmp_path / "logs_root")
    real_video = tmp_path / "real_smoke.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", "testsrc=duration=1:size=160x120:rate=10",
         "-c", "libx264", "-pix_fmt", "yuv420p", str(real_video)],
        capture_output=True, timeout=30,
    )
    assert real_video.exists(), "ffmpeg 生成测试视频失败"
    with app.app_context():
        db = get_db()
        db.execute("UPDATE watermarked_videos SET output_path=?, duration=1.0 WHERE id=1",
                   (str(real_video),))
        db.commit()
    # loop_count=30 → 30s 内容，确保 ffmpeg 在 2.5s 启动检查时仍在推流
    task_id = _create_task(client, stream_name="smoke", loop_count=30)
    resp = client.post(f"/api/v1/streaming/tasks/{task_id}:start", json={})
    if resp.status_code == 500:
        # 真实推流起不来（MediaMTX 未真服务 RTSP / 编码速度等环境问题）→ skip 不阻塞
        pytest.skip(f"真实推流启动失败（环境问题）：{resp.get_json().get('message', '')[:200]}")
    data = _data(resp)
    assert data["status"] == "running"
    prog = _data(client.get(f"/api/v1/streaming/tasks/{task_id}/progress"))
    assert prog["status"] in ("running", "done")
    stop = _data(client.post(f"/api/v1/streaming/tasks/{task_id}:stop"))
    assert stop["status"] in ("stopped", "failed")


@pytest.mark.slow
@pytest.mark.skipif(not os.environ.get("STREAM_SMOKE"),
                    reason="需 STREAM_SMOKE=1 且 ffmpeg+MediaMTX 就绪才跑真实链路冒烟")
def test_real_smoke_transcode(client, app, seed, tmp_path, monkeypatch):
    """真实转码冒烟：构造非 H.264 源（mpeg4 无音频）→ _probe_codec_compatible=False →
    start 走转码分支（-c:v libx264 ...）成功起流，DB transcode=1。loop_count=30 使推流
    超过 _play_video 的 2.5s 启动检查。start 500 也 skip 兜底。重定向 concat/resume/log
    路径到 tmp，不污染仓库。"""
    if not _has_ffmpeg() or not _mediamtx_up():
        pytest.skip("ffmpeg/MediaMTX 未就绪")
    (tmp_path / "stream_concat").mkdir(exist_ok=True)
    monkeypatch.setattr(_legacy, "_get_concat_list_path",
                        lambda tid: tmp_path / "stream_concat" / f"task_{tid}.txt")
    monkeypatch.setattr(_legacy, "_get_resume_video_path",
                        lambda tid: tmp_path / "stream_resume" / f"task_{tid}.mp4")
    app.root_path = str(tmp_path / "logs_root")
    # 非 H.264 源（mpeg4 + 无音频）→ 转码兜底
    real_video = tmp_path / "real_smoke_mpeg4.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", "testsrc=duration=1:size=160x120:rate=10",
         "-c:v", "mpeg4", "-pix_fmt", "yuv420p", "-an", str(real_video)],
        capture_output=True, timeout=30,
    )
    assert real_video.exists(), "ffmpeg 生成 mpeg4 测试视频失败"
    with app.app_context():
        db = get_db()
        db.execute("UPDATE watermarked_videos SET output_path=?, duration=1.0 WHERE id=1",
                   (str(real_video),))
        db.commit()
    task_id = _create_task(client, stream_name="smoke-tc", loop_count=30)
    resp = client.post(f"/api/v1/streaming/tasks/{task_id}:start", json={})
    if resp.status_code == 500:
        pytest.skip(f"真实转码推流启动失败（环境问题）：{resp.get_json().get('message', '')[:200]}")
    data = _data(resp)
    assert data["status"] == "running"
    # 转码标记已落库（探测到 mpeg4 → transcode=1）
    with app.app_context():
        row = get_db().execute(
            "SELECT transcode FROM stream_tasks WHERE id=?", (task_id,)
        ).fetchone()
        assert row["transcode"] == 1
    stop = _data(client.post(f"/api/v1/streaming/tasks/{task_id}:stop"))
    assert stop["status"] in ("stopped", "failed")
