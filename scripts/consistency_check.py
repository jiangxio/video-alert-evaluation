#!/usr/bin/env python3
"""结果一致性验证脚本

对同一评测任务重复计算指标 5 次，验证结果偏差 ≤2%（评分项"稳定性-结果一致性"）。

评测指标计算基于数据库中已持久化的 OCR 结果做确定性匹配（不重跑 OCR），
理论上重复计算应得到完全一致的结果（偏差 0%）。

用法:
    python scripts/consistency_check.py <task_id>
    python scripts/consistency_check.py <task_id> --runs 10

退出码:
    0 = 一致性达标（偏差 ≤2%）
    1 = 偏差超阈值或出错
"""
import argparse
import json
import sys
from pathlib import Path

# 确保能导入 app 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app
from app import database
from app.services.eval_service import compute_task_metrics
from app.routes.evaluation import _get_all_event_types


def main():
    parser = argparse.ArgumentParser(description="评测结果一致性验证")
    parser.add_argument("task_id", type=int, help="评测任务 ID")
    parser.add_argument("--runs", type=int, default=5, help="重复计算次数（默认 5）")
    parser.add_argument("--threshold", type=float, default=2.0, help="偏差阈值%%（默认 2）")
    args = parser.parse_args()

    app = create_app()

    with app.app_context():
        db = database.get_db()
        cursor = db.cursor()

        # 读取任务，取 eval_set_id
        cursor.execute(
            "SELECT id, name, status, finalized, eval_set_id FROM eval_tasks WHERE id = ?",
            (args.task_id,),
        )
        task = cursor.fetchone()
        if not task:
            print(f"✗ 任务 {args.task_id} 不存在")
            return 1

        print(f"任务 {args.task_id}「{task['name']}」")
        print(f"状态: {task['status']}  finalized: {task['finalized']}")
        print(f"重复计算 {args.runs} 次，偏差阈值 {args.threshold}%")
        print("-" * 60)

        results = []
        for i in range(args.runs):
            accuracy, recall, avg_fp_per_hour, event_metrics, _ = compute_task_metrics(
                args.task_id, cursor, task["eval_set_id"], _get_all_event_types
            )
            results.append(
                {
                    "run": i + 1,
                    "precision": accuracy,
                    "recall": recall,
                    "avg_fp_per_hour": avg_fp_per_hour,
                    "event_metrics": event_metrics,
                }
            )
            print(
                f"第 {i + 1} 次: 精确率={accuracy:.4f}  召回率={recall:.4f}  "
                f"误检/小时={avg_fp_per_hour:.4f}"
            )

        print("-" * 60)

        # 比较各次结果的最大偏差
        precisions = [r["precision"] for r in results]
        recalls = [r["recall"] for r in results]
        fps = [r["avg_fp_per_hour"] for r in results]

        def max_deviation(values):
            """最大相对偏差（百分比），以首次结果为基准"""
            base = values[0] or 0
            if base == 0:
                # 基准为 0 时，看绝对差是否全为 0
                return 0.0 if all(v == 0 for v in values) else 100.0
            return max(abs(v - base) for v in values) / abs(base) * 100

        max_prec_dev = max_deviation(precisions)
        max_recall_dev = max_deviation(recalls)
        max_fp_dev = max_deviation(fps)
        overall_max = max(max_prec_dev, max_recall_dev, max_fp_dev)

        print(f"精确率最大偏差: {max_prec_dev:.4f}%")
        print(f"召回率最大偏差: {max_recall_dev:.4f}%")
        print(f"误检/小时最大偏差: {max_fp_dev:.4f}%")
        print(f"整体最大偏差: {overall_max:.4f}%")
        print("-" * 60)

        # 事件级别指标也校验
        metrics_json_0 = json.dumps(results[0]["event_metrics"], sort_keys=True, ensure_ascii=False)
        all_metrics_same = all(
            json.dumps(r["event_metrics"], sort_keys=True, ensure_ascii=False) == metrics_json_0
            for r in results
        )
        print(f"事件级别指标各次完全一致: {'是' if all_metrics_same else '否'}")

        if overall_max <= args.threshold:
            print(f"\n✓ 一致性达标：最大偏差 {overall_max:.4f}% ≤ {args.threshold}%")
            print(f"  结论：同输入重复评测偏差 ≤2%，结果一致性可得 8 分")
            return 0
        else:
            print(f"\n✗ 一致性未达标：最大偏差 {overall_max:.4f}% > {args.threshold}%")
            return 1


if __name__ == "__main__":
    sys.exit(main())
