"""评测相关路由 - 基于告警数据集 + 评测视频集/GT帧"""
from flask import Blueprint, request, jsonify, render_template, current_app, send_file
from pathlib import Path
import json
import threading
import subprocess

from app.database import get_db, DATABASE_PATH

bp = Blueprint('evaluation', __name__, url_prefix='/evaluation')

# 评测执行进度（内存存储）
_eval_progress = {}
_eval_lock = threading.Lock()


def calc_expected_count(start_sec, end_sec, interval_sec, trigger_rate):
    """计算预期触发次数"""
    raw = (end_sec - start_sec - 1) / interval_sec * trigger_rate
    return max(1, round(raw))


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
    cursor.execute('SELECT * FROM eval_tasks WHERE id = ?', (task_id,))
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
    cursor.execute('SELECT * FROM eval_video_sets ORDER BY created_at DESC')
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
    cursor.execute('SELECT * FROM eval_tasks ORDER BY created_at DESC')
    tasks = [dict(t) for t in cursor.fetchall()]

    for t in tasks:
        if t.get('dataset_id'):
            cursor.execute('SELECT name FROM datasets WHERE id = ?', (t['dataset_id'],))
            d = cursor.fetchone()
            t['dataset_name'] = d['name'] if d else None
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
    if not dataset_id:
        return jsonify({'error': '请选择告警数据集'}), 400

    eval_set_id = data.get('eval_set_id')
    if not eval_set_id:
        return jsonify({'error': '请选择评测视频集'}), 400

    db = get_db()
    cursor = db.cursor()

    cursor.execute('SELECT id FROM datasets WHERE id = ?', (dataset_id,))
    if not cursor.fetchone():
        return jsonify({'error': '告警数据集不存在'}), 404

    cursor.execute('SELECT id FROM eval_video_sets WHERE id = ?', (eval_set_id,))
    if not cursor.fetchone():
        return jsonify({'error': '评测视频集不存在'}), 404

    cursor.execute('''
        INSERT INTO eval_tasks
        (name, notes, dataset_id, eval_set_id, merge_interval_sec, event_start_sec,
         event_end_sec, event_interval_sec, trigger_rate, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        name,
        data.get('notes', ''),
        dataset_id,
        eval_set_id,
        data.get('merge_interval_sec', 5.0),
        0,
        0,
        data.get('event_interval_sec', 10.0),
        data.get('trigger_rate', 0.5),
        'created',
    ))
    db.commit()
    task_id = cursor.lastrowid

    cursor.execute('SELECT * FROM eval_tasks WHERE id = ?', (task_id,))
    return jsonify({'success': True, 'task': dict(cursor.fetchone())})


@bp.route('/api/tasks/<int:task_id>', methods=['GET'])
def get_task(task_id):
    """获取任务详情"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM eval_tasks WHERE id = ?', (task_id,))
    task = cursor.fetchone()
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    return jsonify(dict(task))


def _analyze_merged_events(task_id):
    """
    分析并返回合并告警组（以告警图片为中心）和 GT 事件列表。

    返回格式:
    {
        "merged_alerts": [...],  # 合并后的告警组
        "gt_events": [...]       # GT 事件列表（带中间帧）
    }
    """
    db = get_db()
    cursor = db.cursor()

    cursor.execute('SELECT * FROM eval_tasks WHERE id = ?', (task_id,))
    task = cursor.fetchone()
    if not task:
        return None

    dataset_id = task['dataset_id']
    eval_set_id = task['eval_set_id']
    merge_interval = task['merge_interval_sec']
    ev_interval = task['event_interval_sec']
    trigger_rate = task['trigger_rate']

    # ── 获取评测视频集的 video db_id 列表 ─────────────────────────────────────
    cursor.execute('SELECT video_ids FROM eval_video_sets WHERE id = ?', (eval_set_id,))
    eval_set = cursor.fetchone()
    if not eval_set:
        return {'merged_alerts': [], 'gt_events': []}

    eval_video_db_ids = []
    if eval_set['video_ids']:
        try:
            eval_video_db_ids = json.loads(eval_set['video_ids'])
        except Exception:
            eval_video_db_ids = []

    if not eval_video_db_ids:
        return {'merged_alerts': [], 'gt_events': []}

    # ── 获取告警数据集中所有已 OCR 的图片 ─────────────────────────────────────
    cursor.execute('''
        SELECT a.id, a.filename, a.file_path, a.event_label, a.alert_type,
               o.video_id, o.timestamp_seconds
        FROM alert_images a
        LEFT JOIN (
            SELECT alert_image_id, video_id, timestamp_seconds
            FROM ocr_results
            WHERE id IN (
                SELECT MAX(id) FROM ocr_results GROUP BY alert_image_id
            )
        ) o ON o.alert_image_id = a.id
        WHERE a.dataset_id = ?
          AND o.video_id IS NOT NULL
          AND o.timestamp_seconds IS NOT NULL
    ''', (dataset_id,))
    alert_images = [dict(r) for r in cursor.fetchall()]

    # ── 按 (video_id, event_type) 分组 ────────────────────────────────────────
    groups = {}  # key: (video_id, event_type) → list of images
    for img in alert_images:
        vid = img.get('video_id')
        etype = img.get('event_label') or img.get('alert_type')
        if not vid or not etype:
            continue
        key = (vid, etype)
        groups.setdefault(key, []).append(img)

    # ── 在每组内按时间戳合并 ───────────────────────────────────────────────────
    merged_alerts = []
    for (vid, etype), imgs in groups.items():
        imgs_sorted = sorted(imgs, key=lambda x: x['timestamp_seconds'])

        current_group = [imgs_sorted[0]]
        for img in imgs_sorted[1:]:
            prev_ts = current_group[-1]['timestamp_seconds']
            cur_ts = img['timestamp_seconds']
            if cur_ts - prev_ts <= merge_interval:
                current_group.append(img)
            else:
                # emit current group
                _emit_group(merged_alerts, vid, etype, current_group)
                current_group = [img]
        _emit_group(merged_alerts, vid, etype, current_group)

    # ── 获取评测视频集中所有 GT 事件 ───────────────────────────────────────────
    placeholders = ','.join('?' for _ in eval_video_db_ids)
    cursor.execute(f'''
        SELECT e.*, v.video_id
        FROM events e
        JOIN videos v ON v.id = e.video_db_id
        WHERE e.video_db_id IN ({placeholders})
        ORDER BY v.video_id, e.event_type, e.start_seconds
    ''', eval_video_db_ids)
    gt_events_raw = [dict(r) for r in cursor.fetchall()]

    # ── 为每个 GT 事件找中间帧 ─────────────────────────────────────────────────
    gt_events = []
    for ev in gt_events_raw:
        vid = ev.get('video_id')
        etype = ev.get('event_type')
        start_sec = ev.get('start_seconds', 0)
        end_sec = ev.get('end_seconds', 0)
        mid_ts = (start_sec + end_sec) / 2

        expected = calc_expected_count(start_sec, end_sec, ev_interval, trigger_rate)

        # 找中间帧
        mid_frame_id = None
        mid_frame_path = None
        cursor.execute('''
            SELECT id, file_path FROM gt_frames
            WHERE event_id = ?
            ORDER BY ABS(timestamp_sec - ?) LIMIT 1
        ''', (ev.get('id'), mid_ts))
        frame_row = cursor.fetchone()
        if frame_row:
            mid_frame_id = frame_row['id']
            mid_frame_path = frame_row['file_path']

        gt_events.append({
            'gt_event_id': ev.get('id'),
            'video_id': vid,
            'event_type': etype,
            'start_sec': start_sec,
            'end_sec': end_sec,
            'expected_count': expected,
            'confirmed_count': expected,
            'mid_frame_id': mid_frame_id,
            'mid_frame_path': mid_frame_path,
        })

    return {'merged_alerts': merged_alerts, 'gt_events': gt_events}


def _emit_group(merged_alerts, vid, etype, group):
    """将一组告警图片合并为一条记录"""
    image_ids = [img['id'] for img in group]
    rep_idx = len(image_ids) // 2
    representative_image_id = image_ids[rep_idx]
    ts_start = group[0]['timestamp_seconds']
    ts_end = group[-1]['timestamp_seconds']
    merged_alerts.append({
        'video_id': vid,
        'event_type': etype,
        'image_ids': image_ids,
        'representative_image_id': representative_image_id,
        'ts_start': ts_start,
        'ts_end': ts_end,
    })


@bp.route('/api/tasks/<int:task_id>/analyze', methods=['POST'])
def analyze_task(task_id):
    """分析可合并事件"""
    try:
        result = _analyze_merged_events(task_id)
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
    cursor.execute('SELECT id FROM eval_tasks WHERE id = ?', (task_id,))
    if not cursor.fetchone():
        return jsonify({'error': '任务不存在'}), 404

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
    cursor.execute('SELECT * FROM eval_tasks WHERE id = ?', (task_id,))
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
        cur.execute('SELECT * FROM eval_merged_events WHERE task_id = ?', (task_id,))
        merged_list = [dict(m) for m in cur.fetchall()]

        cur.execute('SELECT * FROM eval_gt_events WHERE task_id = ?', (task_id,))
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
                    g_start = g['start_sec']
                    g_end = g['end_sec']
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
    cursor.execute('SELECT * FROM eval_tasks WHERE id = ?', (task_id,))
    task = cursor.fetchone()
    if not task:
        return jsonify({'error': '任务不存在'}), 404

    # ── 告警检测结果 ───────────────────────────────────────────────────────────
    cursor.execute('''
        SELECT m.id, m.video_id, m.event_type, m.image_ids,
               m.representative_image_id, m.ts_start, m.ts_end,
               m.is_false_positive, m.matched_gt_event_id,
               a.filename, a.file_path
        FROM eval_merged_events m
        LEFT JOIN alert_images a ON a.id = m.representative_image_id
        WHERE m.task_id = ?
        ORDER BY m.video_id, m.event_type, m.ts_start
    ''', (task_id,))
    alert_results = [dict(r) for r in cursor.fetchall()]

    for r in alert_results:
        r['image_ids'] = json.loads(r.get('image_ids') or '[]')

    # ── GT 事件得分 ────────────────────────────────────────────────────────────
    cursor.execute('''
        SELECT * FROM eval_gt_events
        WHERE task_id = ?
        ORDER BY video_id, event_type, start_sec
    ''', (task_id,))
    gt_results = [dict(r) for r in cursor.fetchall()]

    return jsonify({
        'success': True,
        'alert_results': alert_results,
        'gt_results': gt_results,
    })


@bp.route('/api/tasks/<int:task_id>/finalize', methods=['POST'])
def finalize_task(task_id):
    """确认评测结果，计算并保存准确率/召回率，锁定任务"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM eval_tasks WHERE id = ?', (task_id,))
    task = cursor.fetchone()
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    if task['status'] != 'done':
        return jsonify({'error': '只有已完成的任务才能确认结果'}), 400

    # 计算整体准确率
    cursor.execute('''
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN is_false_positive=0 THEN 1 ELSE 0 END) AS correct
        FROM eval_merged_events WHERE task_id=?
    ''', (task_id,))
    row = cursor.fetchone()
    total = row['total'] or 0
    correct = row['correct'] or 0
    accuracy = correct / total if total > 0 else None

    # 计算整体召回率
    cursor.execute('''
        SELECT SUM(confirmed_count) AS expected, SUM(actual_count) AS actual
        FROM eval_gt_events WHERE task_id=?
    ''', (task_id,))
    row = cursor.fetchone()
    expected = row['expected'] or 0
    actual = row['actual'] or 0
    recall = actual / expected if expected > 0 else None

    # 按事件类型计算指标
    cursor.execute('''
        SELECT DISTINCT event_type FROM eval_merged_events WHERE task_id=?
        UNION
        SELECT DISTINCT event_type FROM eval_gt_events WHERE task_id=?
    ''', (task_id, task_id))
    event_types = [r['event_type'] for r in cursor.fetchall() if r['event_type']]

    event_metrics = []
    for etype in event_types:
        # 告警相关指标
        cursor.execute('''
            SELECT COUNT(*) AS alert_count,
                   SUM(CASE WHEN is_false_positive=0 THEN 1 ELSE 0 END) AS correct_pred_count
            FROM eval_merged_events WHERE task_id=? AND event_type=?
        ''', (task_id, etype))
        alert_row = cursor.fetchone()
        alert_count = alert_row['alert_count'] or 0
        correct_pred_count = alert_row['correct_pred_count'] or 0

        # GT相关指标
        cursor.execute('''
            SELECT SUM(confirmed_count) AS gt_count,
                   SUM(actual_count) AS hit_count
            FROM eval_gt_events WHERE task_id=? AND event_type=?
        ''', (task_id, etype))
        gt_row = cursor.fetchone()
        gt_count = gt_row['gt_count'] or 0
        hit_count = gt_row['hit_count'] or 0

        # 计算精确率和召回率
        precision = correct_pred_count / alert_count if alert_count > 0 else None
        event_recall = hit_count / gt_count if gt_count > 0 else None

        # 漏检的GT事件数（actual_count < confirmed_count）
        cursor.execute('''
            SELECT COUNT(*) AS missed_gt_count
            FROM eval_gt_events
            WHERE task_id=? AND event_type=? AND actual_count < confirmed_count
        ''', (task_id, etype))
        missed_gt_count = cursor.fetchone()['missed_gt_count'] or 0

        event_metrics.append({
            'event_type': etype,
            'alert_count': alert_count,
            'gt_count': gt_count,
            'correct_pred_count': correct_pred_count,
            'hit_count': hit_count,
            'missed_gt_count': missed_gt_count,
            'precision': precision,
            'recall': event_recall
        })

    # 保存事件级别指标到JSON字段（可以扩展数据库表，这里先用JSON存储）
    event_metrics_json = json.dumps(event_metrics, ensure_ascii=False)

    cursor.execute('''
        UPDATE eval_tasks SET finalized=1, accuracy=?, recall=?, event_metrics=? WHERE id=?
    ''', (accuracy, recall, event_metrics_json, task_id))
    db.commit()

    return jsonify({
        'success': True,
        'accuracy': accuracy,
        'recall': recall,
        'event_metrics': event_metrics
    })


@bp.route('/api/tasks/<int:task_id>/event-metrics', methods=['GET'])
def get_event_metrics(task_id):
    """获取事件级别的详细指标"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM eval_tasks WHERE id = ?', (task_id,))
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
    cursor.execute('''
        SELECT DISTINCT event_type FROM eval_merged_events WHERE task_id=?
        UNION
        SELECT DISTINCT event_type FROM eval_gt_events WHERE task_id=?
    ''', (task_id, task_id))
    event_types = [r['event_type'] for r in cursor.fetchall() if r['event_type']]

    event_metrics = []
    for etype in event_types:
        # 告警相关指标
        cursor.execute('''
            SELECT COUNT(*) AS alert_count,
                   SUM(CASE WHEN is_false_positive=0 THEN 1 ELSE 0 END) AS correct_pred_count
            FROM eval_merged_events WHERE task_id=? AND event_type=?
        ''', (task_id, etype))
        alert_row = cursor.fetchone()
        alert_count = alert_row['alert_count'] or 0
        correct_pred_count = alert_row['correct_pred_count'] or 0

        # GT相关指标
        cursor.execute('''
            SELECT SUM(confirmed_count) AS gt_count,
                   SUM(actual_count) AS hit_count
            FROM eval_gt_events WHERE task_id=? AND event_type=?
        ''', (task_id, etype))
        gt_row = cursor.fetchone()
        gt_count = gt_row['gt_count'] or 0
        hit_count = gt_row['hit_count'] or 0

        # 计算精确率和召回率
        precision = correct_pred_count / alert_count if alert_count > 0 else None
        event_recall = hit_count / gt_count if gt_count > 0 else None

        # 漏检的GT事件数
        cursor.execute('''
            SELECT COUNT(*) AS missed_gt_count
            FROM eval_gt_events
            WHERE task_id=? AND event_type=? AND actual_count < confirmed_count
        ''', (task_id, etype))
        missed_gt_count = cursor.fetchone()['missed_gt_count'] or 0

        event_metrics.append({
            'event_type': etype,
            'alert_count': alert_count,
            'gt_count': gt_count,
            'correct_pred_count': correct_pred_count,
            'hit_count': hit_count,
            'missed_gt_count': missed_gt_count,
            'precision': precision,
            'recall': event_recall
        })

    return jsonify({'success': True, 'event_metrics': event_metrics})


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

    return send_file(str(file_path))
