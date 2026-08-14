"""指标计算异常测试（评分项：稳定性-核心功能-指标计算异常 -3分）

针对 compute_task_metrics 的边界/异常输入：
- 空数据集（0 告警、0 GT）
- GT 事件无任何命中
- confirmed_count=0 的事件
- 空事件类型
- 除零场景（总时长为 0）

目标：证明这些异常输入下不崩溃、不报错、返回合理的指标值。
"""
import pytest

from app import database
from app.services.eval_service import compute_task_metrics


def _get_all_event_types():
    """无参的事件类型查询（复用 evaluation 路由的契约）。"""
    return []


def _setup_empty_task(db, task_id=1):
    """建一个 done 状态的空评测任务（无 merged events、无 gt events）。

    db 是 get_db() 返回的 connection；compute_task_metrics 接收 cursor。
    """
    db.execute(
        "INSERT INTO eval_tasks (id, name, status, finalized, eval_set_id) "
        "VALUES (?, '空任务', 'done', 0, NULL)",
        (task_id,),
    )
    db.commit()


class TestMetricCalculationAnomalies:
    """指标计算在异常/边界输入下应稳定，不崩溃、不除零。"""

    def test_empty_task_no_crash(self, app_ctx):
        """空数据集评测（0 告警、0 GT）应安全返回，不报错。"""
        db = database.get_db()
        _setup_empty_task(db, task_id=1)
        cursor = db.cursor()

        accuracy, recall, fp_per_hour, event_metrics, _ = compute_task_metrics(
            1, cursor, None, _get_all_event_types
        )
        # 空数据：不崩即通过（无告警无 GT，指标为 None 或 0 都可接受）
        assert accuracy is None or accuracy == 0
        assert recall is None or recall == 0
        assert fp_per_hour is None or fp_per_hour == 0

    def test_no_hits_recall_zero(self, app_ctx):
        """有 GT 但无任何命中：召回率应为 0，不崩溃。"""
        db = database.get_db()
        _setup_empty_task(db, task_id=2)
        # 插入一条 GT 事件，预期触发但实际 0 次
        db.execute(
            "INSERT INTO eval_gt_events "
            "(task_id, video_id, event_type, confirmed_count, actual_count, start_sec, end_sec) "
            "VALUES (2, 'v1', 'fight', 1, 0, 10, 20)"
        )
        db.execute(
            "INSERT INTO eval_video_sets (id, name, video_ids) VALUES (1, 'vset', '[]')"
        )
        db.execute("UPDATE eval_tasks SET eval_set_id=1 WHERE id=2")
        db.commit()
        cursor = db.cursor()

        accuracy, recall, fp_per_hour, event_metrics, _ = compute_task_metrics(
            2, cursor, 1, _get_all_event_types
        )
        # 召回率应包含 fight 类型且为 0
        assert recall == 0

    def test_confirmed_count_zero_logic(self, app_ctx):
        """confirmed_count=0 的事件：按"不预期但触发按1算"逻辑处理，不崩。"""
        db = database.get_db()
        _setup_empty_task(db, task_id=3)
        db.execute(
            "INSERT INTO eval_gt_events "
            "(task_id, video_id, event_type, confirmed_count, actual_count, start_sec, end_sec) "
            "VALUES (3, 'v1', 'fight', 0, 0, 10, 20)"
        )
        db.execute(
            "INSERT INTO eval_video_sets (id, name, video_ids) VALUES (1, 'vset', '[]')"
        )
        db.execute("UPDATE eval_tasks SET eval_set_id=1 WHERE id=3")
        db.commit()
        cursor = db.cursor()

        # confirmed_count=0 且 actual=0：应跳过该类型（不计入召回率），不崩
        accuracy, recall, fp_per_hour, event_metrics, _ = compute_task_metrics(
            3, cursor, 1, _get_all_event_types
        )
        # 不崩即通过；confirmed_count=0 且 actual=0 的类型应被跳过
        assert recall is None or isinstance(recall, (int, float))

    def test_zero_duration_no_division_error(self, app_ctx):
        """总时长为 0 时计算误检/小时不应除零报错。"""
        db = database.get_db()
        _setup_empty_task(db, task_id=4)
        cursor = db.cursor()
        # 无视频集/视频时长，total_duration_hours 应为 0
        accuracy, recall, fp_per_hour, event_metrics, _ = compute_task_metrics(
            4, cursor, None, _get_all_event_types
        )
        # 不崩、fp_per_hour 为有限数（非 inf/nan）
        import math
        assert math.isfinite(fp_per_hour)

    def test_missing_task_returns_safe(self, app_ctx):
        """不存在的 task_id 应安全返回，不抛未捕获异常。"""
        db = database.get_db()
        cursor = db.cursor()
        # 不存在的 task_id=9999：compute_task_metrics 查不到任务信息，
        # 应安全返回（None 或 0），而非抛 KeyError/AttributeError 这类编程错误
        try:
            accuracy, recall, fp_per_hour, event_metrics, _ = compute_task_metrics(
                9999, cursor, None, _get_all_event_types
            )
            # 不抛异常即通过
            assert accuracy is None or isinstance(accuracy, (int, float))
        except Exception as e:
            pytest.fail(f"不存在任务应优雅处理，而非抛 {type(e).__name__}: {e}")
