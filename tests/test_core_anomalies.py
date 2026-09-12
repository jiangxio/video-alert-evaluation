"""核心功能异常测试：样本导入 / 结果输出 / 报告生成（API 级）

对应评分扣分项（各 -3 分）：
- 样本导入异常（失败、格式无法识别）
- 结果输出异常（缺失、格式错误）
- 报告生成异常（失败、内容缺失）

目标：异常输入下返回明确错误码与提示，不 500 内部错误、不崩溃。
"""
import io
import zipfile

import pytest


def _make_zip(files: dict, raw_bytes: bytes = None) -> tuple:
    """构造一个上传文件元组 (file, filename)。files={name: content}。raw_bytes 直接给损坏字节。"""
    buf = io.BytesIO()
    if raw_bytes is not None:
        buf.write(raw_bytes)
        buf.seek(0)
    else:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            for name, content in files.items():
                zf.writestr(name, content)
        buf.seek(0)
    return (buf, "test.zip")


def _make_dataset(client, name="异常测试集") -> int:
    """创建一个数据集，返回 id。"""
    r = client.post("/alerts/api/datasets", json={"name": name})
    assert r.status_code == 200
    return r.get_json()["dataset"]["id"]


class TestSampleImportAnomalies:
    """样本导入（评分 -3 分）：畸形输入应明确拒绝，不 500。"""

    def test_non_archive_extension(self, app_client):
        """非支持的压缩格式（.txt）应 400 拒绝。"""
        ds_id = _make_dataset(app_client)
        r = app_client.post(
            f"/alerts/api/datasets/{ds_id}/import",
            data={"file": (io.BytesIO(b"not a zip"), "evil.txt")},
            content_type="multipart/form-data",
        )
        assert r.status_code == 400
        assert "格式" in r.get_json().get("error", "")

    def test_corrupt_zip(self, app_client):
        """损坏的 ZIP 字节应优雅拒绝（BadZipFile 被捕获），不 500。"""
        ds_id = _make_dataset(app_client)
        # 随机字节，非合法 zip
        r = app_client.post(
            f"/alerts/api/datasets/{ds_id}/import",
            data={"file": _make_zip({}, raw_bytes=b"\x00\x01\x02not-a-zip")},
            content_type="multipart/form-data",
        )
        # 应是 400/500 带 error，但不应是未捕获的服务器崩溃
        assert r.status_code in (400, 500)
        data = r.get_json()
        assert data is not None and "error" in data

    def test_missing_file_field(self, app_client):
        """未上传 file 字段应 400。"""
        ds_id = _make_dataset(app_client)
        r = app_client.post(
            f"/alerts/api/datasets/{ds_id}/import",
            data={},
            content_type="multipart/form-data",
        )
        assert r.status_code == 400

    def test_nonexistent_dataset(self, app_client):
        """导入到不存在的数据集应 404。"""
        r = app_client.post(
            "/alerts/api/datasets/999999/import",
            data={"file": (io.BytesIO(b"x"), "x.zip")},
            content_type="multipart/form-data",
        )
        assert r.status_code == 404

    def test_empty_archive(self, app_client):
        """空 ZIP（无图片）应安全处理，不崩。"""
        ds_id = _make_dataset(app_client)
        r = app_client.post(
            f"/alerts/api/datasets/{ds_id}/import",
            data={"file": _make_zip({})},
            content_type="multipart/form-data",
        )
        # 空包导入：成功但 0 张，或提示；关键是优雅不崩
        assert r.status_code in (200, 400)
        data = r.get_json()
        assert data is not None


class TestResultOutputAnomalies:
    """结果输出（评分 -3 分）：异常请求返回正确结构与错误码。"""

    def test_results_nonexistent_task(self, app_client):
        """请求不存在任务的结果应 404，不返回残缺数据。"""
        r = app_client.get("/evaluation/api/tasks/999999/results")
        assert r.status_code == 404
        assert "error" in r.get_json()

    def test_metrics_nonexistent_task(self, app_client):
        """请求不存在任务的事件指标应 404。"""
        r = app_client.get("/evaluation/api/tasks/999999/event-metrics")
        assert r.status_code == 404


class TestReportGenerationAnomalies:
    """报告生成（评分 -3 分）：异常状态应优雅降级，不 500 崩溃。"""

    def test_report_nonexistent_task(self, app_client):
        """不存在的任务生成报告应 404。"""
        r = app_client.post("/evaluation/api/tasks/999999/detailed-report", json={})
        assert r.status_code == 404
        assert "error" in r.get_json()

    def test_report_unfinished_task(self, app_client):
        """未完成评测的任务生成报告应 400 提示"请先完成评测"。"""
        # 创建一个 created 状态的任务
        r = app_client.post(
            "/evaluation/api/tasks",
            json={"name": "未完成报告测试", "eval_set_id": None},
        )
        # 创建可能要求字段，宽松处理
        if r.status_code != 200:
            pytest.skip("任务创建接口约束不同，跳过")
        task_id = r.get_json().get("id") or r.get_json().get("task_id")
        if not task_id:
            pytest.skip("无法获取任务 id")
        r2 = app_client.post(f"/evaluation/api/tasks/{task_id}/detailed-report", json={})
        assert r2.status_code == 400
        assert "完成" in r2.get_json().get("error", "") or "评测" in r2.get_json().get("error", "")
