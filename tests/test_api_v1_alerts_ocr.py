"""端到端验证 /api/v1/alerts OCR 系列端点。

真实跑 EasyOCR（不 mock）；OCR 识别路径用 PIL 运行时生成的带水印告警图（复用
scripts/process_single.find_font 取系统字体），水印落在 run_ocr 裁剪的左上
380×100 内。慢用例标 @pytest.mark.slow。
"""
import io
import time

import pytest
from werkzeug.datastructures import MultiDict


# 最小有效 PNG（1x1），用于不跑 OCR 的用例（manual / not_found 等）
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c636000000000020001e221bc330000000049454e44ae426082"
)


def _envelope(resp):
    """断言成功信封（code==0）并返回 data。"""
    assert resp.status_code == 200, resp.status_code
    body = resp.get_json()
    assert body["code"] == 0
    return body["data"]


def _make_dataset(client, name="ds"):
    return client.post("/api/v1/alerts/datasets", json={"name": name}).get_json()["data"]["id"]


def _find_font_path():
    """复用 scripts/process_single.find_font()（scripts 非 package，按文件路径 importlib 加载）。"""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "_ps_single_for_test",
        Path(__file__).resolve().parent.parent / "scripts" / "process_single.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.find_font()


def _make_watermarked_png(video_id="0460000001", hhmmss="00:01:30.000"):
    """PIL 生成一张带水印的告警图：640×360 暗底，左上实心黑底 + 32px 白字
    '{video_id} | {hhmmss}'。关键：文字必须落在左上 540×50 内——run_ocr 的
    preprocess_and_ocr 只裁剪该区域（min(540,w)×min(50,h)），超出会被截掉导致
    OCR 乱码。"""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (640, 360), (30, 30, 30))
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(overlay).rectangle([(16, 4), (525, 46)], fill=(0, 0, 0, 255))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)
    font_path = _find_font_path()
    font = ImageFont.truetype(font_path, 32) if font_path else ImageFont.load_default()
    draw.text((24, 10), f"{video_id} | {hhmmss}", fill="white", font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()


def _upload_png(client, dataset_id, png, filename):
    """上传一张 PNG 到数据集，返回 image_id。"""
    resp = client.post(
        f"/api/v1/alerts/datasets/{dataset_id}/images",
        data=MultiDict([("image", (io.BytesIO(png), filename))]),
        content_type="multipart/form-data",
    )
    return _envelope(resp)["uploaded"][0]["id"]


def _upload_watermarked(client, dataset_id, filename="w_103.png"):
    """上传一张水印图，返回 image_id。"""
    return _upload_png(client, dataset_id, _make_watermarked_png(), filename)


@pytest.fixture(autouse=True)
def _reset_ocr_progress(app):
    """OCR 用例隔离：teardown 时取消运行中任务、等后台线程退出（退出即关闭其对
    tmp DB 的 sqlite 连接，解除 Windows 文件锁，让 conftest 的 app teardown 能删库）、
    清空模块级 _ocr_progress，避免跨用例串库/串进度。依赖 app 保证 teardown 在
    app / tmp_path 之前。"""
    yield
    from app.routes import alerts as _legacy

    with _legacy._ocr_lock:
        for _prog in _legacy._ocr_progress.values():
            if _prog.get("running"):
                _prog["cancelled"] = True
    deadline = time.time() + 30
    while time.time() < deadline:
        with _legacy._ocr_lock:
            if not any(p.get("running") for p in _legacy._ocr_progress.values()):
                break
        time.sleep(0.2)
    with _legacy._ocr_lock:
        _legacy._ocr_progress.clear()


# ── 单图 OCR（同步） ───────────────────────────────────────────────────────────

@pytest.mark.slow
def test_ocr_single_real(client):
    """单图 OCR 真跑：水印图 → success=True、video_id 解析正确、ocr_results 落库。"""
    did = _make_dataset(client)
    img_id = _upload_watermarked(client, did)
    data = _envelope(client.post(f"/api/v1/alerts/images/{img_id}/ocr"))
    assert data["success"] is True
    assert data["ocr"]["video_id"] == "0460000001"
    # 落库后 GET 详情应见最新 ocr
    detail = _envelope(client.get(f"/api/v1/alerts/images/{img_id}"))
    assert detail["ocr"] is not None
    assert detail["ocr"]["video_id"] == "0460000001"


def test_ocr_single_not_found(client):
    resp = client.post("/api/v1/alerts/images/999999/ocr")
    assert resp.status_code == 404
    assert resp.get_json()["code"] == 20320


def test_ocr_save_manual(client):
    """手动保存 OCR 结果（不跑 EasyOCR），落库后可读回。"""
    did = _make_dataset(client)
    img_id = _upload_png(client, did, _PNG, "m_103.png")
    data = _envelope(client.post(f"/api/v1/alerts/images/{img_id}/ocr:manual", json={
        "video_id": "1234567890",
        "timestamp": "00:02:15.000",
        "timestamp_seconds": 135.0,
        "success": True,
    }))
    assert data["ocr"]["video_id"] == "1234567890"
    detail = _envelope(client.get(f"/api/v1/alerts/images/{img_id}"))
    assert detail["ocr"]["video_id"] == "1234567890"
    assert detail["ocr"]["timestamp_seconds"] == 135.0


def test_ocr_save_manual_not_found(client):
    resp = client.post("/api/v1/alerts/images/999999/ocr:manual", json={"video_id": "x"})
    assert resp.status_code == 404
    assert resp.get_json()["code"] == 20320


# ── 批量 OCR（后台线程） ───────────────────────────────────────────────────────

@pytest.mark.slow
def test_ocr_batch_real(client):
    """批量 OCR 真跑：1 张水印图，轮询直到 done==total，断言 results。"""
    did = _make_dataset(client)
    _upload_watermarked(client, did)
    resp = client.post(f"/api/v1/alerts/datasets/{did}/ocr:batch", json={})
    assert resp.status_code == 200
    assert resp.get_json()["data"]["total"] == 1

    data = _poll_until_done(client, did, timeout=60)
    assert data["done"] == 1 and data["total"] == 1
    assert len(data["results"]) == 1


@pytest.mark.slow
def test_ocr_batch_conflict(client):
    """同一数据集连发两次 ocr:batch，第二次 → 409 / 30340。2 张图拉长耗时确保第一轮仍在跑。"""
    did = _make_dataset(client)
    _upload_watermarked(client, did, "a_103.png")
    _upload_watermarked(client, did, "b_104.png")
    assert client.post(f"/api/v1/alerts/datasets/{did}/ocr:batch", json={}).status_code == 200
    resp = client.post(f"/api/v1/alerts/datasets/{did}/ocr:batch", json={})
    assert resp.status_code == 409
    assert resp.get_json()["code"] == 30340


def test_ocr_batch_no_images(client):
    """空数据集 → 没有需要 OCR 的图 → 400 / 10311。"""
    did = _make_dataset(client)
    resp = client.post(f"/api/v1/alerts/datasets/{did}/ocr:batch", json={})
    assert resp.status_code == 400
    assert resp.get_json()["code"] == 10311


def test_ocr_status_empty(client):
    """无任务 → 200 空进度 + message（修正旧版 404）。"""
    did = _make_dataset(client)
    resp = client.get(f"/api/v1/alerts/datasets/{did}/ocr-status")
    data = _envelope(resp)
    assert data["total"] == 0
    assert data["running"] is False
    assert data["cancelled"] is False
    assert resp.get_json()["message"]  # 有"没有正在进行的 OCR 任务"提示


@pytest.mark.slow
def test_ocr_cancel(client):
    """起批量后 cancel：cancelled=True、线程退出、第 2 张被跳过（done<total）。"""
    did = _make_dataset(client)
    _upload_watermarked(client, did, "a_103.png")
    _upload_watermarked(client, did, "b_104.png")
    assert client.post(f"/api/v1/alerts/datasets/{did}/ocr:batch", json={}).status_code == 200
    resp = client.post(f"/api/v1/alerts/datasets/{did}/ocr-status:cancel")
    assert resp.status_code == 200
    assert resp.get_json()["data"]["cancelled"] is True

    data = _poll_until_done(client, did, timeout=60)
    assert data["running"] is False
    assert data["cancelled"] is True
    assert data["done"] < data["total"]  # cancel 跳过了后续


# ── helpers ─────────────────────────────────────────────────────────────────────

def _poll_until_done(client, dataset_id, timeout=60):
    """轮询 ocr-status 直到 running==False（线程退出），返回最终进度 data。"""
    deadline = time.time() + timeout
    data = None
    while time.time() < deadline:
        data = client.get(f"/api/v1/alerts/datasets/{dataset_id}/ocr-status").get_json()["data"]
        if not data["running"]:
            break
        time.sleep(0.5)
    assert data is not None, "ocr-status 未返回数据"
    assert not data["running"], f"OCR 线程在 {timeout}s 内未退出"
    return data
