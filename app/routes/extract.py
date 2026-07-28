"""视频抽帧 blueprint：把水印视频抽帧成图片，用于图片测试模型。

命名规则：{video_id}_{时间秒}_{事件1+事件2}.jpg
- 时间保留1位小数（如 15.0s）
- 事件按字母序，用 + 连接；无事件标 normal
- 可设目标宽度等比例缩放、抽帧间隔、是否含 normal 帧
"""
import io
import json
import os
import subprocess
import threading
import time
import zipfile
from pathlib import Path

from flask import Blueprint, request, jsonify, current_app, send_file

from app.database import get_db, DATABASE_PATH

bp = Blueprint('extract', __name__, url_prefix='/extract')

# 后台任务状态：{task_id: {video_id, total, done, status, output_dir, frame_count, error}}
_extract_tasks = {}
_extract_lock = threading.Lock()


@bp.route('/api/start', methods=['POST'])
def start_extract():
    """提交批量抽帧任务（异步，立即返回 task_id）。

    body: {wm_ids: [int, ...], target_width, interval_sec, include_normal}
    多个视频合并为一个任务，统一进度（已完成视频数/总数），帧输出到同一目录打包成一个 zip。
    """
    data = request.get_json() or {}
    wm_ids = data.get('wm_ids') or ([data.get('wm_id')] if data.get('wm_id') else [])
    target_width = data.get('target_width') or None
    interval_sec = float(data.get('interval_sec') or 1.0)
    include_normal = bool(data.get('include_normal', False))

    if not wm_ids or not isinstance(wm_ids, list):
        return jsonify({'error': '缺少 wm_ids 列表'}), 400
    if interval_sec <= 0:
        return jsonify({'error': '抽帧间隔必须大于0'}), 400

    db = get_db()
    cur = db.cursor()
    # 取所有水印视频信息
    placeholders = ','.join('?' for _ in wm_ids)
    cur.execute(f'''
        SELECT w.id, w.output_path, w.original_video_id, v.video_id
        FROM watermarked_videos w
        JOIN videos v ON v.id = w.original_video_id
        WHERE w.id IN ({placeholders})
    ''', wm_ids)
    wms = [dict(w) for w in cur.fetchall()]
    if not wms:
        return jsonify({'error': '水印视频不存在'}), 404
    # 过滤掉无 video_id 或文件不存在的
    valid = []
    for w in wms:
        if not w['video_id']:
            continue
        if not w['output_path'] or not os.path.exists(w['output_path']):
            continue
        valid.append(w)
    if not valid:
        return jsonify({'error': '选中的视频均不可抽帧（未设video_id或文件不存在）'}), 400

    # 取每个视频的 GT 事件
    for w in valid:
        cur.execute(
            'SELECT event_type, start_seconds, end_seconds FROM events WHERE video_db_id = ? ORDER BY start_seconds',
            (w['original_video_id'],)
        )
        w['events'] = [dict(e) for e in cur.fetchall()]

    video_ids = [w['video_id'] for w in valid]
    video_id_str = ','.join(video_ids)
    output_dir = Path(current_app.config['EXTRACTED_FRAMES_DIR']) / f'batch_{int(time.time())}'

    # 创建一条批量任务记录
    cur.execute('''
        INSERT INTO extracted_frames_tasks
        (wm_ids, video_id, video_count, target_width, interval_sec, include_normal, status, output_dir)
        VALUES (?, ?, ?, ?, ?, ?, 'running', ?)
    ''', (json.dumps(wm_ids), video_id_str, len(valid), target_width, interval_sec,
          1 if include_normal else 0, str(output_dir)))
    db.commit()
    task_id = cur.lastrowid

    with _extract_lock:
        _extract_tasks[task_id] = {
            'video_id': video_id_str,
            'video_count': len(valid),
            'total': len(valid),      # 总视频数
            'done': 0,                # 已完成视频数
            'frame_count': 0,
            'status': 'running',
            'output_dir': str(output_dir),
            'error': None,
        }

    thread = threading.Thread(
        target=_do_extract_batch,
        args=(task_id, valid, target_width, interval_sec, include_normal, str(output_dir)),
        daemon=True,
    )
    thread.start()

    return jsonify({'success': True, 'task_id': task_id, 'video_count': len(valid)})


