#!/usr/bin/env python3
"""标注结果一致性核对脚本（评分项：标注结果一致性 MAPE -12分）

对比"系统计算的指标"与"基于人工标注真值的标准计算结果"（验收报告基准），
计算加权平均绝对百分比误差（MAPE）。MAPE≤2% → 12 分。

用法:
    python scripts/label_consistency_check.py <task_id>
    python scripts/label_consistency_check.py <task_id> --base 6   # 指定验收基准任务

退出码:
    0 = MAPE≤2%（达标，12分）
    1 = MAPE>2% 或出错
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app
from app import database
from app.services.eval_service import compute_task_metrics
from app.routes.evaluation import _get_all_event_types


# ── 验收报告人工核对基准（《视频流算法验证报告》3.1 节）──────────────────────
# 各事件类型：告警数, 误检数, 漏检数, 精确率, 召回率, 误检/h
# 精确率口径已验证 = (告警-误检)/告警
BASELINE = {
    "call_phone": (235, 35, 12, 0.851, 0.839, 7.16),
    "chef":       (204, 6, 13, 0.971, 0.807, 1.23),
    "flame":      (113, 0, 6, 1.000, 0.860, 0.00),
    "mask":       (440, 1, 9, 0.998, 0.841, 0.20),
    "rat":        (317, 13, 16, 0.959, 0.858, 2.66),
    "smoke":      (242, 8, 35, 0.967, 0.806, 1.64),
    "trash":      (156, 1, 0, 0.994, 1.000, 0.20),
    "use_phone":  (432, 33, 33, 0.924, 0.801, 6.76),
}
# 验收整体：精确率 95.5%，召回率 85.2%，误检/h 2.48（8 类算术平均）
BASELINE_OVERALL = (0.955, 0.852, 2.48)


def mape(system, standard):
    """单指标 MAPE（百分比）。"""
    if standard == 0:
        return 0.0 if system == 0 else 100.0
    return abs(system - standard) / abs(standard) * 100


def main():
    parser = argparse.ArgumentParser(description="标注结果一致性核对（MAPE）")
    parser.add_argument("task_id", type=int, help="评测任务 ID（需对应验收数据集）")
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        db = database.get_db()
        cursor = db.cursor()
        task = cursor.execute(
            "SELECT id, name, status, eval_set_id FROM eval_tasks WHERE id=?",
            (args.task_id,),
        ).fetchone()
        if not task:
            print(f"✗ 任务 {args.task_id} 不存在")
            return 1

        accuracy, recall, avg_fp_hour, event_metrics, _ = compute_task_metrics(
            args.task_id, cursor, task["eval_set_id"], _get_all_event_types
        )

        # 各类型 avg_fp_per_hour 算术平均（验收口径）
        # 仅取验收基准涉及的类型，避免分母类型数不一致误判
        base_types = set(BASELINE.keys())
        fp_vals = [m["avg_fp_per_hour"] for m in event_metrics
                   if m.get("avg_fp_per_hour") is not None and m.get("event_type") in base_types]
        fp_arith_mean = sum(fp_vals) / len(fp_vals) if fp_vals else 0

        print(f"任务 {args.task_id}「{task['name']}」vs 验收报告基准")
        print("=" * 70)

        # ── 整体指标对比 ──
        b_prec, b_rec, b_fp = BASELINE_OVERALL
        sys_prec = accuracy or 0
        sys_rec = recall or 0
        print("【整体指标 MAPE】")
        print(f"  精确率: 系统 {sys_prec:.4f}  验收 {b_prec:.4f}  MAPE {mape(sys_prec, b_prec):.2f}%")
        print(f"  召回率: 系统 {sys_rec:.4f}  验收 {b_rec:.4f}  MAPE {mape(sys_rec, b_rec):.2f}%")
        print(f"  误检/h(算术平均, 仅验收8类): 系统 {fp_arith_mean:.4f}  验收 {b_fp:.4f}  MAPE {mape(fp_arith_mean, b_fp):.2f}%")
        print()

        # ── 逐类对比（仅对比验收报告涉及的类型，避免分母类型数不一致误判）──
        print("【逐类指标 MAPE】")
        print(f"  {'类型':<12} {'精确率MAPE':>10} {'召回率MAPE':>10} {'误检hMAPE':>10}")
        prec_mapes, rec_mapes, fp_mapes = [], [], []
        sys_by_type = {m["event_type"]: m for m in event_metrics}
        missing = []
        for etype, (alerts, fp, miss, b_p, b_r, b_fh) in BASELINE.items():
            sm = sys_by_type.get(etype)
            if not sm:
                missing.append(etype)
                continue
            sp = sm.get("precision", 0) or 0
            sr = sm.get("recall", 0) or 0
            sfh = sm.get("avg_fp_per_hour", 0) or 0
            pm, rm, fm = mape(sp, b_p), mape(sr, b_r), mape(sfh, b_fh)
            prec_mapes.append(pm)
            rec_mapes.append(rm)
            fp_mapes.append(fm)
            print(f"  {etype:<12} {pm:>9.2f}% {rm:>9.2f}% {fm:>9.2f}%")

        if missing:
            print(f"  （验收基准中 {missing} 在该任务无数据，已排除）")

        # 加权 MAPE（仅对有数据的类型等权平均）
        n = len(prec_mapes)
        if n == 0:
            print("✗ 该任务无任何验收基准类型数据，无法核对")
            return 1
        w_prec = sum(prec_mapes) / n
        w_rec = sum(rec_mapes) / n
        w_fp = sum(fp_mapes) / n
        overall_mape = (w_prec + w_rec + w_fp) / 3
        print()
        print("【加权 MAPE 汇总（各类型等权平均）】")
        print(f"  精确率加权 MAPE: {w_prec:.2f}%")
        print(f"  召回率加权 MAPE: {w_rec:.2f}%")
        print(f"  误检/h 加权 MAPE: {w_fp:.2f}%")
        print(f"  三指标平均 MAPE: {overall_mape:.2f}%")
        print("=" * 70)

        if overall_mape <= 2.0:
            print(f"✓ 达标：整体 MAPE {overall_mape:.2f}% ≤ 2%，标注一致性可得 12 分")
            return 0
        else:
            print(f"✗ 未达标：整体 MAPE {overall_mape:.2f}% > 2%，需核对口径")
            print(f"  提示：误检/h 若用算术平均口径，整体 MAPE 会显著降低")
            return 1


if __name__ == "__main__":
    sys.exit(main())
