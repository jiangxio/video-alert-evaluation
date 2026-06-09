"""Tests for app.services.eval_service."""
import sqlite3

import pytest

from app.services.eval_service import (
    compute_overall_avg_fp,
    compute_task_metrics,
    get_effective_status,
)


class TestGetEffectiveStatus:
    """测试 manual_status 优先级高于 is_false_positive."""

    @pytest.mark.parametrize(
        "manual,is_fp,expected",
        [
            ("correct", 1, "correct"),
            ("correct", 0, "correct"),
            ("false_positive", 0, "false_positive"),
            ("false_positive", 1, "false_positive"),
            ("ignored", 0, "ignored"),
            ("ignored", 1, "ignored"),
            ("auto", 0, "correct"),
            ("auto", 1, "false_positive"),
            (None, 0, "correct"),
            (None, 1, "false_positive"),
            ("", 0, "correct"),
            ("", 1, "false_positive"),
        ],
    )
    def test_priority(self, manual, is_fp, expected):
        row = {"manual_status": manual, "is_false_positive": is_fp}
        assert get_effective_status(row) == expected


class TestComputeOverallAvgFp:
    def test_empty(self):
        assert compute_overall_avg_fp([]) == 0

    def test_normal(self):
        event_metrics = [
            {"avg_fp_per_hour": 2.0},
            {"avg_fp_per_hour": 4.0},
        ]
        assert compute_overall_avg_fp(event_metrics) == 3.0

    def test_skips_none(self):
        event_metrics = [
            {"avg_fp_per_hour": 2.0},
            {"avg_fp_per_hour": None},
            {"avg_fp_per_hour": 4.0},
        ]
        assert compute_overall_avg_fp(event_metrics) == 3.0

    def test_missing_key_defaults_to_zero(self):
        # em.get('avg_fp_per_hour', 0) 会把缺失键当作 0，但 is not None 会过滤掉它
        event_metrics = [
            {"avg_fp_per_hour": 2.0},
            {"event_type": "x"},  # 无 avg_fp_per_hour 键
        ]
        assert compute_overall_avg_fp(event_metrics) == 2.0

    def test_single_value(self):
        assert compute_overall_avg_fp([{"avg_fp_per_hour": 5.5}]) == 5.5


