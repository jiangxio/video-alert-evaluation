
"""
计算 推流测试集召回率 + 整理告警表格。

输入：
  - alarm_report_fight.csv   （stream_loop.py 产出的每轮视频告警统计）
  - aibox_alarm_20260805/    （aibox-alarm-fetcher skill 拉取的当日告警元数据 metadata/*.json）

输出（写到脚本同目录，按 --videos-name 命名）：
  - {name}_recall_summary.csv     每个视频一行：是否告警、告警数、置信度、时间区间
  - {name}_alarmed_videos.csv     产生告警的视频表
  - {name}_missed_videos.csv      未产生告警的视频表
  - {name}_recall_report.md       人类可读的召回率报告（含两张 Markdown 表）

命令行参数：
  --videos-name NAME   数据集名称，定位 alarm_report_{NAME}.csv（默认: fight）
  --date YYYYMMDD       告警元数据产生时间，定位 aibox_alarm_{YYYYMMDD}/（默认: 20260805）
"""
import argparse
import csv
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent


def parse_args():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--videos-name", default="fight",
                    help="计算的数据集名称 (默认: fight)")
    ap.add_argument("--date", default="20260805",
                    help="告警元数据产生时间 YYYYMMDD (默认: 20260805)")
    return ap.parse_args()


def load_rounds(csv_path):
    rows = []
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            r["round"] = int(r["round"])
            r["duration"] = float(r["duration"])
            r["alarmed"] = r["alarmed"].strip().lower() == "true"
            r["count"] = int(r["count"])
            rows.append(r)
    return rows


def parse_hms_to_sec(t: str) -> int:
    """'10:31:53' -> 当天从 0 点起的秒数"""
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def load_alarm_meta(alarm_meta_dir):
    """读 metadata/*.json，返回告警 dict 列表"""
    if not alarm_meta_dir.exists():
        return []
    out = []
    for p in sorted(alarm_meta_dir.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            pass
    return out


def main(name, date):
    csv_path = BASE / f"alarm_report_{name}.csv"
    alarm_meta_dir = BASE / f"aibox_alarm_{date}" / "metadata"
    rounds = load_rounds(csv_path)
    alarms = load_alarm_meta(alarm_meta_dir)

    total = len(rounds)
    alarmed_rows = [r for r in rounds if r["alarmed"]]
    missed_rows = [r for r in rounds if not r["alarmed"]]
    hit = len(alarmed_rows)
    miss = len(missed_rows)
    recall = hit / total * 100 if total else 0.0
    total_alarm_count = sum(r["count"] for r in rounds)

    # ── 把当日告警按时间归到各视频轮次，校验每轮 count ──
    # alarm time 形如 '2026-08-05 10:31:53'，只取 HH:MM:SS 转秒
    alarm_secs = []
    for a in alarms:
        t = str(a.get("time", ""))
        try:
            hhmmss = t.split(" ")[1]
        except IndexError:
            continue
        alarm_secs.append(parse_hms_to_sec(hhmmss))

    for r in rounds:
        s = parse_hms_to_sec(r["start"])
        e = parse_hms_to_sec(r["end"])
        # 落在该视频 [start, end) 区间的告警数（不去延迟，纯时间归并）
        r["meta_count"] = sum(1 for a in alarm_secs if s <= a < e)

    # ── 写汇总 CSV（每视频一行）──
    summary_path = BASE / f"{name}_recall_summary.csv"
    with open(summary_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["轮次", "视频文件", "时长(秒)", "是否告警",
                    "告警数(csv)", "告警数(元数据)", "置信度", "起", "止"])
        for r in rounds:
            w.writerow([r["round"], r["video"], f"{r['duration']:.0f}",
                        "是" if r["alarmed"] else "否",
                        r["count"], r["meta_count"],
                        r["conf"], r["start"], r["end"]])

    # ── 产生告警的视频表 ──
    alarmed_path = BASE / f"{name}_alarmed_videos.csv"
    with open(alarmed_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["轮次", "视频文件", "时长(秒)", "告警数", "置信度", "时间区间"])
        for r in alarmed_rows:
            w.writerow([r["round"], r["video"], f"{r['duration']:.0f}",
                        r["count"], r["conf"], f"{r['start']}-{r['end']}"])

    # ── 未产生告警的视频表 ──
    missed_path = BASE / f"{name}_missed_videos.csv"
    with open(missed_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["轮次", "视频文件", "时长(秒)", "时间区间"])
        for r in missed_rows:
            w.writerow([r["round"], r["video"], f"{r['duration']:.0f}",
                        f"{r['start']}-{r['end']}"])

    # ── Markdown 报告 ──
    md = []
    md.append(f"# {name.capitalize()} 推流测试集 — 召回率报告\n")
    md.append(f"- 推流日期：{date}")
    md.append(f"- 数据来源：`alarm_report_{name}.csv` + 告警元数据（aibox-alarm-fetcher skill 拉取）")
    md.append(f"- 流名称 / 通道：{name}")
    md.append(f"- 推流时间区间：{rounds[0]['start']} ~ {rounds[-1]['end']}\n")
    md.append("## 召回率\n")
    md.append("| 指标 | 值 |")
    md.append("|---|---|")
    md.append(f"| 测试视频总数 | {total} |")
    md.append(f"| 产生告警的视频（命中） | {hit} |")
    md.append(f"| 未产生告警的视频（漏报） | {miss} |")
    md.append(f"| **召回率 (Recall)** | **{recall:.2f}%** |")
    md.append(f"| 告警总触发次数 | {total_alarm_count} |")
    md.append(f"| 当日 {name} 通道告警元数据条数 | {len(alarms)} |\n")

    md.append("## 产生告警的视频（共 {} 个）\n".format(hit))
    md.append("| 轮次 | 视频文件 | 时长 | 告警数 | 置信度 | 时间区间 |")
    md.append("|---|---|---|---|---|---|")
    for r in alarmed_rows:
        md.append(f"| {r['round']} | {r['video']} | {r['duration']:.0f}s | {r['count']} | {r['conf']} | {r['start']}-{r['end']} |")

    md.append(f"\n## 未产生告警的视频（共 {miss} 个，漏报）\n")
    md.append("| 轮次 | 视频文件 | 时长 | 时间区间 |")
    md.append("|---|---|---|---|")
    for r in missed_rows:
        md.append(f"| {r['round']} | {r['video']} | {r['duration']:.0f}s | {r['start']}-{r['end']} |")

    md_path = BASE / f"{name}_recall_report.md"
    md_path.write_text("\n".join(md), encoding="utf-8")

    # 控制台摘要
    print("=" * 56)
    print(f"{name.capitalize()} 推流测试集 召回率统计")
    print("=" * 56)
    print(f"视频总数      : {total}")
    print(f"产生告警(命中) : {hit}")
    print(f"未产生告警(漏报): {miss}")
    print(f"召回率        : {recall:.2f}%")
    print(f"告警总触发次数 : {total_alarm_count}")
    print(f"当日告警元数据 : {len(alarms)} 条 ({name} 通道)")
    print("-" * 56)
    print("输出文件：")
    print(f"  {summary_path}")
    print(f"  {alarmed_path}")
    print(f"  {missed_path}")
    print(f"  {md_path}")
    print("=" * 56)


if __name__ == "__main__":
    args = parse_args()
    main(args.videos_name, args.date)
