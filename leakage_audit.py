#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
泄露核查：fight 召回率评测是否存在跨 run / 跨轮次的告警归因污染。

输入：
  - alarm_report_fight.csv     首轮 47 段推流区间（accumulate 模式，10:31:53~11:06:47）
  - fight_retest_summary.csv   重测 14 段区间（repeat 60s，15:23~15:37）
  - aibox_alarm_20260805/metadata/*.json  当日 fight 通道全部 100 条告警

核查向量：
  A. 当日告警时间轴：多少落在首轮窗口 / 重测窗口 / 两者之外（测试前/后的污染）
  B. 两套归因逻辑差异：stream_fight_loop.py(推 DETECT_DELAY=8s) vs compute_fight_recall.py(推 0s)
     —— 同一条告警在两套逻辑下归到不同视频，导致 hit/miss 翻转
  C. 跨轮次 spillover：相邻段间隙告警被时移挪到邻段
  D. 跨 run 污染：首轮 run 与重测 run 之间是否有告警串味
"""
import csv, json, datetime as dt
from pathlib import Path

BASE = Path(__file__).resolve().parent
FIRST_CSV = BASE / "alarm_report_fight.csv"
RETEST_CSV = BASE / "fight_retest_summary.csv"
META_DIR = BASE / "aibox_alarm_20260805" / "metadata"
DETECT_DELAY = 8.0  # stream_fight_loop.py 里的检测延迟估计

def hms_to_sec(t):
    """'10:31:53' or '2026-08-05 10:31:53' -> 当天0点起的秒数(float)"""
    t = str(t).strip()
    if " " in t:
        t = t.split(" ")[1]
    h, m, s = t.split(":")
    return int(h)*3600 + int(m)*60 + float(s)

def load_alarms():
    alarms = []
    for p in sorted(META_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        t = str(d.get("time",""))
        if not t:
            continue
        ts = hms_to_sec(t)
        objs = d.get("objects") or []
        conf = round(float(objs[0].get("confidence",0) or 0), 2) if objs else 0.0
        alarms.append({"time": t, "sec": ts, "conf": conf, "id": d.get("id")})
    return alarms

def load_first_rounds():
    """首轮 47 段。返回 [(round, video, dur, start_sec, end_sec), ...]"""
    rows = []
    with open(FIRST_CSV, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            rows.append({
                "round": int(r["round"]),
                "video": r["video"],
                "dur": float(r["duration"]),
                "s": hms_to_sec(r["start"]),
                "e": hms_to_sec(r["end"]),
                "csv_count": int(r["count"]),
                "csv_alarmed": r["alarmed"].strip().lower() == "true",
            })
    return rows

def load_retest_rounds():
    rows = []
    with open(RETEST_CSV, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            si = r["retest_interval"]
            # 形如 '15:23:01-15:24:00'
            s, e = si.split("-")
            rows.append({
                "round": int(r["retest_round"]),
                "video": r["video"],
                "dur": float(r["retest_dur"]),
                "s": hms_to_sec(s),
                "e": hms_to_sec(e),
                "result": r["retest_result"],
                "count": int(r["retest_count"]),
            })
    return rows

def assign(alarms, rounds, delay):
    """把告警时间往前推 delay 秒后归到 [s,e) 区间。返回每轮命中告警列表 + 未归因告警列表。"""
    assigned = {r["round"]: [] for r in rounds}
    used = set()
    for a in alarms:
        pt = a["sec"] - delay
        for r in rounds:
            if r["s"] <= pt < r["e"]:
                assigned[r["round"]].append(a)
                used.add(a["id"])
                break
    orphan = [a for a in alarms if a["id"] not in used]
    return assigned, orphan

def fmt(sec):
    sec = int(round(sec))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:d}:{m:02d}:{s:02d}"

def main():
    alarms = load_alarms()
    first = load_first_rounds()
    retest = load_retest_rounds()

    fs, fe = first[0]["s"], first[-1]["e"]
    rs, re_ = retest[0]["s"], retest[-1]["e"]

    print("="*70)
    print("A. 当日告警时间轴分布（共 %d 条 fight 通道告警）" % len(alarms))
    print("="*70)
    before = [a for a in alarms if a["sec"] < fs]
    in_first = [a for a in alarms if fs <= a["sec"] < fe]
    between = [a for a in alarms if fe <= a["sec"] < rs]
    in_retest = [a for a in alarms if rs <= a["sec"] < re_]
    after = [a for a in alarms if a["sec"] >= re_]
    print(f"  首轮测试窗口 [{fmt(fs)}~{fmt(fe)}] 内 : {len(in_first)} 条")
    print(f"  首轮前(测试开始前)        : {len(before)} 条  区间 [{fmt(0)}~{fmt(fs)}]")
    print(f"  首轮后~重测前(两 run 之间): {len(between)} 条  区间 [{fmt(fe)}~{fmt(rs)}]")
    print(f"  重测窗口 [{fmt(rs)}~{fmt(re_)}] 内  : {len(in_retest)} 条")
    print(f"  重测后                    : {len(after)} 条")
    if before:
        print(f"  ⚠ 首轮测试前就有 {len(before)} 条告警 —— 最早 {before[0]['time']} (测试 {fmt(fs)} 才开始)")
        print(f"    说明当天同通道有更早的推流 run，或算法任务持续挂着。")
    print()

    print("="*70)
    print("B. 两套归因逻辑对首轮的影响（同 100 条告警，仅首轮 47 段）")
    print("="*70)
    # 只用首轮窗口内的告警 + 可能 spillover 的（用全部告警，delay 会把窗口外的拉进来）
    for delay, name in [(0, "compute_fight_recall.py (delay=0)"), (DETECT_DELAY, f"stream_fight_loop.py (delay={DETECT_DELAY}s)")]:
        assigned, orphan = assign(alarms, first, delay)
        hit = sum(1 for r in first if assigned[r["round"]])
        miss = len(first) - hit
        # 和 csv(脚本产出) 比对
        diff = []
        for r in first:
            my_count = len(assigned[r["round"]])
            if my_count != r["csv_count"]:
                diff.append((r["round"], r["video"], r["csv_count"], my_count))
        print(f"\n  [{name}]")
        print(f"    命中 {hit}/47, 漏报 {miss}/47, 召回率 {hit/47*100:.1f}%")
        print(f"    与脚本 CSV 告警数不一致的轮次: {len(diff)}")
        for rnd, vid, cc, mc in diff:
            tag = "← 翻转风险" if (cc==0) != (mc==0) else ""
            print(f"      轮{rnd:>2} {vid:<24} csv={cc} 本逻辑={mc} {tag}")
        # 漏报名单
        missed = [r for r in first if not assigned[r["round"]]]
        print(f"    漏报视频({len(missed)}): {[r['video'] for r in missed]}")

    print()
    print("="*70)
    print("C. 跨轮次 spillover：检测延迟把告警推到相邻段")
    print("="*70)
    # 用 delay=0 vs delay=8 看哪些告警在两者间归到了不同轮次
    a0, _ = assign(alarms, first, 0)
    a8, _ = assign(alarms, first, DETECT_DELAY)
    spillover = []
    for r in first:
        ids0 = {a["id"] for a in a0[r["round"]]}
        ids8 = {a["id"] for a in a8[r["round"]]}
        sym = ids0.symmetric_difference(ids8)
        if sym:
            spillover.append((r["round"], r["video"], len(ids0), len(ids8), len(sym)))
    print(f"  归因受 delay 影响的轮次: {len(spillover)}/47")
    if spillover:
        print(f"  {'轮':>3} {'视频':<24} {'d=0':>4} {'d=8':>4} {'差':>4}")
        for rnd, vid, c0, c8, d in spillover:
            print(f"  {rnd:>3} {vid:<24} {c0:>4} {c8:>4} {d:>4}")
    # 关键：有没有「d=0 命中但 d=8 漏报」或反之的翻转
    flip_to_hit = []
    flip_to_miss = []
    for r in first:
        h0 = len(a0[r["round"]]) > 0
        h8 = len(a8[r["round"]]) > 0
        if h0 and not h8:
            flip_to_miss.append(r)  # delay=8 把它判漏报
        elif h8 and not h0:
            flip_to_hit.append(r)   # delay=8 把它判命中
    print(f"\n  delay=0→8 翻转为漏报的视频: {[r['video'] for r in flip_to_miss]}")
    print(f"  delay=0→8 翻转为命中的视频: {[r['video'] for r in flip_to_hit]}")

    print()
    print("="*70)
    print("D. 跨 run 污染：重测 14 段的告警是否也参与首轮归因")
    print("="*70)
    # 重测窗口的告警，若用首轮 rounds 归因(delay=8)会不会被错误拉进首轮？
    retest_alarms = [a for a in alarms if rs <= a["sec"] < re_]
    pulled = []
    for a in retest_alarms:
        pt = a["sec"] - DETECT_DELAY
        for r in first:
            if r["s"] <= pt < r["e"]:
                pulled.append((a, r))
                break
    print(f"  重测窗口告警 {len(retest_alarms)} 条，被首轮(delay=8)误归因: {len(pulled)} 条")
    # 反向：首轮窗口告警被重测误归因
    first_alarms = [a for a in alarms if fs <= a["sec"] < fe]
    pulled2 = []
    for a in first_alarms:
        pt = a["sec"] - DETECT_DELAY
        for r in retest:
            if r["s"] <= pt < r["e"]:
                pulled2.append((a, r))
                break
    print(f"  首轮窗口告警 {len(first_alarms)} 条，被重测(delay=8)误归因: {len(pulled2)} 条")

    # 段间隙
    print()
    print("="*70)
    print("E. 首轮 47 段的相邻间隙（gap）—— 漏报短视频的告警可能漂移到这里")
    print("="*70)
    gaps = []
    for i in range(len(first)-1):
        g = first[i+1]["s"] - first[i]["e"]
        gaps.append((first[i]["round"], first[i+1]["round"], g))
    maxg = max(gaps, key=lambda x: x[2])
    ming = min(gaps, key=lambda x: x[2])
    avg = sum(g for _,_,g in gaps)/len(gaps)
    print(f"  共 {len(gaps)} 个间隙，平均 {avg:.1f}s，最小 {ming[2]:.1f}s(轮{ming[0]}→{ming[1]})，最大 {maxg[2]:.1f}s(轮{maxg[0]}→{maxg[1]})")
    # 落在间隙里的告警(delay=0，即未被任何段捕获的、但在首轮时间跨度内的)
    in_span_not_in_any = []
    for a in alarms:
        if fs <= a["sec"] < fe:
            inside = any(r["s"] <= a["sec"] < r["e"] for r in first)
            if not inside:
                in_span_not_in_any.append(a)
    print(f"  落在首轮时间跨度内但不在任何段窗口内的告警(delay=0): {len(in_span_not_in_any)} 条")
    if in_span_not_in_any:
        for a in in_span_not_in_any[:10]:
            # 找它在哪个间隙
            loc = "间隙"
            for i in range(len(first)-1):
                if first[i]["e"] <= a["sec"] < first[i+1]["s"]:
                    loc = f"轮{first[i]['round']}~轮{first[i+1]['round']}间隙"
                    break
            print(f"    {a['time']} conf={a['conf']} @ {loc}")

if __name__ == "__main__":
    main()
