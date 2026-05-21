#!/usr/bin/env python3
"""
一次性迁移脚本：将所有 ground truth 时间偏移 -5 秒。
用于回退"视频开头增加 5 秒空白页"的改动。
"""
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
GT_DIR = PROJECT_ROOT / 'ground_truth'
DB_PATH = PROJECT_ROOT / 'benchmark.db'


def migrate_json_files():
    """偏移所有 ground_truth/*.json 的 start/end -5 秒"""
    if not GT_DIR.exists():
        print("ground_truth 目录不存在，跳过 JSON 迁移")
        return 0

    count = 0
    for gt_file in GT_DIR.glob('*.json'):
        try:
            with open(gt_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            events = data.get('events', [])
            modified = False
            for evt in events:
                if 'start' in evt:
                    evt['start'] = round(evt['start'] - 5, 3)
                    modified = True
                if 'end' in evt:
                    evt['end'] = round(evt['end'] - 5, 3)
                    modified = True

            if modified:
                with open(gt_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                count += 1
                print(f"  + {gt_file.name}")
        except Exception as e:
            print(f"  ! 跳过 {gt_file.name}: {e}")

    print(f"JSON 迁移完成: {count} 个文件已修改")
    return count


def migrate_db():
    """偏移数据库 events 和 ground_truth 表的时间 -5 秒"""
    if not DB_PATH.exists():
        print("数据库不存在，跳过数据库迁移")
        return 0, 0

    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()

    # 1. events 表
    cursor.execute("UPDATE events SET start_seconds = start_seconds - 5, end_seconds = end_seconds - 5")
    events_updated = cursor.rowcount

    # 2. ground_truth 表
    cursor.execute("UPDATE ground_truth SET start_seconds = start_seconds - 5, end_seconds = end_seconds - 5")
    gt_updated = cursor.rowcount

    conn.commit()
    conn.close()

    print(f"数据库迁移完成: events={events_updated}, ground_truth={gt_updated}")
    return events_updated, gt_updated


if __name__ == '__main__':
    print("开始 ground truth -5 秒回退...")
    print("-" * 40)
    migrate_json_files()
    print("-" * 40)
    migrate_db()
    print("-" * 40)
    print("回退完成。请确认所有视频已重新打水印（不带 5 秒空白页）。")
