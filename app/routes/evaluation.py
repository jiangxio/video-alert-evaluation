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

bp = Blueprint('evaluation', __name__, url_prefix='/evaluation')

# 评测执行进度（内存存储）
_eval_progress = {}
_eval_lock = threading.Lock()


def calc_expected_count(start_sec, end_sec, interval_sec, trigger_rate, min_event_duration_sec=0):
    """计算预期触发次数"""
    if end_sec - start_sec < min_event_duration_sec:
        return 0
    raw = (end_sec - start_sec - 1) / interval_sec * trigger_rate
    return max(1, round(raw))


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


def _get_effective_status(row):
    """根据 manual_status 解析有效状态"""
    manual = row.get('manual_status') if isinstance(row, dict) else row['manual_status']
    if manual == 'correct':
        return 'correct'
    if manual == 'false_positive':
        return 'false_positive'
    if manual == 'ignored':
        return 'ignored'
    # auto 或未设置时，以 is_false_positive 字段为准
    is_fp = row.get('is_false_positive') if isinstance(row, dict) else row['is_false_positive']
    return 'false_positive' if is_fp else 'correct'


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
         event_end_sec, event_interval_sec, trigger_rate, min_event_duration_sec, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        data.get('min_event_duration_sec', 0),
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

    cursor.execute('SELECT * FROM eval_tasks WHERE id = ?', (task_id,))
    return jsonify({'success': True, 'task': dict(cursor.fetchone())})


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
    min_event_duration_sec = task['min_event_duration_sec'] if task['min_event_duration_sec'] is not None else 0

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

        expected = calc_expected_count(start_sec, end_sec, ev_interval, trigger_rate, min_event_duration_sec)

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

    # 按时间戳对合并告警组排序
    merged_alerts.sort(key=lambda x: x['ts_start'])
    return {'merged_alerts': merged_alerts, 'gt_events': gt_events}


