
"""
对比"上次漏报" vs "本次 60s 重测"，生成 {name}_retest_report.md + {name}_retest_summary.csv。

输入：
  - 上次漏报清单 {name}_missed_videos.csv（来自 compute_recall.py）
  - 上次全量汇总 {name}_recall_summary.csv（动态计算上次全量 total/hit/miss/recall）
  - 本次重测统计 alarm_report_{name}.csv（repeat 模式 5s×12=60s）
输出（写到脚本所在目录，即项目根 alert\\alert\\）：
  - {name}_retest_report.md
  - {name}_retest_summary.csv

命令行参数：
  --videos-name NAME   数据集名称（默认: fight）
"""
import argparse
import csv
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def parse_args():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--videos-name", default="fight",
                    help="数据集名称 (默认: fight)")
    return ap.parse_args()


def load_baseline(name):
    """从 {name}_recall_summary.csv 读上次全量的 (total, hit, miss, recall)。"""
    path = ROOT / f"{name}_recall_summary.csv"
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    total = len(rows)
    hit = sum(1 for r in rows if r["是否告警"] == "是")
    miss = total - hit
    recall = hit / total * 100 if total else 0.0
    return total, hit, miss, recall


def load_missed(name):
    """读 {name}_missed_videos.csv → {视频文件: {last_round, last_dur, last_interval}}"""
    path = ROOT / f"{name}_missed_videos.csv"
    out = {}
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            out[row["视频文件"]] = {
                "last_round": row["轮次"],
                "last_dur": int(row["时长(秒)"]),
                "last_interval": row["时间区间"],
            }
    return out


def load_retest(name):
    """读 alarm_report_{name}.csv（重测统计）→ {video: {...}}"""
    path = Path(tempfile.gettempdir()) / "stream_fight_loop" / f"alarm_report_{name}.csv"
    out = {}
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            out[row["video"]] = {
                "round": row["round"],
                "dur": float(row["duration"]),
                "alarmed": row["alarmed"] == "True",
                "count": int(row["count"]),
                "conf": row["conf"],
                "start": row["start"],
                "end": row["end"],
            }
    return out


def main(name):
    total_full, hit_full, miss_full, recall_full = load_baseline(name)
    last_missed = load_missed(name)
    retest = load_retest(name)

    # 组装对比行（按 last_missed 顺序）
    rows = []
    for v, last in last_missed.items():
        rt = retest.get(v, {})
        rows.append({
            "video": v,
            "last_round": last["last_round"],
            "last_dur": last["last_dur"],
            "last_result": "漏报",
            "last_interval": last["last_interval"],
            "retest_round": rt.get("round", ""),
            "retest_dur": int(rt.get("dur", 60)),
            "retest_result": "命中" if rt.get("alarmed") else "漏报",
            "retest_count": rt.get("count", 0),
            "retest_conf": rt.get("conf", ""),
            "retest_interval": f"{rt.get('start','')}-{rt.get('end','')}",
        })

    hit = sum(1 for r in rows if r["retest_result"] == "命中")
    miss = sum(1 for r in rows if r["retest_result"] == "漏报")
    total_alarms = sum(r["retest_count"] for r in rows)
    n = len(rows)
    retest_rate = hit / n * 100 if n else 0.0

    # ── 写 CSV ──
    csv_path = ROOT / f"{name}_retest_summary.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # ── 写 Markdown 报告 ──
    md = []
    md.append(f"# {name.capitalize()} 漏报视频重测报告")
    md.append("")
    md.append(f'- 目的：验证"短视频漏报是有效时长不足检测窗口"的假设——把上次漏报的 {miss_full} 个视频(原 accumulate 模式下被推成 10~20s)用 repeat 模式延长到 60s 重测')
    md.append("- 重测方式：`stream_loop.py --mode repeat --repeat 12`（5s×12=60s），逐个推流，挂人员行为算法(maskTm=10s, actLvlCountThresh=1)")
    md.append(f"- 数据来源：本次 `alarm_report_{name}.csv` vs 上次 `{name}_missed_videos.csv`")
    md.append("")
    md.append("## 结论")
    md.append("")
    md.append("|---|---|")
    md.append(f"| 视频数 | {n} |")
    md.append(f"| 命中 |  {hit} |")
    md.append(f"| 漏报 |  {miss} |")
    md.append(f"| 命中率| {retest_rate:.1f}% |")
    md.append(f"| 触发告警总次数 | {total_alarms} |")
    md.append("")

    # 漏报视频
    if miss:
        mv.append(r for r in rows if r["retest_result"] == "漏报")
        md.append(f"- 第 {mv['retest_round']} 轮，推流区间 {mv['retest_interval']}（60s）")

    md.append("")
    md.append("## 逐视频对比")
    md.append("")
    md.append("| # | 视频文件 | 上次时长 | 上次结果 | 本次时长 | 本次结果 | 告警数 | 置信度 |")
    md.append("|---|---|---|---|---|---|---|---|")
    for i, r in enumerate(rows, 1):
        flag = "✅ 命中" if r["retest_result"] == "命中" else "❌ 漏报"
        md.append(f"| {i} | {r['video']} | {r['last_dur']}s | 漏报 | {r['retest_dur']}s | {flag} | {r['retest_count']} | {r['retest_conf']} |")
    md.append("")
    md.append("## 修正后的整体召回率")
    md.append("")
    md.append(f"上次全量 {total_full} 个视频：命中 {hit_full}、漏报 {miss_full}，召回率 {recall_full:.2f}%。")
    md.append(f"本次 {miss_full} 个漏报转命中 {hit} 个 → 若按重测结果修正，命中数变为 {hit_full}+{hit}={hit_full+hit}，漏报数变为 {miss}。")
    md.append("")
    md.append("| 指标 | 上次全量 | 重测修正后 |")
    md.append("|---|---|---|")
    md.append(f"| 测试视频总数 | {total_full} | {total_full} |")
    md.append(f"| 命中 | {hit_full} | {hit_full+hit} |")
    md.append(f"| 漏报 | {miss_full} | {miss} |")
    md.append(f"| **召回率** | **{recall_full:.2f}%** | **{(hit_full+hit)/total_full*100:.1f}%** |")
    md.append("")

    md_path = ROOT / f"{name}_retest_report.md"
    md_path.write_text("\n".join(md), encoding="utf-8")

    print(f"已生成：{md_path}")
    print(f"已生成：{csv_path}")
    print(f"\n摘要：{miss_full} 个原漏报 → 命中 {hit}、仍漏报 {miss}，重测修正召回率 {(hit_full+hit)/total_full*100:.1f}%")


if __name__ == "__main__":
    args = parse_args()
    main(args.videos_name)
