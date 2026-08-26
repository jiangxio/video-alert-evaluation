"""端到端验证 /api/v1/extract 端点（抽帧任务 start/status 委托 + download/delete/list 重写）。

测试分层（对齐 plan §测试策略）：
- 委托端点（start/status）：_stub_worker（autouse）把 _do_extract_batch 替成 no-op，
  避开真 ffmpeg/ffprobe/仓库写/Windows 文件锁，只测信封+状态码+错误码+委托真触发
  （task 落库、模块态 _extract_tasks 更新）。worker 内部是旧生产代码职责，不重测。
- 原位重写端点（download/delete/list）：seed 行直测；download/delete 的 output_dir
  用 tmp 目录造帧文件测删除/打包。

DB 为 conftest tmp 库（create_app→init_db 建全 schema 含 extracted_frames_tasks）。
conftest 已 patch app.routes.extract.DATABASE_PATH→tmp（双绑定）+ EXTRACTED_FRAMES_DIR→tmp。

盲区（bug-audit 另修）：worker 端到端（抽帧/ffprobe/缩放）未覆盖；_fail_task 死代码+
失败仍标 done；float(interval_sec) 传字符串崩 500（v1 不修只标，测试传数字）。
"""
import io
import json
import zipfile

import pytest

from app.database import get_db
from app.routes import extract as _legacy


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
    """autouse：把真 worker _do_extract_batch 替成 no-op，避开真 ffmpeg/ffprobe/仓库写/
    Windows tmp 库文件锁。autouse 防漏标导致 start 成功用例起真 worker。"""
    monkeypatch.setattr(_legacy, "_do_extract_batch", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _reset_extract_state():
    """autouse：每用例后清模块级 _extract_tasks，防跨用例串（stub 不开 sqlite 连接故无需等线程）。"""
    yield
    _legacy._extract_tasks.clear()


@pytest.fixture
def seed(app, tmp_path):
    """videos(id=1, video_id=046-001) + watermarked_videos(id=1, output_path 指向 tmp 真文件)。
    start 的 os.path.exists(output_path) 校验通过。"""
    wm_file = tmp_path / "wm1.mp4"
    wm_file.write_bytes(b"fake mp4")
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
    return {"wm_id": 1, "video_id": "046-001", "wm_file": str(wm_file)}


def _insert_task(app, **fields):
    """直接插一条 extracted_frames_tasks 行。动态拼列。返回 id。"""
    cols = {
        "wm_ids": "[1]", "video_id": "046-001", "video_count": 1,
        "target_width": None, "interval_sec": 1.0, "include_normal": 0,
        "status": "done", "frame_count": 5, "output_dir": "/tmp/none",
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
            f"INSERT INTO extracted_frames_tasks ({col_list}) VALUES ({placeholders})",
            values,
        )
        db.commit()
        return cur.lastrowid


# ── 1. list（原位重写·真分页）──────────────────────────────────────────────────

def test_list_empty(client):
    data = _data(client.get("/api/v1/extract/tasks"))
    assert data["total"] == 0 and data["items"] == []


def test_list_with_data(app, client, seed):
    _insert_task(app, status="running")
    data = _data(client.get("/api/v1/extract/tasks"))
    assert data["total"] == 1
    assert data["items"][0]["video_id"] == "046-001"
    assert data["items"][0]["status"] == "running"


def test_list_pagination(app, client, seed):
    for i in range(25):
        _insert_task(app, video_id=f"046-{i:03d}")
    data = _data(client.get("/api/v1/extract/tasks?page_size=20"))
    assert data["total"] == 25
    assert len(data["items"]) == 20
    assert data["has_next"] is True
    data2 = _data(client.get("/api/v1/extract/tasks?page=2&page_size=20"))
    assert len(data2["items"]) == 5
    assert data2["has_next"] is False


# ── 2. start（委托）────────────────────────────────────────────────────────────

def test_start_success(app, client, seed):
    """有效 wm_ids + 文件存在 + stub worker → 200 + task_id + video_count，task 行落库。"""
    resp = client.post("/api/v1/extract/tasks", json={"wm_ids": [1], "interval_sec": 2})
    data = _data(resp)
    assert data["task_id"] >= 1
    assert data["video_count"] == 1
    # 委托真触发：task 行落 tmp 库 + 模块态更新
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT status FROM extracted_frames_tasks WHERE id = ?", (data["task_id"],))
        row = cur.fetchone()
        assert row is not None and row["status"] == "running"
    assert data["task_id"] in _legacy._extract_tasks


def test_start_no_wm_ids(client, seed):
    _err(client.post("/api/v1/extract/tasks", json={}), 400, 11100)


def test_start_bad_interval(client, seed):
    # 旧代码 `float(data.get('interval_sec') or 1.0)`：0 是 falsy→被当默认 1.0，不触发；
    # 须传负数（truthy 且 ≤0）才命中「抽帧间隔必须大于0」。
    _err(client.post("/api/v1/extract/tasks", json={"wm_ids": [1], "interval_sec": -1}), 400, 11101)


def test_start_wm_not_found(client, seed):
    _err(client.post("/api/v1/extract/tasks", json={"wm_ids": [999]}), 404, 21100)


def test_start_all_invalid(app, client, tmp_path):
    """wm 存在但 output_path 文件不存在 → valid=[] → 11102。"""
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        cur.execute(
            "INSERT INTO videos (id, filename, original_path, video_id, duration) "
            "VALUES (2, 'v2.mp4', 'orig/v2.mp4', '046-002', 10.0)"
        )
        cur.execute(
            "INSERT INTO watermarked_videos (id, original_video_id, filename, output_path, duration) "
            "VALUES (2, 2, 'v2_wm.mp4', '/nonexistent/path.mp4', 10.0)"
        )
        db.commit()
    _err(client.post("/api/v1/extract/tasks", json={"wm_ids": [2], "interval_sec": 2}), 400, 11102)


# ── 3. status（委托）──────────────────────────────────────────────────────────

def test_status_memory_path(app, client, seed):
    """模块态有任务 → 旧视图走内存路径返 200。"""
    _legacy._extract_tasks[42] = {
        "video_id": "046-001", "video_count": 1, "total": 1, "done": 1,
        "frame_count": 5, "status": "done", "output_dir": "/tmp/x", "error": None,
    }
    data = _data(client.get("/api/v1/extract/tasks/42/status"))
    assert data["status"] == "done"
    assert data["frame_count"] == 5
    assert data["done"] == 1 and data["total"] == 1


def test_status_db_fallback(app, client, seed):
    """模块态 miss → 旧视图回退 DB → 200。"""
    tid = _insert_task(app, status="done", frame_count=3, video_count=1)
    data = _data(client.get(f"/api/v1/extract/tasks/{tid}/status"))
    assert data["status"] == "done"
    assert data["frame_count"] == 3


def test_status_not_found(client, seed):
    _err(client.get("/api/v1/extract/tasks/999/status"), 404, 21101)


# ── 4. download（原位重写·二进制）────────────────────────────────────────────

def test_download_not_found(client, seed):
    _err(client.get("/api/v1/extract/tasks/999/download"), 404, 21101)


def test_download_dir_missing(app, client, seed):
    tid = _insert_task(app, output_dir="/nonexistent/dir")
    _err(client.get(f"/api/v1/extract/tasks/{tid}/download"), 404, 21102)


def test_download_success(app, client, seed, tmp_path):
    """有帧目录 → 返回 zip 含帧文件。"""
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    (frames_dir / "frame_001.jpg").write_bytes(b"img1")
    (frames_dir / "frame_002.jpg").write_bytes(b"img2")
    tid = _insert_task(app, output_dir=str(frames_dir), video_id="046-001")
    resp = client.get(f"/api/v1/extract/tasks/{tid}/download")
    assert resp.status_code == 200
    assert "application/zip" in resp.mimetype
    zf = zipfile.ZipFile(io.BytesIO(resp.data))
    names = zf.namelist()
    assert "frame_001.jpg" in names and "frame_002.jpg" in names


# ── 5. delete（原位重写）──────────────────────────────────────────────────────

def test_delete_not_found(client, seed):
    _err(client.delete("/api/v1/extract/tasks/999"), 404, 21101)


def test_delete_success(app, client, seed, tmp_path):
    """删行 + rmtree 帧目录 → 204。"""
    frames_dir = tmp_path / "to_delete"
    frames_dir.mkdir()
    (frames_dir / "f.jpg").write_bytes(b"x")
    tid = _insert_task(app, output_dir=str(frames_dir))
    resp = client.delete(f"/api/v1/extract/tasks/{tid}")
    assert resp.status_code == 204
    with app.app_context():
        db = get_db()
        cur = db.cursor()
        cur.execute("SELECT id FROM extracted_frames_tasks WHERE id = ?", (tid,))
        assert cur.fetchone() is None
    assert not frames_dir.exists()
