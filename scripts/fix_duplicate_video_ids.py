#!/usr/bin/env python3
"""
清理 videos 表中重复的 video_id。

策略：对每个重复的 video_id，保留 id 最大（最新）的记录，
将其余记录关联的数据迁移到保留记录上，然后删除重复记录。

运行方式:
    cd /data/41-benchmark
    source .venv/bin/activate
    python scripts/fix_duplicate_video_ids.py
"""
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / 'benchmark.db'


def main():
    if not DB_PATH.exists():
        print(f'数据库不存在: {DB_PATH}')
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # 1. 找出所有重复的 video_id（保留 id 最大的）
    cursor.execute('''
        SELECT video_id, COUNT(*) as cnt, MAX(id) as keep_id
        FROM videos
        WHERE video_id IS NOT NULL AND video_id != ''
        GROUP BY video_id
        HAVING cnt > 1
    ''')
    duplicates = cursor.fetchall()

    if not duplicates:
        print('没有重复的 video_id，无需清理。')
        conn.close()
        return

    total_dups = sum(d['cnt'] - 1 for d in duplicates)
    print(f'发现 {len(duplicates)} 个重复的 video_id，涉及 {total_dups} 条重复记录。')
    print()

    for dup in duplicates:
        video_id = dup['video_id']
        keep_id = dup['keep_id']

        cursor.execute(
            'SELECT id FROM videos WHERE video_id = ? AND id != ? ORDER BY id',
            (video_id, keep_id)
        )
        dup_ids = [r['id'] for r in cursor.fetchall()]

        print(f'video_id={video_id}: 保留 id={keep_id}, 处理 {len(dup_ids)} 条重复记录 {dup_ids}')

        for dup_id in dup_ids:
            # 2. 迁移 watermarked_videos（打水印记录）
            cursor.execute('''
                UPDATE watermarked_videos SET original_video_id = ?
                WHERE original_video_id = ?
            ''', (keep_id, dup_id))
            wm_migrated = cursor.rowcount

            # 3. 迁移 auto_annotation_tasks（自动标注任务）
            cursor.execute('''
                UPDATE auto_annotation_tasks SET video_db_id = ?
                WHERE video_db_id = ?
            ''', (keep_id, dup_id))
            anno_migrated = cursor.rowcount

            # 4. 处理 events（事件标注）
            # 先查出重复记录的所有事件
            cursor.execute('''
                SELECT id, event_type, start_seconds, end_seconds
                FROM events WHERE video_db_id = ?
            ''', (dup_id,))
            dup_events = cursor.fetchall()

            migrated_events = 0
            dropped_events = 0
            for ev in dup_events:
                # 检查保留记录是否已有完全相同的事件
                cursor.execute('''
                    SELECT id FROM events
                    WHERE video_db_id = ? AND event_type = ?
                      AND start_seconds = ? AND end_seconds = ?
                ''', (keep_id, ev['event_type'], ev['start_seconds'], ev['end_seconds']))
                existing = cursor.fetchone()

                if existing:
                    # 重复：将 gt_frames 解绑（设 event_id=NULL，更新 video_db_id），
                    # 然后删除 dup 事件（避免 CASCADE 误删 gt_frames）
                    cursor.execute('''
                        UPDATE gt_frames
                        SET event_id = NULL, video_db_id = ?
                        WHERE event_id = ?
                    ''', (keep_id, ev['id']))
                    cursor.execute('DELETE FROM events WHERE id = ?', (ev['id'],))
                    dropped_events += 1
                else:
                    # 不重复：直接迁移 event 的 video_db_id
                    cursor.execute('''
                        UPDATE events SET video_db_id = ? WHERE id = ?
                    ''', (keep_id, ev['id']))
                    migrated_events += 1

            # 5. 迁移剩余 gt_frames（没有 event_id 关联的）
            cursor.execute('''
                UPDATE gt_frames SET video_db_id = ?
                WHERE video_db_id = ?
            ''', (keep_id, dup_id))
            gt_migrated = cursor.rowcount

            # 6. 删除重复 videos 记录
            # 此时所有外键关联的数据要么已迁移，要么已处理
            cursor.execute('DELETE FROM videos WHERE id = ?', (dup_id,))
            deleted = cursor.rowcount

            print(f'  删除 id={dup_id}: watermark迁移={wm_migrated}, 事件迁移={migrated_events}, 事件丢弃={dropped_events}, gt迁移={gt_migrated}')

        conn.commit()
        print()

    # 7. 验证
    cursor.execute('''
        SELECT COUNT(*) FROM (
            SELECT video_id FROM videos
            WHERE video_id IS NOT NULL AND video_id != ''
            GROUP BY video_id HAVING COUNT(*) > 1
        )
    ''')
    remaining = cursor.fetchone()[0]
    if remaining == 0:
        print('清理完成，已无重复 video_id。')
    else:
        print(f'警告：仍有 {remaining} 个 video_id 存在重复！')

    conn.close()


if __name__ == '__main__':
    main()
