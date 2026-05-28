"""评测相关路由 - 基于告警数据集 + 评测视频集/GT帧"""
from flask import Blueprint, request, jsonify, render_template, current_app, send_file
from pathlib import Path
from datetime import datetime
import json
import threading
import subprocess
import io

from app.database import get_db, DATABASE_PATH
from app.routes import send_file_with_cache
from app.services.eval_service import (
    calc_expected_count,
    get_effective_status,
    analyze_merged_events,
    get_font,
    generate_report_image,
)

bp = Blueprint('evaluation', __name__, url_prefix='/evaluation')

# 评测执行进度（内存存储）
_eval_progress = {}
_eval_lock = threading.Lock()


def _get_all_event_types():
    """从 report/config.json 读取所有事件类型列表（按配置顺序）"""
    config_path = Path(current_app.config['ALERT_TYPES_CONFIG'])
    event_types = []
    if config_path.exists():
        with open(config_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(maxsplit=1)
                if len(parts) == 2:
                    event_types.append(parts[1])
    return event_types


# ── 页面路由 ──────────────────────────────────────────────────────────────────

@bp.route('/')
def evaluation_page():
    """评测任务列表页"""
    return render_template('evaluation.html')


@bp.route('/<int:task_id>/')
def eval_task_page(task_id):
    """评测任务详情页"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, name, notes, dataset_id, alert_eval_set_id, eval_set_id, merge_interval_sec, event_start_sec, event_end_sec, event_interval_sec, trigger_rate, min_event_duration_sec, status, created_at, finalized, accuracy, recall, avg_fp_per_hour, event_metrics, confirmed_at FROM eval_tasks WHERE id = ?', (task_id,))
    task = cursor.fetchone()
    if not task:
        return '任务不存在', 404
    return render_template('eval_task.html', task=dict(task))


# ── API 路由 ──────────────────────────────────────────────────────────────────

@bp.route('/api/eval-sets', methods=['GET'])
def list_eval_sets():
    """获取所有评测视频集"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, name, notes, video_ids, created_at FROM eval_video_sets ORDER BY created_at DESC')
    sets = cursor.fetchall()

    result = []
    for s in sets:
        s_dict = dict(s)
        if s_dict.get('video_ids'):
            try:
                s_dict['video_ids'] = json.loads(s_dict['video_ids'])
            except Exception:
                s_dict['video_ids'] = []
        else:
            s_dict['video_ids'] = []

        # 统计视频数量和GT帧数量
        video_count = len(s_dict['video_ids'])
        gt_frame_count = 0
        if s_dict['video_ids']:
            placeholders = ','.join('?' for _ in s_dict['video_ids'])
            cursor.execute(f'''
                SELECT COUNT(*) FROM gt_frames WHERE video_db_id IN ({placeholders})
            ''', s_dict['video_ids'])
            row = cursor.fetchone()
            gt_frame_count = row[0] if row else 0

        s_dict['video_count'] = video_count
        s_dict['gt_frame_count'] = gt_frame_count
        result.append(s_dict)

    return jsonify({'sets': result})


@bp.route('/api/tasks', methods=['GET'])
def list_tasks():
    """列出所有评测任务"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, name, notes, dataset_id, alert_eval_set_id, eval_set_id, merge_interval_sec, event_start_sec, event_end_sec, event_interval_sec, trigger_rate, min_event_duration_sec, status, created_at, finalized, accuracy, recall, avg_fp_per_hour, event_metrics, confirmed_at FROM eval_tasks ORDER BY created_at DESC')
    tasks = [dict(t) for t in cursor.fetchall()]

    for t in tasks:
        if t.get('dataset_id'):
            cursor.execute('SELECT name FROM datasets WHERE id = ?', (t['dataset_id'],))
            d = cursor.fetchone()
            t['dataset_name'] = d['name'] if d else None
        if t.get('alert_eval_set_id'):
            cursor.execute('SELECT name FROM eval_alert_sets WHERE id = ?', (t['alert_eval_set_id'],))
            d = cursor.fetchone()
            t['alert_eval_set_name'] = d['name'] if d else None
        if t.get('eval_set_id'):
            cursor.execute('SELECT name FROM eval_video_sets WHERE id = ?', (t['eval_set_id'],))
            d = cursor.fetchone()
            t['eval_set_name'] = d['name'] if d else None
    return jsonify(tasks)


@bp.route('/api/tasks/<int:task_id>', methods=['DELETE'])
def delete_task(task_id):
    """删除评测任务"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id FROM eval_tasks WHERE id = ?', (task_id,))
    if not cursor.fetchone():
        return jsonify({'error': '任务不存在'}), 404

    cursor.execute('DELETE FROM eval_results WHERE task_id = ?', (task_id,))
    cursor.execute('DELETE FROM eval_merged_events WHERE task_id = ?', (task_id,))
    cursor.execute('DELETE FROM eval_gt_events WHERE task_id = ?', (task_id,))
    cursor.execute('DELETE FROM eval_tasks WHERE id = ?', (task_id,))
    db.commit()

    return jsonify({'success': True})


@bp.route('/api/tasks', methods=['POST'])
def create_task():
    """新建评测任务"""
    data = request.get_json() or {}
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': '任务名称不能为空'}), 400

    dataset_id = data.get('dataset_id')
    alert_eval_set_id = data.get('alert_eval_set_id')
    if not dataset_id and not alert_eval_set_id:
        return jsonify({'error': '请选择告警数据集或告警评测集'}), 400

    eval_set_id = data.get('eval_set_id')
    if not eval_set_id:
        return jsonify({'error': '请选择评测视频集'}), 400

    db = get_db()
    cursor = db.cursor()

    if dataset_id:
        cursor.execute('SELECT id FROM datasets WHERE id = ?', (dataset_id,))
        if not cursor.fetchone():
            return jsonify({'error': '告警数据集不存在'}), 404

    if alert_eval_set_id:
        cursor.execute('SELECT id FROM eval_alert_sets WHERE id = ?', (alert_eval_set_id,))
        if not cursor.fetchone():
            return jsonify({'error': '告警评测集不存在'}), 404

    cursor.execute('SELECT id FROM eval_video_sets WHERE id = ?', (eval_set_id,))
    if not cursor.fetchone():
        return jsonify({'error': '评测视频集不存在'}), 404

    cursor.execute('''
        INSERT INTO eval_tasks
        (name, notes, dataset_id, alert_eval_set_id, eval_set_id, merge_interval_sec, event_start_sec,
         event_end_sec, event_interval_sec, trigger_rate, min_event_duration_sec, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        name,
        data.get('notes', ''),
        dataset_id,
        alert_eval_set_id,
        eval_set_id,
        data.get('merge_interval_sec', 5.0),
        0,
        0,
        data.get('event_interval_sec', 10.0),
        data.get('trigger_rate', 0.5),
        data.get('min_event_duration_sec', 0),
        'created',
    ))
    db.commit()
    task_id = cursor.lastrowid

    cursor.execute('SELECT id, name, notes, dataset_id, alert_eval_set_id, eval_set_id, merge_interval_sec, event_start_sec, event_end_sec, event_interval_sec, trigger_rate, min_event_duration_sec, status, created_at, finalized, accuracy, recall, avg_fp_per_hour, event_metrics, confirmed_at FROM eval_tasks WHERE id = ?', (task_id,))
    return jsonify({'success': True, 'task': dict(cursor.fetchone())})


@bp.route('/api/tasks/<int:task_id>', methods=['GET'])
def get_task(task_id):
    """获取任务详情"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, name, notes, dataset_id, alert_eval_set_id, eval_set_id, merge_interval_sec, event_start_sec, event_end_sec, event_interval_sec, trigger_rate, min_event_duration_sec, status, created_at, finalized, accuracy, recall, avg_fp_per_hour, event_metrics, confirmed_at FROM eval_tasks WHERE id = ?', (task_id,))
    task = cursor.fetchone()
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    return jsonify(dict(task))


@bp.route('/api/tasks/<int:task_id>', methods=['PUT'])
def update_task(task_id):
    """更新任务参数"""
    data = request.get_json() or {}
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id FROM eval_tasks WHERE id = ?', (task_id,))
    if not cursor.fetchone():
        return jsonify({'error': '任务不存在'}), 404

    # 更新参数
    update_fields = []
    update_values = []

    if 'merge_interval_sec' in data:
        update_fields.append('merge_interval_sec = ?')
        update_values.append(data['merge_interval_sec'])
    if 'event_interval_sec' in data:
        update_fields.append('event_interval_sec = ?')
        update_values.append(data['event_interval_sec'])
    if 'trigger_rate' in data:
        update_fields.append('trigger_rate = ?')
        update_values.append(data['trigger_rate'])
    if 'min_event_duration_sec' in data:
        update_fields.append('min_event_duration_sec = ?')
        update_values.append(data['min_event_duration_sec'])

    if update_fields:
        update_values.append(task_id)
        cursor.execute(
            f'UPDATE eval_tasks SET {", ".join(update_fields)} WHERE id = ?',
            update_values
        )
        db.commit()

    cursor.execute('SELECT id, name, notes, dataset_id, alert_eval_set_id, eval_set_id, merge_interval_sec, event_start_sec, event_end_sec, event_interval_sec, trigger_rate, min_event_duration_sec, status, created_at, finalized, accuracy, recall, avg_fp_per_hour, event_metrics, confirmed_at FROM eval_tasks WHERE id = ?', (task_id,))
    return jsonify({'success': True, 'task': dict(cursor.fetchone())})


@bp.route('/api/tasks/<int:task_id>/analyze', methods=['POST'])
def analyze_task(task_id):
    """分析可合并事件"""
    try:
        db = get_db()
        result = analyze_merged_events(task_id, db)
        if result is None:
            return jsonify({'error': '任务不存在'}), 404
        return jsonify({'success': True, **result})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'分析出错: {str(e)}'}), 500


@bp.route('/api/tasks/<int:task_id>/confirm', methods=['POST'])
def confirm_merged(task_id):
    """保存用户确认的合并告警和 GT 事件"""
    data = request.get_json() or {}
    merged_alerts = data.get('merged_alerts', [])
    gt_events = data.get('gt_events', [])

    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, name, notes, dataset_id, alert_eval_set_id, eval_set_id, merge_interval_sec, event_start_sec, event_end_sec, event_interval_sec, trigger_rate, min_event_duration_sec, status, created_at, finalized, accuracy, recall, avg_fp_per_hour, event_metrics, confirmed_at FROM eval_tasks WHERE id = ?', (task_id,))
    task = cursor.fetchone()
    if not task:
        return jsonify({'error': '任务不存在'}), 404

    # ── 校验：告警 video_id 是否全部包含在评测视频集中 ────────────────────────
    eval_set_id = task['eval_set_id']
    cursor.execute('SELECT video_ids FROM eval_video_sets WHERE id = ?', (eval_set_id,))
    eval_set = cursor.fetchone()
    eval_video_db_ids = []
    if eval_set and eval_set['video_ids']:
        try:
            eval_video_db_ids = json.loads(eval_set['video_ids'])
        except Exception:
            eval_video_db_ids = []

    eval_video_ids = set()
    if eval_video_db_ids:
        placeholders = ','.join('?' for _ in eval_video_db_ids)
        cursor.execute(f'SELECT video_id FROM videos WHERE id IN ({placeholders})', eval_video_db_ids)
        for row in cursor.fetchall():
            if row['video_id']:
                eval_video_ids.add(row['video_id'])

    alert_video_ids = set(m['video_id'] for m in merged_alerts if m.get('video_id'))
    missing = sorted(alert_video_ids - eval_video_ids)
    if missing:
        return jsonify({
            'error': '以下 video_id 不在评测视频集中，无法确认：' + ', '.join(missing),
            'missing_video_ids': missing,
        }), 400

    # 清除旧数据
    cursor.execute('DELETE FROM eval_merged_events WHERE task_id = ?', (task_id,))
    cursor.execute('DELETE FROM eval_gt_events WHERE task_id = ?', (task_id,))

    for m in merged_alerts:
        cursor.execute('''
            INSERT INTO eval_merged_events
            (task_id, video_id, event_type, image_ids,
             representative_image_id, ts_start, ts_end,
             start_sec, end_sec, expected_count, confirmed_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            task_id,
            m['video_id'],
            m['event_type'],
            json.dumps(m.get('image_ids', []), ensure_ascii=False),
            m.get('representative_image_id'),
            m.get('ts_start'),
            m.get('ts_end'),
            m.get('ts_start'),  # start_sec = ts_start for compatibility
            m.get('ts_end'),
            len(m.get('image_ids', [])),
            len(m.get('image_ids', [])),
        ))

    for g in gt_events:
        cursor.execute('''
            INSERT INTO eval_gt_events
            (task_id, gt_event_id, video_id, event_type,
             start_sec, end_sec, expected_count, confirmed_count,
             mid_frame_id, mid_frame_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            task_id,
            g.get('gt_event_id'),
            g['video_id'],
            g['event_type'],
            g.get('start_sec'),
            g.get('end_sec'),
            g.get('expected_count', 1),
            g.get('confirmed_count', g.get('expected_count', 1)),
            g.get('mid_frame_id'),
            g.get('mid_frame_path'),
        ))

    cursor.execute('UPDATE eval_tasks SET status = ? WHERE id = ?', ('confirming', task_id))
    db.commit()
    return jsonify({'success': True})


@bp.route('/api/tasks/<int:task_id>/execute', methods=['POST'])
def execute_task(task_id):
    """执行评测（后台线程）"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, name, notes, dataset_id, alert_eval_set_id, eval_set_id, merge_interval_sec, event_start_sec, event_end_sec, event_interval_sec, trigger_rate, min_event_duration_sec, status, created_at, finalized, accuracy, recall, avg_fp_per_hour, event_metrics, confirmed_at FROM eval_tasks WHERE id = ?', (task_id,))
    task = cursor.fetchone()
    if not task:
        return jsonify({'error': '任务不存在'}), 404

    with _eval_lock:
        if _eval_progress.get(task_id, {}).get('running'):
            return jsonify({'error': '评测正在运行中'}), 409
        _eval_progress[task_id] = {
            'total': 0,
            'done': 0,
            'running': True,
        }

    def _worker():
        import sqlite3
        conn = sqlite3.connect(str(DATABASE_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # 加载合并告警和 GT 事件
        cur.execute('SELECT id, task_id, video_id, event_type, start_sec, end_sec, expected_count, confirmed_count, image_ids, confirmed_at, ts_start, ts_end, representative_image_id, is_false_positive, matched_gt_event_id, manual_status FROM eval_merged_events WHERE task_id = ? ORDER BY ts_start, id', (task_id,))
        merged_list = [dict(m) for m in cur.fetchall()]

        cur.execute('SELECT id, task_id, gt_event_id, video_id, event_type, start_sec, end_sec, expected_count, confirmed_count, actual_count, mid_frame_id, mid_frame_path, created_at FROM eval_gt_events WHERE task_id = ?', (task_id,))
        gt_list = [dict(g) for g in cur.fetchall()]

        total = len(merged_list) + len(gt_list)
        with _eval_lock:
            _eval_progress[task_id]['total'] = total
            _eval_progress[task_id]['done'] = 0

        # 先删旧结果
        cur.execute('DELETE FROM eval_results WHERE task_id = ?', (task_id,))
        conn.commit()

        # ── 判断每个合并告警是否命中 GT 事件 ──────────────────────────────────
        # 命中条件：video_id 相同 AND event_type 相同
        #           AND (ts_start 或 ts_end) 落在 GT 事件 [start_sec, end_sec]
        gt_hit_counts = {g['id']: 0 for g in gt_list}  # gt_event id → 命中次数

        for merged in merged_list:
            vid = merged['video_id']
            etype = merged['event_type']
            ts_start = merged.get('ts_start') or merged.get('start_sec')
            ts_end = merged.get('ts_end') or merged.get('end_sec')

            is_fp = True
            matched_gt_id = None

            for g in gt_list:
                if g['video_id'] == vid and g['event_type'] == etype:
                    # 评测容差 ±5 秒
                    tolerance = 5
                    g_start = g['start_sec'] - tolerance
                    g_end = g['end_sec'] + tolerance
                    # 告警时间窗口与 GT 事件有重叠
                    overlaps = (
                        ts_start is not None and g_start <= ts_start <= g_end
                    ) or (
                        ts_end is not None and g_start <= ts_end <= g_end
                    ) or (
                        ts_start is not None and ts_end is not None
                        and ts_start <= g_start and ts_end >= g_end
                    )
                    if overlaps:
                        is_fp = False
                        matched_gt_id = g['id']
                        gt_hit_counts[g['id']] = gt_hit_counts.get(g['id'], 0) + 1
                        break

            # 更新 eval_merged_events
            cur.execute('''
                UPDATE eval_merged_events
                SET is_false_positive = ?, matched_gt_event_id = ?
                WHERE id = ?
            ''', (1 if is_fp else 0, matched_gt_id, merged['id']))

            # 写入 eval_results（一条告警组对应一条结果）
            image_ids = json.loads(merged.get('image_ids') or '[]')
            rep_img_id = merged.get('representative_image_id') or (image_ids[0] if image_ids else None)
            cur.execute('''
                INSERT INTO eval_results
                (task_id, merged_event_id, alert_image_id, is_false_positive, is_missed)
                VALUES (?, ?, ?, ?, ?)
            ''', (task_id, merged['id'], rep_img_id, 1 if is_fp else 0, False))
            conn.commit()

            with _eval_lock:
                _eval_progress[task_id]['done'] += 1

        # ── 更新每个 GT 事件的 actual_count ───────────────────────────────────
        for g in gt_list:
            actual = gt_hit_counts.get(g['id'], 0)

            cur.execute('''
                UPDATE eval_gt_events
                SET actual_count = ?
                WHERE id = ?
            ''', (actual, g['id']))
            conn.commit()

            with _eval_lock:
                _eval_progress[task_id]['done'] += 1

        try:
            cur.execute('UPDATE eval_tasks SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?', ('done', task_id))
        except Exception:
            # 兼容旧数据库（没有 updated_at 列）
            cur.execute('UPDATE eval_tasks SET status = ? WHERE id = ?', ('done', task_id))
        conn.commit()
        conn.close()
        with _eval_lock:
            _eval_progress[task_id]['running'] = False

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()

    cursor.execute('UPDATE eval_tasks SET status = ? WHERE id = ?', ('evaluating', task_id))
    db.commit()
    return jsonify({'success': True})


@bp.route('/api/tasks/<int:task_id>/status', methods=['GET'])
def eval_status(task_id):
    """查询评测进度"""
    with _eval_lock:
        prog = _eval_progress.get(task_id)
    if not prog:
        return jsonify({'error': '没有正在运行的评测'}), 404
    return jsonify({
        'total': prog['total'],
        'done': prog['done'],
        'running': prog['running'],
    })


@bp.route('/api/tasks/<int:task_id>/results', methods=['GET'])
def get_results(task_id):
    """获取评测结果（告警检测结果 + GT 事件得分）"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, name, notes, dataset_id, alert_eval_set_id, eval_set_id, merge_interval_sec, event_start_sec, event_end_sec, event_interval_sec, trigger_rate, min_event_duration_sec, status, created_at, finalized, accuracy, recall, avg_fp_per_hour, event_metrics, confirmed_at FROM eval_tasks WHERE id = ?', (task_id,))
    task = cursor.fetchone()
    if not task:
        return jsonify({'error': '任务不存在'}), 404

    # ── 告警检测结果 ───────────────────────────────────────────────────────────
    # 注意：同一个 video_id 可能在 videos 表中有多条记录，先子查询去重
    cursor.execute('''
        SELECT m.id, m.video_id, m.event_type, m.image_ids,
               m.representative_image_id, m.ts_start, m.ts_end,
               m.is_false_positive, m.matched_gt_event_id, m.manual_status,
               a.filename, a.file_path, v.id as video_db_id,
               o.timestamp_seconds
        FROM eval_merged_events m
        LEFT JOIN alert_images a ON a.id = m.representative_image_id
        LEFT JOIN (
            SELECT id, video_id FROM videos
            WHERE id IN (SELECT MAX(id) FROM videos GROUP BY video_id)
        ) v ON v.video_id = m.video_id
        LEFT JOIN (
            SELECT alert_image_id, timestamp_seconds
            FROM ocr_results
            WHERE id IN (
                SELECT MAX(id) FROM ocr_results GROUP BY alert_image_id
            )
        ) o ON o.alert_image_id = m.representative_image_id
        WHERE m.task_id = ?
        ORDER BY m.video_id, m.event_type, m.ts_start
    ''', (task_id,))
    alert_results = [dict(r) for r in cursor.fetchall()]

    for r in alert_results:
        r['image_ids'] = json.loads(r.get('image_ids') or '[]')
        r['effective_status'] = get_effective_status(r)

    # ── GT 事件得分 ────────────────────────────────────────────────────────────
    # 注意：同一个 video_id 可能在 videos 表中有多条记录，先子查询去重
    cursor.execute('''
        SELECT g.*, v.id as video_db_id
        FROM eval_gt_events g
        LEFT JOIN (
            SELECT id, video_id FROM videos
            WHERE id IN (SELECT MAX(id) FROM videos GROUP BY video_id)
        ) v ON v.video_id = g.video_id
        WHERE g.task_id = ?
        ORDER BY g.video_id, g.event_type, g.start_sec
    ''', (task_id,))
    gt_results = [dict(r) for r in cursor.fetchall()]

    # ── 统计评测视频集总时长 ────────────────────────────────────────────────────
    total_duration = 0
    cursor.execute('SELECT video_ids FROM eval_video_sets WHERE id = ?', (task['eval_set_id'],))
    eval_set = cursor.fetchone()
    if eval_set and eval_set['video_ids']:
        try:
            video_db_ids = json.loads(eval_set['video_ids'])
            if video_db_ids:
                placeholders = ','.join('?' for _ in video_db_ids)
                cursor.execute(f'SELECT SUM(duration) as total FROM videos WHERE id IN ({placeholders})', video_db_ids)
                row = cursor.fetchone()
                total_duration = row['total'] or 0
        except Exception:
            pass

    # ── 计算 GT 覆盖时长、覆盖率、理论告警数等统计 ──────────────────────────────
    gt_event_count = len(gt_results)
    gt_intervals = [(g['start_sec'], g['end_sec']) for g in gt_results
                    if g.get('start_sec') is not None and g.get('end_sec') is not None]
    merged_gt_intervals = _merge_intervals(gt_intervals)
    gt_coverage_seconds = sum(end - start for start, end in merged_gt_intervals)
    gt_coverage_rate = gt_coverage_seconds / total_duration if total_duration > 0 else 0.0
    expected_alert_total = sum(g.get('expected_count', 0) or 0 for g in gt_results)

    # 计算平均误检数/小时（排除被忽略的记录）
    fp_count = sum(1 for r in alert_results if get_effective_status(r) == 'false_positive')
    total_count = sum(1 for r in alert_results if get_effective_status(r) != 'ignored')
    total_duration_hours = total_duration / 3600 if total_duration else 0
    avg_fp_per_hour = round(fp_count / total_duration_hours, 2) if total_duration_hours else 0
    accuracy = (total_count - fp_count) / total_count if total_count > 0 else None

    # 计算整体召回率 = gt_count > 0 的事件类型召回率的算术平均
    # 与 finalize_task 保持一致：对每个事件类型，分别累加 min(actual, confirmed) 和 confirmed
    from collections import defaultdict
    gt_by_type = defaultdict(lambda: {'confirmed': 0, 'hit': 0})
    for g in gt_results:
        confirmed = g.get('confirmed_count') or 0
        actual = g.get('actual_count') or 0
        et = g.get('event_type')
        if not et:
            continue
        if confirmed == 0:
            if actual > 0:
                gt_by_type[et]['confirmed'] += 1
                gt_by_type[et]['hit'] += 1
        else:
            gt_by_type[et]['confirmed'] += confirmed
            gt_by_type[et]['hit'] += min(actual, confirmed)

    type_recalls = []
    for vals in gt_by_type.values():
        confirmed = vals['confirmed']
        hit = vals['hit']
        if confirmed > 0:
            type_recalls.append(hit / confirmed)
    recall = sum(type_recalls) / len(type_recalls) if type_recalls else None

    try:
        evaluated_at = task['updated_at']
    except Exception:
        evaluated_at = None

    return jsonify({
        'success': True,
        'alert_results': alert_results,
        'gt_results': gt_results,
        'evaluated_at': evaluated_at,
        'total_duration': round(total_duration, 2),
        'avg_fp_per_hour': avg_fp_per_hour,
        'accuracy': accuracy,
        'recall': recall,
        'gt_event_count': gt_event_count,
        'gt_coverage_seconds': round(gt_coverage_seconds, 2),
        'gt_coverage_rate': round(gt_coverage_rate, 4),
        'expected_alert_total': expected_alert_total,
    })


@bp.route('/api/tasks/<int:task_id>/merged-events/<int:merged_id>/status', methods=['PUT'])
def update_manual_status(task_id, merged_id):
    """更新合并告警的人工修正状态"""
    data = request.get_json() or {}
    manual_status = data.get('manual_status')
    if manual_status not in ('auto', 'correct', 'false_positive', 'ignored'):
        return jsonify({'error': '无效的状态值'}), 400

    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id FROM eval_tasks WHERE id = ?', (task_id,))
    if not cursor.fetchone():
        return jsonify({'error': '任务不存在'}), 404

    cursor.execute('''
        UPDATE eval_merged_events SET manual_status = ? WHERE id = ? AND task_id = ?
    ''', (manual_status, merged_id, task_id))
    db.commit()

    if cursor.rowcount == 0:
        return jsonify({'error': '记录不存在'}), 404

    return jsonify({'success': True, 'manual_status': manual_status})


@bp.route('/api/tasks/<int:task_id>/gt-events/<int:gt_id>', methods=['PUT'])
def update_gt_event_counts(task_id, gt_id):
    """更新 GT 事件的预期触发数和实际命中数"""
    data = request.get_json() or {}
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id FROM eval_tasks WHERE id = ?', (task_id,))
    if not cursor.fetchone():
        return jsonify({'error': '任务不存在'}), 404

    update_fields = []
    update_values = []
    if 'confirmed_count' in data:
        update_fields.append('confirmed_count = ?')
        update_values.append(int(data['confirmed_count']))
    if 'actual_count' in data:
        update_fields.append('actual_count = ?')
        update_values.append(int(data['actual_count']))

    if not update_fields:
        return jsonify({'error': '缺少要更新的字段（confirmed_count 或 actual_count）'}), 400

    update_values.append(gt_id)
    update_values.append(task_id)
    cursor.execute(
        f'UPDATE eval_gt_events SET {", ".join(update_fields)} WHERE id = ? AND task_id = ?',
        update_values
    )
    db.commit()
    if cursor.rowcount == 0:
        return jsonify({'error': '记录不存在'}), 404

    return jsonify({'success': True})


@bp.route('/api/tasks/<int:task_id>/check-updates', methods=['GET'])
def check_updates(task_id):
    """检查该任务涉及的视频标注是否有更新"""
    db = get_db()
    cursor = db.cursor()
    try:
        cursor.execute('SELECT updated_at FROM eval_tasks WHERE id = ?', (task_id,))
    except Exception:
        return jsonify({'has_updates': False})
    task = cursor.fetchone()
    if not task:
        return jsonify({'error': '任务不存在'}), 404

    evaluated_at = task['updated_at']
    if not evaluated_at:
        return jsonify({'has_updates': False})

    # 获取任务涉及的所有 video_id（TEXT）
    cursor.execute('''
        SELECT DISTINCT video_id FROM (
            SELECT video_id FROM eval_merged_events WHERE task_id = ?
            UNION
            SELECT video_id FROM eval_gt_events WHERE task_id = ?
        )
        WHERE video_id IS NOT NULL
    ''', (task_id, task_id))
    video_ids = [row[0] for row in cursor.fetchall()]
    if not video_ids:
        return jsonify({'has_updates': False})

    placeholders = ','.join('?' for _ in video_ids)

    try:
        # 检查 videos 表的更新时间
        cursor.execute(f'''
            SELECT MAX(updated_at) FROM videos WHERE video_id IN ({placeholders})
        ''', video_ids)
        video_max = cursor.fetchone()[0]

        # 检查 events 表的更新时间（通过 videos.video_id 关联）
        cursor.execute(f'''
            SELECT MAX(e.updated_at) FROM events e
            JOIN videos v ON v.id = e.video_db_id
            WHERE v.video_id IN ({placeholders})
        ''', video_ids)
        event_max = cursor.fetchone()[0]
    except Exception:
        return jsonify({'has_updates': False})

    def _to_dt(value):
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            return datetime.fromisoformat(value)
        return None

    evaluated_dt = _to_dt(evaluated_at)
    if not evaluated_dt:
        return jsonify({'has_updates': False})

    times = [evaluated_dt] + [_to_dt(v) for v in [video_max, event_max] if v is not None]
    times = [t for t in times if t is not None]
    if not times:
        return jsonify({'has_updates': False})

    max_updated = max(times)
    return jsonify({'has_updates': max_updated > evaluated_dt})


@bp.route('/api/tasks/<int:task_id>/finalize', methods=['POST'])
def finalize_task(task_id):
    """确认评测结果，计算并保存准确率/召回率，锁定任务"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, name, notes, dataset_id, alert_eval_set_id, eval_set_id, merge_interval_sec, event_start_sec, event_end_sec, event_interval_sec, trigger_rate, min_event_duration_sec, status, created_at, finalized, accuracy, recall, avg_fp_per_hour, event_metrics, confirmed_at FROM eval_tasks WHERE id = ?', (task_id,))
    task = cursor.fetchone()
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    if task['status'] != 'done':
        return jsonify({'error': '只有已完成的任务才能确认结果'}), 400

    # 计算整体准确率（尊重 manual_status）
    cursor.execute('''
        SELECT is_false_positive, manual_status
        FROM eval_merged_events WHERE task_id=?
    ''', (task_id,))
    total = 0
    correct = 0
    for row in cursor.fetchall():
        status = get_effective_status(row)
        if status == 'ignored':
            continue
        total += 1
        if status == 'correct':
            correct += 1
    accuracy = correct / total if total > 0 else None

    # 计算整体召回率
    cursor.execute('''
        SELECT confirmed_count, actual_count
        FROM eval_gt_events WHERE task_id=?
    ''', (task_id,))
    gt_events = cursor.fetchall()

    total_expected = 0
    total_actual = 0
    for ev in gt_events:
        confirmed = ev['confirmed_count'] or 0
        actual = ev['actual_count'] or 0
        if confirmed == 0:
            # 当confirmed_count为0时，如果有actual就按1算，否则忽略
            if actual > 0:
                total_expected += 1
                total_actual += min(actual, 1)
        else:
            total_expected += confirmed
            total_actual += min(actual, confirmed)

    recall = total_actual / total_expected if total_expected > 0 else None

    # 计算评测视频总时长
    total_duration = 0
    cursor.execute('SELECT video_ids FROM eval_video_sets WHERE id = ?', (task['eval_set_id'],))
    eval_set = cursor.fetchone()
    if eval_set and eval_set['video_ids']:
        try:
            video_db_ids = json.loads(eval_set['video_ids'])
            if video_db_ids:
                placeholders = ','.join('?' for _ in video_db_ids)
                cursor.execute(f'SELECT SUM(duration) as total FROM videos WHERE id IN ({placeholders})', video_db_ids)
                row = cursor.fetchone()
                total_duration = row['total'] or 0
        except Exception:
            pass
    total_duration_hours = total_duration / 3600 if total_duration else 0

    # 按事件类型计算指标（包含配置文件中所有事件类型）
    all_event_types = _get_all_event_types()
    # 如果配置文件为空， fallback 到数据库中已有的事件类型
    if not all_event_types:
        cursor.execute('''
            SELECT DISTINCT event_type FROM eval_merged_events WHERE task_id=?
            UNION
            SELECT DISTINCT event_type FROM eval_gt_events WHERE task_id=?
        ''', (task_id, task_id))
        all_event_types = [r['event_type'] for r in cursor.fetchall() if r['event_type']]

    event_metrics = []
    for etype in all_event_types:
        # 告警相关指标（尊重 manual_status）
        cursor.execute('''
            SELECT is_false_positive, manual_status
            FROM eval_merged_events WHERE task_id=? AND event_type=?
        ''', (task_id, etype))
        alert_count = 0
        correct_pred_count = 0
        fp_count = 0
        for row in cursor.fetchall():
            status = get_effective_status(row)
            if status == 'ignored':
                continue
            alert_count += 1
            if status == 'correct':
                correct_pred_count += 1
            elif status == 'false_positive':
                fp_count += 1

        # GT相关指标 - 按新逻辑计算
        cursor.execute('''
            SELECT confirmed_count, actual_count
            FROM eval_gt_events WHERE task_id=? AND event_type=?
        ''', (task_id, etype))
        gt_events = cursor.fetchall()

        gt_count = 0
        hit_count = 0
        missed_gt_count = 0

        for ev in gt_events:
            confirmed = ev['confirmed_count'] or 0
            actual = ev['actual_count'] or 0

            if confirmed == 0:
                # 当confirmed_count为0时
                if actual > 0:
                    # 有actual就按1算，算作命中
                    gt_count += 1
                    hit_count += min(actual, 1)
                else:
                    # 没有actual就忽略这个事件
                    pass
            else:
                gt_count += confirmed
                hit_count += min(actual, confirmed)
                if actual < confirmed:
                    missed_gt_count += 1

        # 计算精确率、召回率和平均误检数/小时
        precision = correct_pred_count / alert_count if alert_count > 0 else None
        event_recall = hit_count / gt_count if gt_count > 0 else None
        avg_fp_per_hour = round(fp_count / total_duration_hours, 2) if total_duration_hours else 0

        event_metrics.append({
            'event_type': etype,
            'alert_count': alert_count,
            'gt_count': gt_count,
            'correct_pred_count': correct_pred_count,
            'false_positive_count': fp_count,
            'hit_count': hit_count,
            'missed_gt_count': missed_gt_count,
            'precision': precision,
            'recall': event_recall,
            'avg_fp_per_hour': avg_fp_per_hour
        })

    # 整体召回率 = 所有 gt_count > 0 的事件类型的召回率的算术平均
    recalls_with_gt = [em['recall'] for em in event_metrics if em['recall'] is not None and em['gt_count'] > 0]
    recall = sum(recalls_with_gt) / len(recalls_with_gt) if recalls_with_gt else None

    # 整体平均误检数/小时 = 总误检数 / 总时长（小时）
    cursor.execute('''
        SELECT is_false_positive, manual_status
        FROM eval_merged_events WHERE task_id=?
    ''', (task_id,))
    fp_count = 0
    for row in cursor.fetchall():
        if get_effective_status(row) == 'false_positive':
            fp_count += 1
    avg_fp_per_hour = round(fp_count / total_duration_hours, 2) if total_duration_hours else 0

    # 保存事件级别指标到JSON字段（可以扩展数据库表，这里先用JSON存储）
    event_metrics_json = json.dumps(event_metrics, ensure_ascii=False)

    cursor.execute('''
        UPDATE eval_tasks SET finalized=1, accuracy=?, recall=?, avg_fp_per_hour=?, event_metrics=?, confirmed_at=CURRENT_TIMESTAMP WHERE id=?
    ''', (accuracy, recall, avg_fp_per_hour, event_metrics_json, task_id))
    db.commit()

    return jsonify({
        'success': True,
        'accuracy': accuracy,
        'recall': recall,
        'avg_fp_per_hour': avg_fp_per_hour,
        'event_metrics': event_metrics
    })


@bp.route('/api/tasks/<int:task_id>/unconfirm', methods=['POST'])
def unconfirm_task(task_id):
    """取消确认，允许重新执行评测"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, finalized, status FROM eval_tasks WHERE id = ?', (task_id,))
    task = cursor.fetchone()
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    if not task['finalized']:
        return jsonify({'error': '任务尚未确认'}), 400

    cursor.execute('''
        UPDATE eval_tasks
        SET finalized=0, accuracy=NULL, recall=NULL, avg_fp_per_hour=NULL, event_metrics=NULL
        WHERE id=?
    ''', (task_id,))
    db.commit()
    return jsonify({'success': True, 'message': '已取消确认，可重新执行评测'})


@bp.route('/api/tasks/<int:task_id>/event-metrics', methods=['GET'])
def get_event_metrics(task_id):
    """获取事件级别的详细指标"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, name, notes, dataset_id, alert_eval_set_id, eval_set_id, merge_interval_sec, event_start_sec, event_end_sec, event_interval_sec, trigger_rate, min_event_duration_sec, status, created_at, finalized, accuracy, recall, avg_fp_per_hour, event_metrics, confirmed_at FROM eval_tasks WHERE id = ?', (task_id,))
    task = cursor.fetchone()
    if not task:
        return jsonify({'error': '任务不存在'}), 404

    # 如果有保存的事件指标，直接返回
    if task['event_metrics']:
        try:
            event_metrics = json.loads(task['event_metrics'])
            return jsonify({'success': True, 'event_metrics': event_metrics})
        except Exception:
            pass

    # 否则实时计算
    # 先计算评测视频总时长
    total_duration = 0
    cursor.execute('SELECT video_ids FROM eval_video_sets WHERE id = ?', (task['eval_set_id'],))
    eval_set = cursor.fetchone()
    if eval_set and eval_set['video_ids']:
        try:
            video_db_ids = json.loads(eval_set['video_ids'])
            if video_db_ids:
                placeholders = ','.join('?' for _ in video_db_ids)
                cursor.execute(f'SELECT SUM(duration) as total FROM videos WHERE id IN ({placeholders})', video_db_ids)
                row = cursor.fetchone()
                total_duration = row['total'] or 0
        except Exception:
            pass
    total_duration_hours = total_duration / 3600 if total_duration else 0

    # 按事件类型计算指标（包含配置文件中所有事件类型）
    all_event_types = _get_all_event_types()
    if not all_event_types:
        cursor.execute('''
            SELECT DISTINCT event_type FROM eval_merged_events WHERE task_id=?
            UNION
            SELECT DISTINCT event_type FROM eval_gt_events WHERE task_id=?
        ''', (task_id, task_id))
        all_event_types = [r['event_type'] for r in cursor.fetchall() if r['event_type']]

    event_metrics = []
    for etype in all_event_types:
        # 告警相关指标（尊重 manual_status）
        cursor.execute('''
            SELECT is_false_positive, manual_status
            FROM eval_merged_events WHERE task_id=? AND event_type=?
        ''', (task_id, etype))
        alert_count = 0
        correct_pred_count = 0
        fp_count = 0
        for row in cursor.fetchall():
            status = get_effective_status(row)
            if status == 'ignored':
                continue
            alert_count += 1
            if status == 'correct':
                correct_pred_count += 1
            elif status == 'false_positive':
                fp_count += 1

        # GT相关指标 - 按新逻辑计算
        cursor.execute('''
            SELECT confirmed_count, actual_count
            FROM eval_gt_events WHERE task_id=? AND event_type=?
        ''', (task_id, etype))
        gt_events = cursor.fetchall()

        gt_count = 0
        hit_count = 0
        missed_gt_count = 0

        for ev in gt_events:
            confirmed = ev['confirmed_count'] or 0
            actual = ev['actual_count'] or 0

            if confirmed == 0:
                if actual > 0:
                    gt_count += 1
                    hit_count += min(actual, 1)
            else:
                gt_count += confirmed
                hit_count += min(actual, confirmed)
                if actual < confirmed:
                    missed_gt_count += 1

        precision = correct_pred_count / alert_count if alert_count > 0 else None
        event_recall = hit_count / gt_count if gt_count > 0 else None
        avg_fp_per_hour = round(fp_count / total_duration_hours, 2) if total_duration_hours else 0

        event_metrics.append({
            'event_type': etype,
            'alert_count': alert_count,
            'gt_count': gt_count,
            'correct_pred_count': correct_pred_count,
            'false_positive_count': fp_count,
            'hit_count': hit_count,
            'missed_gt_count': missed_gt_count,
            'precision': precision,
            'recall': event_recall,
            'avg_fp_per_hour': avg_fp_per_hour
        })

    # 计算整体指标
    total_alert_count = sum(em['alert_count'] for em in event_metrics)
    total_gt_count = sum(em['gt_count'] for em in event_metrics)
    total_correct_pred_count = sum(em['correct_pred_count'] for em in event_metrics)
    total_fp_count = sum(em['false_positive_count'] for em in event_metrics)
    total_hit_count = sum(em['hit_count'] for em in event_metrics)
    total_missed_gt_count = sum(em['missed_gt_count'] for em in event_metrics)

    overall_precision = total_correct_pred_count / total_alert_count if total_alert_count > 0 else None
    recalls_with_gt = [em['recall'] for em in event_metrics if em['recall'] is not None and em['gt_count'] > 0]
    overall_recall = sum(recalls_with_gt) / len(recalls_with_gt) if recalls_with_gt else None
    overall_avg_fp = round(total_fp_count / total_duration_hours, 2) if total_duration_hours else 0

    overall = {
        'accuracy': overall_precision,
        'recall': overall_recall,
        'avg_fp_per_hour': overall_avg_fp,
        'alert_count': total_alert_count,
        'gt_count': total_gt_count,
        'correct_pred_count': total_correct_pred_count,
        'false_positive_count': total_fp_count,
        'hit_count': total_hit_count,
        'missed_gt_count': total_missed_gt_count
    }

    return jsonify({'success': True, 'event_metrics': event_metrics, 'overall': overall})


@bp.route('/api/tasks/<int:task_id>/report-image', methods=['GET'])
def get_report_image(task_id):
    """生成并下载评测报告图片"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, name, notes, dataset_id, alert_eval_set_id, eval_set_id, merge_interval_sec, event_start_sec, event_end_sec, event_interval_sec, trigger_rate, min_event_duration_sec, status, created_at, finalized, accuracy, recall, avg_fp_per_hour, event_metrics, confirmed_at FROM eval_tasks WHERE id = ?', (task_id,))
    task = cursor.fetchone()
    if not task:
        return jsonify({'error': '任务不存在'}), 404

    task_dict = dict(task)
    event_metrics = []
    accuracy = None
    recall = None
    avg_fp_per_hour = None

    # 计算评测视频总时长（两边都需要）
    total_duration = 0
    cursor.execute('SELECT video_ids FROM eval_video_sets WHERE id = ?', (task_dict['eval_set_id'],))
    eval_set = cursor.fetchone()
    if eval_set and eval_set['video_ids']:
        try:
            video_db_ids = json.loads(eval_set['video_ids'])
            if video_db_ids:
                placeholders = ','.join('?' for _ in video_db_ids)
                cursor.execute(f'SELECT SUM(duration) as total FROM videos WHERE id IN ({placeholders})', video_db_ids)
                row = cursor.fetchone()
                total_duration = row['total'] or 0
        except Exception:
            pass
    total_duration_hours = total_duration / 3600 if total_duration else 0

    if task_dict.get('finalized') and task_dict.get('event_metrics'):
        # 已确认结果，直接读取保存的数据
        try:
            event_metrics = json.loads(task_dict['event_metrics'])
        except Exception:
            event_metrics = []
        accuracy = task_dict.get('accuracy')
        recall = task_dict.get('recall')
        avg_fp_per_hour = task_dict.get('avg_fp_per_hour')
        # 兼容旧数据：如果数据库没存 avg_fp_per_hour，实时计算
        if avg_fp_per_hour is None and total_duration_hours:
            cursor.execute('''
                SELECT is_false_positive, manual_status
                FROM eval_merged_events WHERE task_id=?
            ''', (task_id,))
            fp_count = sum(1 for row in cursor.fetchall() if get_effective_status(row) == 'false_positive')
            avg_fp_per_hour = round(fp_count / total_duration_hours, 2)
    else:
        # 未确认，实时计算
        res = get_event_metrics(task_id)
        if res.status_code != 200:
            return res
        data = res.get_json()
        event_metrics = data.get('event_metrics', [])

        # 精确率
        cursor.execute('''
            SELECT is_false_positive, manual_status
            FROM eval_merged_events WHERE task_id=?
        ''', (task_id,))
        total = 0
        correct = 0
        for row in cursor.fetchall():
            status = get_effective_status(row)
            if status == 'ignored':
                continue
            total += 1
            if status == 'correct':
                correct += 1
        accuracy = correct / total if total > 0 else None

        # 召回率（gt_count>0 的事件类型召回率的算术平均）
        recalls_with_gt = [em['recall'] for em in event_metrics if em.get('recall') is not None and em.get('gt_count', 0) > 0]
        recall = sum(recalls_with_gt) / len(recalls_with_gt) if recalls_with_gt else None

        # 平均误检数 = 总误检数 / 总时长
        cursor.execute('''
            SELECT is_false_positive, manual_status
            FROM eval_merged_events WHERE task_id=?
        ''', (task_id,))
        fp_count = sum(1 for row in cursor.fetchall() if get_effective_status(row) == 'false_positive')
        avg_fp_per_hour = round(fp_count / total_duration_hours, 2) if total_duration_hours else 0

    # 计算 GT 统计（报告图片也需要）
    cursor.execute('SELECT start_sec, end_sec, expected_count FROM eval_gt_events WHERE task_id = ?', (task_id,))
    gt_rows = cursor.fetchall()
    gt_event_count = len(gt_rows)
    gt_intervals = [(r['start_sec'], r['end_sec']) for r in gt_rows
                    if r['start_sec'] is not None and r['end_sec'] is not None]
    merged_gt_intervals = _merge_intervals(gt_intervals)
    gt_coverage_seconds = sum(end - start for start, end in merged_gt_intervals)
    gt_coverage_rate = gt_coverage_seconds / total_duration if total_duration > 0 else 0.0
    expected_alert_total = sum((r['expected_count'] or 0) for r in gt_rows)

    # 对于报告图片，统一用总误检数/总时长重新计算平均误检数，避免旧数据不一致
    cursor.execute('''
        SELECT is_false_positive, manual_status
        FROM eval_merged_events WHERE task_id=?
    ''', (task_id,))
    fp_count = sum(1 for row in cursor.fetchall() if get_effective_status(row) == 'false_positive')
    avg_fp_per_hour = round(fp_count / total_duration_hours, 2) if total_duration_hours else 0

    buf = generate_report_image(
        task_dict, event_metrics, accuracy, recall, avg_fp_per_hour, total_duration_hours,
        total_duration, gt_event_count, gt_coverage_seconds, gt_coverage_rate, expected_alert_total
    )
    filename = f"report_{task_dict.get('name', 'task')}_{task_id}.png"
    response = send_file(buf, mimetype='image/png', as_attachment=True, download_name=filename)
    response.headers['Cache-Control'] = 'public, max-age=86400'
    return response


@bp.route('/api/gt-frames/<int:frame_id>/file', methods=['GET'])
def serve_gt_frame(frame_id):
    """提供GT帧图片文件"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT file_path FROM gt_frames WHERE id = ?', (frame_id,))
    frame = cursor.fetchone()
    if not frame:
        return 'Frame not found', 404

    file_path = Path(frame['file_path'])
    if not file_path.exists():
        return 'File not found', 404

    return send_file_with_cache(str(file_path))


# ── 测前分析 ──────────────────────────────────────────────────────────────────

def _merge_intervals(intervals):
    """合并重叠或相邻的时间区间，返回 [(start, end), ...]"""
    if not intervals:
        return []
    sorted_intervals = sorted(intervals, key=lambda x: x[0])
    merged = [list(sorted_intervals[0])]
    for start, end in sorted_intervals[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def _run_pre_analysis(eval_video_set_id, merge_interval_sec, event_interval_sec,
                      trigger_rate, min_event_duration_sec):
    """
    执行测前分析，返回结果字典。
    """
    db = get_db()
    cursor = db.cursor()

    # 获取评测视频集
    cursor.execute('SELECT id, name, notes, video_ids, created_at FROM eval_video_sets WHERE id = ?', (eval_video_set_id,))
    eval_set = cursor.fetchone()
    if not eval_set:
        return {'error': '评测视频集不存在'}

    video_db_ids = []
    if eval_set['video_ids']:
        try:
            video_db_ids = json.loads(eval_set['video_ids'])
        except Exception:
            video_db_ids = []

    if not video_db_ids:
        return {'error': '评测视频集为空'}

    # 获取所有视频信息
    placeholders = ','.join('?' for _ in video_db_ids)
    cursor.execute(f'''
        SELECT id, video_id, filename, duration FROM videos WHERE id IN ({placeholders})
    ''', video_db_ids)
    videos = {row['id']: dict(row) for row in cursor.fetchall()}

    # 从 DB 获取事件（按 video_db_id）
    cursor.execute(f'''
        SELECT video_db_id, event_type, start_seconds, end_seconds
        FROM events WHERE video_db_id IN ({placeholders})
    ''', video_db_ids)
    db_events_raw = [dict(r) for r in cursor.fetchall()]

    # DB 事件按 video_db_id + type 分组计数
    db_counts_by_video = {}  # video_db_id -> type -> count
    for ev in db_events_raw:
        vid = ev['video_db_id']
        et = ev['event_type']
        db_counts_by_video.setdefault(vid, {}).setdefault(et, 0)
        db_counts_by_video[vid][et] += 1

    # 读取 GT 文件并计算指标
    gt_dir = Path(current_app.config['GROUND_TRUTH_DIR'])
    all_event_types = _get_all_event_types()

    # 初始化类型统计
    event_type_stats = {}
    for et in all_event_types:
        event_type_stats[et] = {
            'total_count': 0,
            'total_duration': 0.0,
            'durations': [],
            'expected_alert_count': 0,
            'filtered_count': 0,
        }

    per_video_coverage = []
    missing_gt_videos = []
    total_video_duration = 0.0

    for vid in video_db_ids:
        video = videos.get(vid)
        if not video:
            continue

        video_id = video['video_id'] or ''
        duration = video['duration'] or 0.0
        total_video_duration += duration

        # 读取 GT 文件
        gt_file = gt_dir / f"{video_id}.json"
        gt_events = []
        if gt_file.exists():
            try:
                with open(gt_file, 'r', encoding='utf-8') as f:
                    gt_data = json.load(f)
                gt_events = gt_data.get('events', [])
            except Exception:
                gt_events = []

        if not gt_events:
            missing_gt_videos.append(video_id)
            per_video_coverage.append({
                'video_id': video_id,
                'video_db_id': vid,
                'duration': duration,
                'has_gt': False,
                'gt_coverage_seconds': 0.0,
                'coverage_rate': 0.0,
                'event_count': 0,
            })
            continue

        # 计算该视频的 GT 覆盖时长（合并所有事件区间，跨类型）
        all_intervals = [(e['start'], e['end']) for e in gt_events if 'start' in e and 'end' in e]
        merged_intervals = _merge_intervals(all_intervals)
        gt_coverage_seconds = sum(end - start for start, end in merged_intervals)
        coverage_rate = gt_coverage_seconds / duration if duration > 0 else 0.0

        event_count = len(gt_events)
        per_video_coverage.append({
            'video_id': video_id,
            'video_db_id': vid,
            'duration': duration,
            'has_gt': True,
            'gt_coverage_seconds': round(gt_coverage_seconds, 2),
            'coverage_rate': round(coverage_rate, 4),
            'event_count': event_count,
        })

        # 统计每个类型
        for ev in gt_events:
            et = ev.get('type', '')
            if et not in event_type_stats:
                continue
            start = ev.get('start', 0)
            end = ev.get('end', 0)
            dur = end - start

            event_type_stats[et]['total_count'] += 1
            event_type_stats[et]['total_duration'] += dur
            event_type_stats[et]['durations'].append(dur)

            # 参数敏感性：是否被 min_event_duration_sec 过滤
            if dur < min_event_duration_sec:
                event_type_stats[et]['filtered_count'] += 1
            else:
                # 理论告警数预估
                expected = calc_expected_count(start, end, event_interval_sec, trigger_rate, min_event_duration_sec)
                event_type_stats[et]['expected_alert_count'] += expected

    # 计算持续时间分布和最终类型统计
    final_type_stats = {}
    for et, stats in event_type_stats.items():
        durations = stats['durations']
        if not durations:
            continue
        durations_sorted = sorted(durations)
        n = len(durations_sorted)
        median = durations_sorted[n // 2] if n % 2 == 1 else (durations_sorted[n // 2 - 1] + durations_sorted[n // 2]) / 2

        final_type_stats[et] = {
            'total_count': stats['total_count'],
            'total_duration': round(stats['total_duration'], 2),
            'min_duration': round(min(durations), 2),
            'max_duration': round(max(durations), 2),
            'avg_duration': round(sum(durations) / len(durations), 2),
            'median_duration': round(median, 2),
            'expected_alert_count': stats['expected_alert_count'],
            'filtered_count': stats['filtered_count'],
            'remaining_count': stats['total_count'] - stats['filtered_count'],
        }

    # 计算总计覆盖率
    total_gt_coverage = sum(v['gt_coverage_seconds'] for v in per_video_coverage)
    overall_coverage_rate = total_gt_coverage / total_video_duration if total_video_duration > 0 else 0.0

    # GT/DB 一致性对比
    gt_db_diff = {}
    inconsistent_videos = []
    for vid in video_db_ids:
        video = videos.get(vid)
        if not video:
            continue
        video_id = video['video_id'] or ''
        gt_file = gt_dir / f"{video_id}.json"
        gt_counts = {}
        if gt_file.exists():
            try:
                with open(gt_file, 'r', encoding='utf-8') as f:
                    gt_data = json.load(f)
                for ev in gt_data.get('events', []):
                    et = ev.get('type', '')
                    if et:
                        gt_counts[et] = gt_counts.get(et, 0) + 1
            except Exception:
                pass

        db_counts = db_counts_by_video.get(vid, {})
        has_inconsistency = False
        for et in all_event_types:
            gt_c = gt_counts.get(et, 0)
            db_c = db_counts.get(et, 0)
            if gt_c > 0 or db_c > 0:
                if et not in gt_db_diff:
                    gt_db_diff[et] = {'gt_file': 0, 'db': 0, 'match': True}
                gt_db_diff[et]['gt_file'] += gt_c
                gt_db_diff[et]['db'] += db_c
                if gt_c != db_c:
                    has_inconsistency = True

        if has_inconsistency:
            inconsistent_videos.append({
                'video_db_id': vid,
                'video_id': video_id,
            })

    for et in list(gt_db_diff.keys()):
        gt_db_diff[et]['match'] = gt_db_diff[et]['gt_file'] == gt_db_diff[et]['db']

    return {
        'event_type_stats': final_type_stats,
        'total_video_duration': round(total_video_duration, 2),
        'video_coverage': {
            'total_coverage_seconds': round(total_gt_coverage, 2),
            'overall_coverage_rate': round(overall_coverage_rate, 4),
        },
        'per_video_coverage': per_video_coverage,
        'gt_db_diff': gt_db_diff,
        'missing_gt_videos': missing_gt_videos,
        'inconsistent_videos': inconsistent_videos,
    }


@bp.route('/pre-analysis')
def pre_analysis_history_page():
    """测前分析历史记录列表页"""
    return render_template('pre_analysis_history.html')


@bp.route('/pre-analysis/<int:record_id>')
def pre_analysis_detail_page(record_id):
    """测前分析详情页"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, eval_video_set_id, merge_interval_sec, event_interval_sec, trigger_rate, min_event_duration_sec, result_json, created_at FROM pre_analysis_records WHERE id = ?', (record_id,))
    record = cursor.fetchone()
    if not record:
        return '分析记录不存在', 404
    return render_template('pre_analysis.html', record=dict(record))


@bp.route('/api/pre-analysis', methods=['POST'])
def create_pre_analysis():
    """执行测前分析并保存记录"""
    data = request.get_json() or {}
    eval_video_set_id = data.get('eval_video_set_id')
    if not eval_video_set_id:
        return jsonify({'error': '请选择评测视频集'}), 400

    merge_interval_sec = float(data.get('merge_interval_sec', 5.0))
    event_interval_sec = float(data.get('event_interval_sec', 10.0))
    trigger_rate = float(data.get('trigger_rate', 0.5))
    min_event_duration_sec = float(data.get('min_event_duration_sec', 0))

    result = _run_pre_analysis(
        eval_video_set_id, merge_interval_sec, event_interval_sec,
        trigger_rate, min_event_duration_sec
    )
    if 'error' in result:
        return jsonify({'error': result['error']}), 400

    db = get_db()
    cursor = db.cursor()
    cursor.execute('''
        INSERT INTO pre_analysis_records
        (eval_video_set_id, merge_interval_sec, event_interval_sec, trigger_rate, min_event_duration_sec, result_json)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (
        eval_video_set_id, merge_interval_sec, event_interval_sec,
        trigger_rate, min_event_duration_sec,
        json.dumps(result, ensure_ascii=False)
    ))
    db.commit()
    record_id = cursor.lastrowid

    return jsonify({'success': True, 'record_id': record_id, 'result': result})


@bp.route('/api/pre-analysis', methods=['GET'])
def list_pre_analysis():
    """列出所有测前分析历史记录"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('''
        SELECT p.*, e.name as eval_set_name
        FROM pre_analysis_records p
        LEFT JOIN eval_video_sets e ON e.id = p.eval_video_set_id
        ORDER BY p.created_at DESC
    ''')
    records = []
    for row in cursor.fetchall():
        r = dict(row)
        try:
            r['result'] = json.loads(r['result_json'])
        except Exception:
            r['result'] = {}
        del r['result_json']
        records.append(r)
    return jsonify({'records': records})


@bp.route('/api/pre-analysis/<int:record_id>', methods=['GET'])
def get_pre_analysis(record_id):
    """获取单个测前分析详情"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('''
        SELECT p.*, e.name as eval_set_name
        FROM pre_analysis_records p
        LEFT JOIN eval_video_sets e ON e.id = p.eval_video_set_id
        WHERE p.id = ?
    ''', (record_id,))
    row = cursor.fetchone()
    if not row:
        return jsonify({'error': '分析记录不存在'}), 404

    r = dict(row)
    try:
        r['result'] = json.loads(r['result_json'])
    except Exception:
        r['result'] = {}
    del r['result_json']
    return jsonify(r)


@bp.route('/api/pre-analysis/by-set/<int:set_id>', methods=['GET'])
def list_pre_analysis_by_set(set_id):
    """获取某个评测集的测前分析历史"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('''
        SELECT p.*, e.name as eval_set_name
        FROM pre_analysis_records p
        LEFT JOIN eval_video_sets e ON e.id = p.eval_video_set_id
        WHERE p.eval_video_set_id = ?
        ORDER BY p.created_at DESC
    ''', (set_id,))
    records = []
    for row in cursor.fetchall():
        r = dict(row)
        try:
            r['result'] = json.loads(r['result_json'])
        except Exception:
            r['result'] = {}
        del r['result_json']
        records.append(r)
    return jsonify({'records': records})


@bp.route('/api/pre-analysis/<int:record_id>', methods=['DELETE'])
def delete_pre_analysis(record_id):
    """删除测前分析记录"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id FROM pre_analysis_records WHERE id = ?', (record_id,))
    if not cursor.fetchone():
        return jsonify({'error': '记录不存在'}), 404
    cursor.execute('DELETE FROM pre_analysis_records WHERE id = ?', (record_id,))
    db.commit()
    return jsonify({'success': True, 'message': '记录已删除'})


@bp.route('/api/eval-sets/with-analysis-count', methods=['GET'])
def list_eval_sets_with_analysis_count():
    """获取所有评测视频集，附带分析次数"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('''
        SELECT e.*, COUNT(p.id) as analysis_count
        FROM eval_video_sets e
        LEFT JOIN pre_analysis_records p ON p.eval_video_set_id = e.id
        GROUP BY e.id
        ORDER BY e.created_at DESC
    ''')
    sets = []
    for row in cursor.fetchall():
        s = dict(row)
        if s.get('video_ids'):
            try:
                s['video_ids'] = json.loads(s['video_ids'])
            except Exception:
                s['video_ids'] = []
        else:
            s['video_ids'] = []
        s['video_count'] = len(s['video_ids'])
        s['analysis_count'] = row['analysis_count'] or 0
        sets.append(s)
    return jsonify({'sets': sets})


@bp.route('/api/sync-gt', methods=['POST'])
def sync_ground_truth():
    """同步 Ground Truth：JSON 文件与 DB 标注互相同步"""
    data = request.get_json() or {}
    video_db_id = data.get('video_db_id')
    direction = data.get('direction')  # 'db_to_gt' or 'gt_to_db'

    if not video_db_id:
        return jsonify({'error': '缺少视频ID'}), 400
    if direction not in ('db_to_gt', 'gt_to_db'):
        return jsonify({'error': '同步方向必须是 db_to_gt 或 gt_to_db'}), 400

    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, filename, original_path, video_id, file_size, duration, created_at, updated_at, video_id_confirmed FROM videos WHERE id = ?', (video_db_id,))
    video = cursor.fetchone()
    if not video:
        return jsonify({'error': '视频不存在'}), 404

    vid = video['video_id']
    if not vid:
        return jsonify({'error': '视频ID未设置'}), 400

    gt_dir = Path(current_app.config['GROUND_TRUTH_DIR'])
    gt_file = gt_dir / f"{vid}.json"

    if direction == 'db_to_gt':
        # 以 DB 标注为准，生成 JSON 文件
        # 先读取当前 DB 事件
        cursor.execute(
            'SELECT event_type, start_seconds, end_seconds FROM events WHERE video_db_id = ? ORDER BY start_seconds',
            (video_db_id,)
        )
        events = cursor.fetchall()

        gt_data = {
            'file': video['filename'],
            'id': vid,
            'events': [
                {'type': e['event_type'], 'start': e['start_seconds'], 'end': e['end_seconds']}
                for e in events
            ]
        }

        gt_dir.mkdir(parents=True, exist_ok=True)
        with open(str(gt_file), 'w', encoding='utf-8') as f:
            json.dump(gt_data, f, ensure_ascii=False, indent=2)

        return jsonify({
            'success': True,
            'message': f'已将 {len(events)} 条标注同步到 GT 文件',
            'event_count': len(events),
        })

    else:
        # 以 GT 文件为准，同步到 DB 标注
        if not gt_file.exists():
            return jsonify({'error': 'GT 文件不存在'}), 404

        try:
            with open(str(gt_file), 'r', encoding='utf-8') as f:
                gt_data = json.load(f)
        except Exception as e:
            return jsonify({'error': f'读取 GT 文件失败: {str(e)}'}), 500

        events = gt_data.get('events', [])

        # 删除旧的 DB 事件
        cursor.execute('DELETE FROM events WHERE video_db_id = ?', (video_db_id,))

        added = 0
        for event in events:
            cursor.execute(
                'INSERT INTO events (video_db_id, event_type, start_seconds, end_seconds, gt_frames_status) VALUES (?, ?, ?, ?, ?)',
                (video_db_id, event.get('type'), event.get('start'), event.get('end'), 'pending')
            )
            added += 1

        db.commit()
        return jsonify({
            'success': True,
            'message': f'已将 {added} 条事件从 GT 文件同步到标注',
            'added': added,
        })