class TestComputeTaskMetrics:
    """测试 compute_task_metrics 的核心指标计算逻辑."""

    def _setup_minimal(self, conn):
        """插入一个最小任务所需的基础数据."""
        cur = conn.cursor()
        cur.execute("INSERT INTO eval_video_sets (id, video_ids) VALUES (1, '[1]')")
        cur.execute("INSERT INTO videos (id, duration, video_id) VALUES (1, 3600, 'v001')")
        cur.execute(
            "INSERT INTO eval_tasks (id, eval_set_id, dataset_id, status) "
            "VALUES (1, 1, 1, 'done')"
        )
        conn.commit()
        return cur

    def _insert_merged(self, cur, task_id, rows):
        """rows: list of (event_type, is_false_positive, manual_status)"""
        for etype, is_fp, manual in rows:
            cur.execute(
                "INSERT INTO eval_merged_events (task_id, event_type, is_false_positive, manual_status) "
                "VALUES (?, ?, ?, ?)",
                (task_id, etype, is_fp, manual),
            )

    def _insert_gt(self, cur, task_id, rows):
        """rows: list of (event_type, confirmed_count, actual_count)"""
        for etype, confirmed, actual in rows:
            cur.execute(
                "INSERT INTO eval_gt_events (task_id, event_type, confirmed_count, actual_count) "
                "VALUES (?, ?, ?, ?)",
                (task_id, etype, confirmed, actual),
            )

    def test_basic_hit(self, db_conn):
        """基本场景：命中 GT，计算 accuracy/recall/avg_fp."""
        cur = self._setup_minimal(db_conn)
        # 2 个命中，1 个误检
        self._insert_merged(cur, 1, [
            ("smoke", 0, "auto"),
            ("smoke", 0, "auto"),
            ("smoke", 1, "auto"),
        ])
        # GT 期望 2 次，实际命中 2 次
        self._insert_gt(cur, 1, [("smoke", 2, 2)])
        db_conn.commit()

        accuracy, recall, avg_fp, event_metrics, duration = compute_task_metrics(
            1, db_conn.cursor(), 1
        )

        assert accuracy == pytest.approx(2 / 3)
        assert recall == pytest.approx(1.0)
        assert avg_fp == pytest.approx(1.0)  # 1 FP / 1 hour
        assert duration == 3600

        em = event_metrics[0]
        assert em["event_type"] == "smoke"
        assert em["alert_count"] == 3
        assert em["correct_pred_count"] == 2
        assert em["false_positive_count"] == 1
        assert em["gt_count"] == 2
        assert em["hit_count"] == 2
        assert em["missed_gt_count"] == 0
        assert em["precision"] == pytest.approx(2 / 3)
        assert em["recall"] == pytest.approx(1.0)
        assert em["avg_fp_per_hour"] == pytest.approx(1.0)

    def test_confirmed_zero_actual_positive(self, db_conn):
        """confirmed_count=0 语义：不主动预期，但如果触发了按 1 次算."""
        cur = self._setup_minimal(db_conn)
        self._insert_merged(cur, 1, [("rat", 0, "auto")])
        self._insert_gt(cur, 1, [("rat", 0, 1)])
        db_conn.commit()

        accuracy, recall, avg_fp, event_metrics, _ = compute_task_metrics(
            1, db_conn.cursor(), 1
        )

        assert accuracy == pytest.approx(1.0)
        assert recall == pytest.approx(1.0)
        em = event_metrics[0]
        assert em["gt_count"] == 1
        assert em["hit_count"] == 1

    def test_confirmed_zero_actual_zero(self, db_conn):
        """confirmed_count=0 且 actual=0：该类型不计入 recall."""
        cur = self._setup_minimal(db_conn)
        self._insert_gt(cur, 1, [("rat", 0, 0)])
        db_conn.commit()

        accuracy, recall, avg_fp, event_metrics, _ = compute_task_metrics(
            1, db_conn.cursor(), 1
        )

        assert accuracy is None  # 无告警
        assert recall is None    # gt_count=0 的类型不计入
        em = event_metrics[0]
        assert em["gt_count"] == 0
        assert em["recall"] is None

    def test_recall_cap(self, db_conn):
        """命中次数不能超过 confirmed_count：hit_count = min(actual, confirmed)."""
        cur = self._setup_minimal(db_conn)
        # 5 次告警都命中了同一个 GT
        self._insert_merged(cur, 1, [("smoke", 0, "auto")] * 5)
        # 但 GT 只预期 2 次
        self._insert_gt(cur, 1, [("smoke", 2, 5)])
        db_conn.commit()

        accuracy, recall, avg_fp, event_metrics, _ = compute_task_metrics(
            1, db_conn.cursor(), 1
        )

        assert accuracy == pytest.approx(1.0)  # 5/5 正确
        assert recall == pytest.approx(1.0)    # min(5,2)/2 = 1.0
        em = event_metrics[0]
        assert em["hit_count"] == 2
        assert em["gt_count"] == 2

    def test_overall_recall_is_arithmetic_mean(self, db_conn):
        """整体召回率是算术平均，不是加权平均."""
        cur = self._setup_minimal(db_conn)
        # smoke: 1 个 GT 命中，recall=1.0
        self._insert_merged(cur, 1, [("smoke", 0, "auto")])
        self._insert_gt(cur, 1, [("smoke", 1, 1)])
        # flame: 10 个 GT 命中 5 个，recall=0.5
        self._insert_merged(cur, 1, [("flame", 0, "auto")] * 5)
        self._insert_gt(cur, 1, [("flame", 10, 5)])
        db_conn.commit()

        _, recall, _, _, _ = compute_task_metrics(1, db_conn.cursor(), 1)

        # 算术平均：(1.0 + 0.5) / 2 = 0.75
        # 加权平均会是 (1 + 5) / (1 + 10) ≈ 0.545
        assert recall == pytest.approx(0.75)

    def test_manual_status_overrides(self, db_conn):
        """manual_status 优先级高于 is_false_positive."""
        cur = self._setup_minimal(db_conn)
        # is_false_positive=1 但 manual_status='correct' → 计为命中
        self._insert_merged(cur, 1, [
            ("smoke", 1, "correct"),
            ("smoke", 0, "false_positive"),
            ("smoke", 0, "ignored"),
        ])
        self._insert_gt(cur, 1, [("smoke", 2, 2)])
        db_conn.commit()

        accuracy, _, _, event_metrics, _ = compute_task_metrics(
            1, db_conn.cursor(), 1
        )

        # 有效告警：correct(1) + false_positive(1) = 2；ignored 不计入
        # correct_pred_count = 1
        assert accuracy == pytest.approx(1 / 2)
        em = event_metrics[0]
        assert em["alert_count"] == 2  # 排除 ignored
        assert em["correct_pred_count"] == 1
        assert em["false_positive_count"] == 1

    def test_empty_task(self, db_conn):
        """空任务：无告警无 GT，指标应为 None 或 0."""
        self._setup_minimal(db_conn)
        # 不插入任何告警和 GT

        accuracy, recall, avg_fp, event_metrics, duration = compute_task_metrics(
            1, db_conn.cursor(), 1
        )

        assert accuracy is None
        assert recall is None
        assert avg_fp == 0
        assert event_metrics == []
        assert duration == 3600

    def test_event_type_without_alerts(self, db_conn):
        """某事件类型有 GT 但无告警：recall=0."""
        cur = self._setup_minimal(db_conn)
        self._insert_gt(cur, 1, [("mask", 3, 0)])
        db_conn.commit()

        _, recall, _, event_metrics, _ = compute_task_metrics(
            1, db_conn.cursor(), 1
        )

        em = event_metrics[0]
        assert em["event_type"] == "mask"
        assert em["alert_count"] == 0
        assert em["gt_count"] == 3
        assert em["hit_count"] == 0
        assert em["recall"] == pytest.approx(0.0)
        assert recall == pytest.approx(0.0)

    def test_fp_only_event_type(self, db_conn):
        """某事件类型只有误检无 GT：precision=0，但不影响其他类型 recall."""
        cur = self._setup_minimal(db_conn)
        self._insert_merged(cur, 1, [("rat", 1, "auto")])
        self._insert_gt(cur, 1, [("smoke", 1, 1)])  # 另一个类型有 GT
        db_conn.commit()

        accuracy, recall, avg_fp, event_metrics, _ = compute_task_metrics(
            1, db_conn.cursor(), 1
        )

        by_type = {em["event_type"]: em for em in event_metrics}
        assert by_type["rat"]["precision"] == pytest.approx(0.0)
        assert by_type["smoke"]["recall"] == pytest.approx(1.0)
        # 整体 recall 只看有 GT 的类型
        assert recall == pytest.approx(1.0)
        # 整体 accuracy：smoke 0 correct / (rat 1 + smoke 0) = 0
        assert accuracy == pytest.approx(0.0)

    def test_overall_avg_fp_with_zero_duration(self, db_conn):
        """视频时长为 0 时，avg_fp 应为 0 避免除零."""
        cur = db_conn.cursor()
        cur.execute("INSERT INTO eval_video_sets (id, video_ids) VALUES (1, '[1]')")
        cur.execute("INSERT INTO videos (id, duration, video_id) VALUES (1, 0, 'v001')")
        cur.execute(
            "INSERT INTO eval_tasks (id, eval_set_id, dataset_id, status) "
            "VALUES (1, 1, 1, 'done')"
        )
        self._insert_merged(cur, 1, [("smoke", 1, "auto")])
        db_conn.commit()

        _, _, avg_fp, event_metrics, _ = compute_task_metrics(
            1, db_conn.cursor(), 1
        )

        assert avg_fp == 0
        assert event_metrics[0]["avg_fp_per_hour"] == 0