def _emit_group(merged_alerts, vid, etype, group):
    """将一组告警图片合并/保留为一条记录（单张也保留）"""
    image_ids = [img['id'] for img in group]
    rep_idx = len(image_ids) // 2
    representative_image_id = image_ids[rep_idx]
    ts_start = group[0]['timestamp_seconds']
    ts_end = group[-1]['timestamp_seconds']
    # 保存所有图片的详细信息供前端显示
    all_images = []
    for img in group:
        all_images.append({
            'id': img['id'],
            'filename': img.get('filename'),
            'file_path': img.get('file_path'),
            'timestamp_seconds': img.get('timestamp_seconds')
        })
    merged_alerts.append({
        'video_id': vid,
        'event_type': etype,
        'image_ids': image_ids,
        'all_images': all_images,
        'representative_image_id': representative_image_id,
        'ts_start': ts_start,
        'ts_end': ts_end,
        'is_single': len(image_ids) == 1,
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
    cursor.execute('SELECT * FROM eval_tasks WHERE id = ?', (task_id,))
    task = cursor.fetchone()
    if not task:
        return jsonify({'error': '任务不存在'}), 404

    # ── 告警检测结果 ───────────────────────────────────────────────────────────
    cursor.execute('''
        SELECT m.id, m.video_id, m.event_type, m.image_ids,
               m.representative_image_id, m.ts_start, m.ts_end,
               m.is_false_positive, m.matched_gt_event_id, m.manual_status,
               a.filename, a.file_path, v.id as video_db_id,
               o.timestamp_seconds
        FROM eval_merged_events m
        LEFT JOIN alert_images a ON a.id = m.representative_image_id
        LEFT JOIN videos v ON v.video_id = m.video_id
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
        r['effective_status'] = _get_effective_status(r)

    # ── GT 事件得分 ────────────────────────────────────────────────────────────
    cursor.execute('''
        SELECT g.*, v.id as video_db_id
        FROM eval_gt_events g
        LEFT JOIN videos v ON v.video_id = g.video_id
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

    # 计算平均误检数/小时（排除被忽略的记录）
    fp_count = sum(1 for r in alert_results if _get_effective_status(r) == 'false_positive')
    total_count = sum(1 for r in alert_results if _get_effective_status(r) != 'ignored')
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
        'recall': recall
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
    cursor.execute('SELECT * FROM eval_tasks WHERE id = ?', (task_id,))
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
        status = _get_effective_status(row)
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
            status = _get_effective_status(row)
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
        if _get_effective_status(row) == 'false_positive':
            fp_count += 1
    avg_fp_per_hour = round(fp_count / total_duration_hours, 2) if total_duration_hours else 0

    # 保存事件级别指标到JSON字段（可以扩展数据库表，这里先用JSON存储）
    event_metrics_json = json.dumps(event_metrics, ensure_ascii=False)

    cursor.execute('''
        UPDATE eval_tasks SET finalized=1, accuracy=?, recall=?, avg_fp_per_hour=?, event_metrics=? WHERE id=?
    ''', (accuracy, recall, avg_fp_per_hour, event_metrics_json, task_id))
    db.commit()

    return jsonify({
        'success': True,
        'accuracy': accuracy,
        'recall': recall,
        'avg_fp_per_hour': avg_fp_per_hour,
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
            status = _get_effective_status(row)
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


def _get_font(size):
    """尝试加载系统中文字体"""
    font_paths = [
        '/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/wqy-microhei/wqy-microhei.ttc',
        '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
    ]
    from PIL import ImageFont
    for fp in font_paths:
        try:
            return ImageFont.truetype(fp, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _generate_report_image(task, event_metrics, accuracy, recall, avg_fp_per_hour, total_duration_hours=0):
    """用 Pillow 生成评测报告图片"""
    from PIL import Image, ImageDraw

    width = 1200
    margin = 40
    bg_color = (255, 255, 255)
    header_color = (52, 152, 219)  # #3498db
    text_dark = (44, 62, 80)
    text_gray = (100, 100, 100)
    line_color = (220, 220, 220)
    card_bg = (248, 249, 250)
    good_color = (39, 174, 96)
    mid_color = (243, 156, 18)
    bad_color = (231, 76, 60)

    font_title = _get_font(36)
    font_subtitle = _get_font(24)
    font_normal = _get_font(20)
    font_small = _get_font(18)
    font_table = _get_font(18)

    # 预估高度
    row_height = 46
    header_height = 160
    overall_height = 140
    section_gap = 30
    table_header = 50
    table_rows = (len(event_metrics) + 1) * row_height  # +1 合计行
    height = header_height + overall_height + section_gap + 60 + table_header + table_rows + margin * 2

    img = Image.new('RGB', (width, height), bg_color)
    draw = ImageDraw.Draw(img)

    y = 0

    # 顶部蓝条
    draw.rectangle([0, 0, width, header_height - 40], fill=header_color)
    draw.text((width // 2, 40), "评测报告", font=font_title, fill=(255, 255, 255), anchor="mt")

    # 任务名 + 评估时间
    y = header_height - 30
    task_name = task.get('name', '-')
    created_at = task.get('created_at', '-')
    if created_at:
        from datetime import datetime as _dt, timezone as _tz
        if isinstance(created_at, str):
            try:
                dt = _dt.strptime(created_at, '%Y-%m-%d %H:%M:%S')
                dt = dt.replace(tzinfo=_tz.utc).astimezone()
                eval_time = dt.strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                eval_time = created_at
        elif isinstance(created_at, _dt):
            if created_at.tzinfo is None:
                dt = created_at.replace(tzinfo=_tz.utc).astimezone()
            else:
                dt = created_at.astimezone()
            eval_time = dt.strftime('%Y-%m-%d %H:%M:%S')
        else:
            eval_time = str(created_at)
    else:
        eval_time = '-'
    draw.text((margin, y), f"任务：{task_name}", font=font_subtitle, fill=text_dark)
    y += 40
    draw.text((margin, y), f"评估时间：{eval_time}", font=font_normal, fill=text_gray)
    y += 50

    # 整体指标区域
    def draw_card(x, w, label, value, color):
        draw.rounded_rectangle([x, y, x + w, y + 100], radius=8, fill=card_bg)
        draw.text((x + w // 2, y + 20), label, font=font_small, fill=text_gray, anchor="mt")
        draw.text((x + w // 2, y + 60), value, font=font_subtitle, fill=color, anchor="mt")

    card_w = (width - margin * 2 - 40) // 3
    acc_str = f"{(accuracy * 100):.1f}%" if accuracy is not None else "N/A"
    rec_str = f"{(recall * 100):.1f}%" if recall is not None else "N/A"
    fp_str = f"{avg_fp_per_hour:.2f}" if avg_fp_per_hour is not None else "N/A"

    def fp_color(val):
        if val is None:
            return text_gray
        if val <= 1.0:
            return good_color
        if val <= 3.0:
            return mid_color
        return bad_color

    draw_card(margin, card_w, "整体精确率", acc_str, good_color if accuracy and accuracy >= 0.8 else mid_color if accuracy and accuracy >= 0.5 else bad_color)
    draw_card(margin + card_w + 20, card_w, "整体召回率", rec_str, good_color if recall and recall >= 0.8 else mid_color if recall and recall >= 0.5 else bad_color)
    draw_card(margin + card_w * 2 + 40, card_w, "平均误检数/小时", fp_str, fp_color(avg_fp_per_hour))
    y += 120

    # 分隔线
    y += section_gap
    draw.line([margin, y, width - margin, y], fill=line_color, width=2)
    y += section_gap

    # 详细指标标题
    draw.text((margin, y), "事件详细指标", font=font_subtitle, fill=text_dark)
    y += 50

    # 表格
    cols = ["事件类型", "告警数", "准确数", "精确率", "GT数", "命中数", "漏检数", "召回率", "误检数", "平均误检/h"]
    col_widths = [150, 90, 90, 100, 80, 80, 80, 100, 90, 110]
    total_table_w = sum(col_widths)
    start_x = margin + (width - margin * 2 - total_table_w) // 2

    def draw_table_row(row_y, cells, is_header=False, is_total=False, cell_colors=None):
        bg = (240, 240, 240) if is_header else (248, 249, 250) if is_total else bg_color
        if is_header or is_total:
            draw.rectangle([start_x, row_y, start_x + total_table_w, row_y + row_height], fill=bg)
        x = start_x
        for i, cell in enumerate(cells):
            text = str(cell)
            fw = col_widths[i]
            anchor = "lm" if i == 0 else "mm"
            tx = x + 10 if i == 0 else x + fw // 2
            font = font_table
            fill = cell_colors[i] if cell_colors and i < len(cell_colors) and cell_colors[i] else text_dark
            draw.text((tx, row_y + row_height // 2), text, font=font, fill=fill, anchor=anchor)
            x += fw
        # 横线
        draw.line([start_x, row_y + row_height, start_x + total_table_w, row_y + row_height], fill=line_color, width=1)

    # 表头
    draw_table_row(y, cols, is_header=True)
    y += row_height

    # 数据行
    for em in event_metrics:
        prec_val = em.get('precision')
        rec_val = em.get('recall')
        fp_val = em.get('avg_fp_per_hour', 0)
        prec = f"{(prec_val * 100):.1f}%" if prec_val is not None else "N/A"
        rec = f"{(rec_val * 100):.1f}%" if rec_val is not None else "N/A"
        fp_txt = f"{fp_val:.2f}"
        cells = [
            em.get('event_type', '-'),
            em.get('alert_count', 0),
            em.get('correct_pred_count', 0),
            prec,
            em.get('gt_count', 0),
            em.get('hit_count', 0),
            em.get('missed_gt_count', 0),
            rec,
            em.get('false_positive_count', 0),
            fp_txt,
        ]
        prec_color = good_color if prec_val is not None and prec_val >= 0.8 else mid_color if prec_val is not None and prec_val >= 0.5 else bad_color if prec_val is not None else None
        rec_color = good_color if rec_val is not None and rec_val >= 0.8 else mid_color if rec_val is not None and rec_val >= 0.5 else bad_color if rec_val is not None else None
        fp_color_val = fp_color(fp_val)
        cell_colors = [None, None, None, prec_color, None, None, None, rec_color, None, fp_color_val]
        draw_table_row(y, cells, cell_colors=cell_colors)
        y += row_height

    # 合计行
    total_alert = sum(em.get('alert_count', 0) for em in event_metrics)
    total_correct = sum(em.get('correct_pred_count', 0) for em in event_metrics)
    total_gt = sum(em.get('gt_count', 0) for em in event_metrics)
    total_hit = sum(em.get('hit_count', 0) for em in event_metrics)
    total_miss = sum(em.get('missed_gt_count', 0) for em in event_metrics)
    total_fp = sum(em.get('false_positive_count', 0) for em in event_metrics)
    overall_avg_fp = round(total_fp / total_duration_hours, 2) if total_duration_hours else 0
    oprec = f"{(accuracy * 100):.1f}%" if accuracy is not None else "N/A"
    orec = f"{(recall * 100):.1f}%" if recall is not None else "N/A"
    total_cells = [
        "合计/整体", total_alert, total_correct, oprec,
        total_gt, total_hit, total_miss, orec, total_fp, f"{overall_avg_fp:.2f}"
    ]
    prec_color = good_color if accuracy and accuracy >= 0.8 else mid_color if accuracy and accuracy >= 0.5 else bad_color
    rec_color = good_color if recall and recall >= 0.8 else mid_color if recall and recall >= 0.5 else bad_color
    fp_color_val = fp_color(avg_fp_per_hour)
    cell_colors = [None, None, None, prec_color, None, None, None, rec_color, None, fp_color_val]
    draw_table_row(y, total_cells, is_total=True, cell_colors=cell_colors)

    # 外边框
    draw.rectangle([start_x, y - (len(event_metrics) + 1) * row_height, start_x + total_table_w, y + row_height], outline=line_color, width=1)

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf


@bp.route('/api/tasks/<int:task_id>/report-image', methods=['GET'])
def get_report_image(task_id):
    """生成并下载评测报告图片"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM eval_tasks WHERE id = ?', (task_id,))
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
            fp_count = sum(1 for row in cursor.fetchall() if _get_effective_status(row) == 'false_positive')
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
            status = _get_effective_status(row)
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
        fp_count = sum(1 for row in cursor.fetchall() if _get_effective_status(row) == 'false_positive')
        avg_fp_per_hour = round(fp_count / total_duration_hours, 2) if total_duration_hours else 0

    # 对于报告图片，统一用总误检数/总时长重新计算平均误检数，避免旧数据不一致
    cursor.execute('''
        SELECT is_false_positive, manual_status
        FROM eval_merged_events WHERE task_id=?
    ''', (task_id,))
    fp_count = sum(1 for row in cursor.fetchall() if _get_effective_status(row) == 'false_positive')
    avg_fp_per_hour = round(fp_count / total_duration_hours, 2) if total_duration_hours else 0

    buf = _generate_report_image(task_dict, event_metrics, accuracy, recall, avg_fp_per_hour, total_duration_hours)
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
