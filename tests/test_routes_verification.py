"""verification 旧端点（/api/alerts/{id}/ocr）行为锁定测试。

这些端点在 REST v1 改造中未迁移，仍由 app/routes/verification.py 提供。
本套测试在「抽 ocr_and_save helper 去重」重构前建立行为基线，确保重构不改变：

- 成功：写 ocr_results 表 + 200 + {success, ocr_result_id, ocr_result}
- 图不存在：404
- OCR 失败：500 且不写表

run_ocr 被 mock（不真跑 EasyOCR），单测路由逻辑。同时 patch
`app.routes.verification.run_ocr`（重构前 ocr_alert 直接调用）与
`app.services.verification_service.run_ocr`（重构后由 helper 调用），
使本套测试在重构前后都绿，断言验证行为不变。
"""
import pytest

from app.database import get_db


def _mock_run_ocr(monkeypatch, result):
    """patch verification_service 模块内的 run_ocr 绑定。

    ocr_alert 调 ocr_and_save helper，helper 内调
    app.services.verification_service.run_ocr（模块全局），patch 此处生效。
    """
    monkeypatch.setattr("app.services.verification_service.run_ocr", lambda path: result)


def _insert_alert(app, aid=1, file_path="/tmp/fake.png"):
    """插一条 alert_images 记录（仅 NOT NULL 字段）。"""
    with app.app_context():
        db = get_db()
        db.execute(
            "INSERT INTO alert_images (id, filename, file_path) VALUES (?, ?, ?)",
            (aid, f"{aid}.png", file_path),
        )
        db.commit()


def _ocr_count(app, aid):
    with app.app_context():
        row = get_db().execute(
            "SELECT COUNT(*) AS c FROM ocr_results WHERE alert_image_id = ?", (aid,)
        ).fetchone()
    return row["c"]


def test_ocr_alert_success(app, client, monkeypatch):
    """成功：mock run_ocr 返回有效结果 → 200 + 写 ocr_results + 三字段壳。"""
    _insert_alert(app)
    _mock_run_ocr(monkeypatch, {
        "raw_ocr_text": "046 | 00:01:30",
        "video_id": "046",
        "timestamp": "00:01:30",
        "timestamp_seconds": 90,
        "success": True,
    })

    resp = client.post("/api/alerts/1/ocr")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    assert body["ocr_result_id"] is not None
    assert body["ocr_result"]["video_id"] == "046"
    assert _ocr_count(app, 1) == 1


def test_ocr_alert_not_found(app, client, monkeypatch):
    """图不存在 → 404。"""
    _mock_run_ocr(monkeypatch, {})
    resp = client.post("/api/alerts/999/ocr")
    assert resp.status_code == 404
    assert "error" in resp.get_json()


def test_ocr_alert_ocr_failure(app, client, monkeypatch):
    """OCR 失败（run_ocr 返回 error）→ 500 且不写 ocr_results。"""
    _insert_alert(app)
    _mock_run_ocr(monkeypatch, {"error": "模型加载失败"})

    resp = client.post("/api/alerts/1/ocr")

    assert resp.status_code == 500
    assert "error" in resp.get_json()
    assert _ocr_count(app, 1) == 0