@bp.route('/api/<int:task_id>/status', methods=['GET'])
def extract_status(task_id):
    """查询抽帧进度。done/total = 已完成视频数/总视频数，frame_count = 累计帧数。"""
    with _extract_lock:
        t = _extract_tasks.get(task_id)
        if not t:
            db = get_db()
            cur = db.cursor()
            cur.execute('SELECT status, frame_count, output_dir, video_count FROM extracted_frames_tasks WHERE id = ?', (task_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({'error': '任务不存在'}), 404
            return jsonify({
                'success': True,
                'status': row['status'],
                'done': row['video_count'],
                'total': row['video_count'],
                'frame_count': row['frame_count'],
                'video_count': row['video_count'],
                'output_dir': row['output_dir'],
            })
        return jsonify({
            'success': True,
            'status': t['status'],
            'done': t['done'],
            'total': t['total'],
            'frame_count': t['frame_count'],
            'video_count': t.get('video_count', 1),
            'output_dir': t['output_dir'],
            'error': t['error'],
        })


@bp.route('/api/<int:task_id>/download', methods=['GET'])
def download_frames(task_id):
    """打包下载抽出的帧（zip）。批量任务的所有视频帧在一个 zip 里。"""
    db = get_db()
    cur = db.cursor()
    cur.execute('SELECT video_id, output_dir, frame_count FROM extracted_frames_tasks WHERE id = ?', (task_id,))
    row = cur.fetchone()
    if not row:
        return jsonify({'error': '任务不存在'}), 404
    output_dir = Path(row['output_dir'])
    if not output_dir.exists():
        return jsonify({'error': '帧目录不存在'}), 404

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(output_dir.iterdir()):
            if f.is_file():
                zf.write(f, f.name)
    buf.seek(0)
    name = row['video_id'].split(',')[0] if row['video_id'] else 'frames'
    return send_file(
        buf,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'{name}_frames.zip'
    )


@bp.route('/api/<int:task_id>', methods=['DELETE'])
def delete_extract(task_id):
    """删除抽帧任务及其帧文件。"""
    db = get_db()
    cur = db.cursor()
    cur.execute('SELECT video_id, output_dir FROM extracted_frames_tasks WHERE id = ?', (task_id,))
    row = cur.fetchone()
    if not row:
        return jsonify({'error': '任务不存在'}), 404
    output_dir = Path(row['output_dir'])
    if output_dir.exists():
        import shutil
        shutil.rmtree(output_dir, ignore_errors=True)
    cur.execute('DELETE FROM extracted_frames_tasks WHERE id = ?', (task_id,))
    db.commit()
    return jsonify({'success': True})


@bp.route('/api/tasks', methods=['GET'])
def list_tasks():
    """历史抽帧任务列表。"""
    db = get_db()
    cur = db.cursor()
    cur.execute('''
        SELECT id, video_id, video_count, target_width, interval_sec, include_normal,
               status, frame_count, created_at
        FROM extracted_frames_tasks
        ORDER BY created_at DESC
        LIMIT 50
    ''')
    return jsonify([dict(r) for r in cur.fetchall()])


# ── 抽帧核心 ──────────────────────────────────────────────────────────────────

def _get_video_duration(video_path):
    """用 ffprobe 取视频时长（秒）。"""
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1',
             str(video_path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(r.stdout.strip()) if r.stdout.strip() else 0
    except Exception:
        return 0


def _events_at(events, t):
    """返回时间点 t 处正在发生的事件类型列表（按字母序）。"""
    return sorted([
        e['event_type'] for e in events
        if (e['start_seconds'] is not None and e['end_seconds'] is not None
            and e['start_seconds'] <= t <= e['end_seconds'])
    ])


def _do_extract_batch(task_id, videos, target_width, interval_sec, include_normal, output_dir):
    """后台批量抽帧。videos=[{video_id, output_path, events}, ...]。

    进度 = 已完成视频数 / 总视频数；所有帧输出到同一 output_dir。
    """
    import sqlite3
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    scale_filter = ['-vf', f'scale={target_width}:-1'] if target_width and target_width > 0 else []
    total_videos = len(videos)
    done_videos = 0
    total_frames = 0

    for w in videos:
        video_path = w['output_path']
        video_id = w['video_id']
        events = w['events']

        duration = _get_video_duration(video_path)
        if duration <= 0:
            done_videos += 1
            with _extract_lock:
                _extract_tasks[task_id]['done'] = done_videos
            continue

        # 确定该视频的抽帧时间点
        time_points = []
        if include_normal:
            t = 0.0
            while t <= duration:
                time_points.append(round(t, 1))
                t += interval_sec
        else:
            seen = set()
            for e in events:
                s = e['start_seconds'] or 0
                en = e['end_seconds'] if e['end_seconds'] is not None else s
                t = s
                while t <= en:
                    tp = round(t, 1)
                    if tp not in seen:
                        seen.add(tp)
                        time_points.append(tp)
                    t += interval_sec
            time_points.sort()

        for t in time_points:
            evs = _events_at(events, t)
            name_part = '+'.join(evs) if evs else 'normal'
            out_path = output_dir / f'{video_id}_{t:.1f}s_{name_part}.jpg'
            try:
                subprocess.run(
                    ['ffmpeg', '-ss', str(t), '-i', str(video_path),
                     '-vframes', '1', '-q:v', '2', '-y', '-loglevel', 'error']
                    + scale_filter
                    + [str(out_path)],
                    timeout=30, check=True,
                )
                if out_path.exists():
                    total_frames += 1
                    with _extract_lock:
                        _extract_tasks[task_id]['frame_count'] = total_frames
            except Exception:
                pass

        done_videos += 1
        with _extract_lock:
            _extract_tasks[task_id]['done'] = done_videos

    # 完成：更新 DB
    conn = sqlite3.connect(str(DATABASE_PATH))
    try:
        cur = conn.cursor()
        cur.execute(
            'UPDATE extracted_frames_tasks SET status=?, frame_count=? WHERE id=?',
            ('done', total_frames, task_id)
        )
        conn.commit()
    finally:
        conn.close()

    with _extract_lock:
        _extract_tasks[task_id]['status'] = 'done'


def _fail_task(task_id, msg):
    """标记任务失败。"""
    import sqlite3
    with _extract_lock:
        if task_id in _extract_tasks:
            _extract_tasks[task_id]['status'] = 'error'
            _extract_tasks[task_id]['error'] = msg
    conn = sqlite3.connect(str(DATABASE_PATH))
    try:
        cur = conn.cursor()
        cur.execute('UPDATE extracted_frames_tasks SET status=?, error_message=? WHERE id=?',
                    ('error', msg, task_id))
        conn.commit()
    finally:
        conn.close()
