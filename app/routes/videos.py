"""视频相关路由"""
from flask import Blueprint, request, jsonify, render_template, current_app, send_file, after_this_request
from pathlib import Path
import re
import os
import json
import zipfile
import tempfile

from app.database import get_db
from app.services.watermark_service import add_watermark

bp = Blueprint('videos', __name__, url_prefix='/videos')

EVENT_TYPES = ['rat', 'smoke', 'use_phone', 'call_phone', 'chef', 'trash', 'mask', 'flame']


def allowed_file(filename, allowed_extensions):
    """检查文件扩展名"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed_extensions


def extract_video_id(filename):
    """从文件名提取视频ID"""
    match = re.match(r'(\d{2,3})', filename)
    if match:
        return match.group(1)
    return None


@bp.route('/')
def videos_page():
    """视频列表页面"""
    return render_template('videos.html')


@bp.route('/api', methods=['GET'])
def list_videos():
    """获取所有视频"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM videos ORDER BY created_at DESC')
    videos = cursor.fetchall()

    result = []
    for video in videos:
        video_dict = dict(video)
        cursor.execute('SELECT * FROM watermarked_videos WHERE original_video_id = ?', (video['id'],))
        watermarked = cursor.fetchall()
        video_dict['watermarked_videos'] = [dict(w) for w in watermarked]
        result.append(video_dict)

    return jsonify(result)


@bp.route('/api/search', methods=['GET'])
def search_videos():
    """搜索视频（按文件名、video_id或事件类型）"""
    q = request.args.get('q', '').strip()
    if not q:
        return list_videos()

    db = get_db()
    cursor = db.cursor()
    like = f'%{q}%'
    cursor.execute('''
        SELECT DISTINCT v.*
        FROM videos v
        LEFT JOIN events e ON e.video_db_id = v.id
        WHERE v.filename LIKE ?
           OR v.video_id LIKE ?
           OR e.event_type LIKE ?
        ORDER BY v.created_at DESC
    ''', (like, like, like))
    videos = cursor.fetchall()

    result = []
    for video in videos:
        video_dict = dict(video)
        cursor.execute('SELECT * FROM watermarked_videos WHERE original_video_id = ?', (video['id'],))
        watermarked = cursor.fetchall()
        video_dict['watermarked_videos'] = [dict(w) for w in watermarked]
        result.append(video_dict)

    return jsonify(result)


@bp.route('/api/upload', methods=['POST'])
def upload_video():
    """上传视频"""
    if 'video' not in request.files:
        return jsonify({'error': '没有上传文件'}), 400

    file = request.files['video']
    if file.filename == '':
        return jsonify({'error': '没有选择文件'}), 400

    if not allowed_file(file.filename, current_app.config['ALLOWED_VIDEO_EXTENSIONS']):
        return jsonify({'error': '不支持的文件格式'}), 400

    filename = file.filename

    # 防止重复上传相同文件名
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id FROM videos WHERE filename = ?', (filename,))
    if cursor.fetchone():
        return jsonify({'error': f'文件 "{filename}" 已存在，请先删除或重命名后再上传'}), 409

    save_path = Path(current_app.config['UPLOAD_VIDEOS']) / filename
    file.save(str(save_path))

    video_id = extract_video_id(filename)
    file_size = save_path.stat().st_size

    cursor.execute('''
        INSERT INTO videos (filename, original_path, video_id, file_size)
        VALUES (?, ?, ?, ?)
    ''', (filename, str(save_path), video_id, file_size))
    db.commit()

    video_db_id = cursor.lastrowid

    return jsonify({
        'success': True,
        'video': {
            'id': video_db_id,
            'filename': filename,
            'video_id': video_id
        }
    })


@bp.route('/api/<int:video_id>', methods=['GET'])
def get_video(video_id):
    """获取单个视频详情"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM videos WHERE id = ?', (video_id,))
    video = cursor.fetchone()

    if not video:
        return jsonify({'error': '视频不存在'}), 404

    video_dict = dict(video)
    cursor.execute('SELECT * FROM watermarked_videos WHERE original_video_id = ?', (video_id,))
    watermarked = cursor.fetchall()
    video_dict['watermarked_videos'] = [dict(w) for w in watermarked]

    return jsonify(video_dict)


@bp.route('/api/<int:video_id>/rename', methods=['PUT'])
def rename_video(video_id):
    """重命名视频"""
    data = request.get_json()
    new_filename = (data or {}).get('filename', '').strip()
    if not new_filename:
        return jsonify({'error': '文件名不能为空'}), 400

    if not allowed_file(new_filename, current_app.config['ALLOWED_VIDEO_EXTENSIONS']):
        return jsonify({'error': '不支持的文件格式'}), 400

    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM videos WHERE id = ?', (video_id,))
    video = cursor.fetchone()
    if not video:
        return jsonify({'error': '视频不存在'}), 404

    # 检查新文件名是否已被其他视频使用
    cursor.execute('SELECT id FROM videos WHERE filename = ? AND id != ?', (new_filename, video_id))
    if cursor.fetchone():
        return jsonify({'error': f'文件名 "{new_filename}" 已被其他视频使用'}), 409

    old_path = Path(video['original_path'])
    new_path = old_path.parent / new_filename

    try:
        os.rename(str(old_path), str(new_path))
    except OSError as e:
        return jsonify({'error': f'文件重命名失败: {e}'}), 500

    cursor.execute(
        'UPDATE videos SET filename = ?, original_path = ? WHERE id = ?',
        (new_filename, str(new_path), video_id)
    )
    db.commit()

    return jsonify({'success': True, 'filename': new_filename})


@bp.route('/api/<int:video_id>/download', methods=['GET'])
def download_video(video_id):
    """下载单个视频，type=original（默认）或 watermarked"""
    download_type = request.args.get('type', 'original')
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM videos WHERE id = ?', (video_id,))
    video = cursor.fetchone()
    if not video:
        return jsonify({'error': '视频不存在'}), 404

    if download_type == 'watermarked':
        cursor.execute('''
            SELECT output_path, filename FROM watermarked_videos
            WHERE original_video_id = ?
            ORDER BY created_at DESC LIMIT 1
        ''', (video_id,))
        row = cursor.fetchone()
        if not row:
            return jsonify({'error': '尚未生成水印视频'}), 404
        file_path = Path(row['output_path'])
        download_name = row['filename']
    else:
        file_path = Path(video['original_path'])
        download_name = video['filename']

    if not file_path.exists():
        return jsonify({'error': '文件不存在于磁盘'}), 404

    return send_file(str(file_path), as_attachment=True, download_name=download_name)


@bp.route('/api/download/batch', methods=['POST'])
def batch_download():
    """批量下载视频（打包为 ZIP），type=original 或 watermarked"""
    data = request.get_json() or {}
    ids = data.get('ids', [])
    download_type = data.get('type', 'original')

    if not ids:
        return jsonify({'error': '请选择要下载的视频'}), 400

    db = get_db()
    cursor = db.cursor()

    tmp_fd, tmp_path = tempfile.mkstemp(suffix='.zip')
    os.close(tmp_fd)

    try:
        added = 0
        skipped = []
        with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_STORED) as zf:
            for vid_id in ids:
                if download_type == 'watermarked':
                    cursor.execute('''
                        SELECT wv.output_path, wv.filename
                        FROM watermarked_videos wv
                        WHERE wv.original_video_id = ?
                        ORDER BY wv.created_at DESC LIMIT 1
                    ''', (vid_id,))
                    row = cursor.fetchone()
                    if not row:
                        cursor.execute('SELECT filename FROM videos WHERE id = ?', (vid_id,))
                        v = cursor.fetchone()
                        skipped.append(v['filename'] if v else str(vid_id))
                        continue
                    file_path = Path(row['output_path'])
                    filename = row['filename']
                else:
                    cursor.execute('SELECT * FROM videos WHERE id = ?', (vid_id,))
                    row = cursor.fetchone()
                    if not row:
                        continue
                    file_path = Path(row['original_path'])
                    filename = row['filename']

                if file_path.exists():
                    zf.write(str(file_path), filename)
                    added += 1
                else:
                    skipped.append(filename)

        if added == 0:
            os.unlink(tmp_path)
            return jsonify({'error': '没有可下载的文件，请检查是否已生成水印视频'}), 404

        @after_this_request
        def cleanup(response):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            return response

        type_label = '水印版' if download_type == 'watermarked' else '原始版'
        return send_file(
            tmp_path,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f'videos_{type_label}.zip'
        )

    except Exception as e:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return jsonify({'error': str(e)}), 500


@bp.route('/api/<int:video_id>/video-id', methods=['PUT'])
def set_video_id(video_id):
    """设置视频ID（必须为10位数字）"""
    data = request.get_json()
    new_vid = (data or {}).get('video_id', '').strip()
    if not new_vid:
        return jsonify({'error': 'video_id 不能为空'}), 400
    if not re.fullmatch(r'\d{10}', new_vid):
        return jsonify({'error': 'video_id 必须为恰好10位数字'}), 400

    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id FROM videos WHERE id = ?', (video_id,))
    if not cursor.fetchone():
        return jsonify({'error': '视频不存在'}), 404

    cursor.execute('UPDATE videos SET video_id = ? WHERE id = ?', (new_vid, video_id))
    db.commit()

    return jsonify({'success': True, 'video_id': new_vid})


@bp.route('/api/<int:video_id>/watermark', methods=['POST'])
def apply_watermark(video_id):
    """给视频添加水印（使用数据库中存储的video_id）"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM videos WHERE id = ?', (video_id,))
    video = cursor.fetchone()

    if not video:
        return jsonify({'error': '视频不存在'}), 404

    if not video['video_id']:
        return jsonify({'error': '请先设置视频ID后再添加水印'}), 400

    result = add_watermark(
        video['original_path'],
        current_app.config['OUTPUT_DIR'],
        video_id=video['video_id']
    )

    if not result['success']:
        return jsonify({'error': result.get('error', '水印添加失败')}), 500

    original_path = Path(video['original_path'])
    ext = original_path.suffix
    output_path = Path(current_app.config['OUTPUT_DIR']) / f"{video['video_id']}{ext}"

    if output_path.exists():
        filename = output_path.name
        file_size = output_path.stat().st_size

        cursor.execute('''
            INSERT INTO watermarked_videos
            (original_video_id, filename, output_path, file_size)
            VALUES (?, ?, ?, ?)
        ''', (video_id, filename, str(output_path), file_size))
        db.commit()

        return jsonify({
            'success': True,
            'watermarked_video': {
                'id': cursor.lastrowid,
                'filename': filename,
                'output_path': str(output_path)
            }
        })

    return jsonify({'success': True, 'message': '水印添加完成'})


# ── 事件标注 ─────────────────────────────────────────────────────────────────

@bp.route('/api/<int:video_id>/events', methods=['GET'])
def list_events(video_id):
    """获取视频的所有事件"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id FROM videos WHERE id = ?', (video_id,))
    if not cursor.fetchone():
        return jsonify({'error': '视频不存在'}), 404

    cursor.execute(
        'SELECT * FROM events WHERE video_db_id = ? ORDER BY start_seconds',
        (video_id,)
    )
    events = [dict(e) for e in cursor.fetchall()]
    return jsonify(events)


@bp.route('/api/<int:video_id>/events', methods=['POST'])
def add_event(video_id):
    """添加事件"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id FROM videos WHERE id = ?', (video_id,))
    if not cursor.fetchone():
        return jsonify({'error': '视频不存在'}), 404

    data = request.get_json() or {}
    event_type = data.get('event_type', '').strip()
    start = data.get('start_seconds')
    end = data.get('end_seconds')

    if not event_type:
        return jsonify({'error': '事件类型不能为空'}), 400
    if start is None or end is None:
        return jsonify({'error': '开始和结束时间不能为空'}), 400
    try:
        start = float(start)
        end = float(end)
    except (TypeError, ValueError):
        return jsonify({'error': '时间格式错误'}), 400
    if start >= end:
        return jsonify({'error': '开始时间必须小于结束时间'}), 400

    cursor.execute(
        'INSERT INTO events (video_db_id, event_type, start_seconds, end_seconds) VALUES (?, ?, ?, ?)',
        (video_id, event_type, start, end)
    )
    db.commit()
    event_id = cursor.lastrowid

    return jsonify({
        'success': True,
        'event': {'id': event_id, 'event_type': event_type, 'start_seconds': start, 'end_seconds': end}
    })


@bp.route('/api/<int:video_id>/events/<int:event_id>', methods=['DELETE'])
def delete_event(video_id, event_id):
    """删除事件"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id FROM events WHERE id = ? AND video_db_id = ?', (event_id, video_id))
    if not cursor.fetchone():
        return jsonify({'error': '事件不存在'}), 404

    cursor.execute('DELETE FROM events WHERE id = ?', (event_id,))
    db.commit()
    return jsonify({'success': True})


# ── Ground Truth JSON ─────────────────────────────────────────────────────────

@bp.route('/api/<int:video_id>/ground-truth/generate', methods=['POST'])
def generate_ground_truth(video_id):
    """根据事件标注生成 ground truth JSON 文件"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM videos WHERE id = ?', (video_id,))
    video = cursor.fetchone()
    if not video:
        return jsonify({'error': '视频不存在'}), 404

    vid = video['video_id']
    if not vid:
        return jsonify({'error': '请先设置视频ID'}), 400

    cursor.execute(
        'SELECT event_type, start_seconds, end_seconds FROM events WHERE video_db_id = ? ORDER BY start_seconds',
        (video_id,)
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

    gt_dir = Path(current_app.config['GROUND_TRUTH_DIR'])
    gt_dir.mkdir(parents=True, exist_ok=True)
    gt_path = gt_dir / f'{vid}.json'

    with open(str(gt_path), 'w', encoding='utf-8') as f:
        json.dump(gt_data, f, ensure_ascii=False, indent=2)

    return jsonify({'success': True, 'path': str(gt_path)})


@bp.route('/api/<int:video_id>/ground-truth', methods=['GET'])
def view_ground_truth(video_id):
    """查看视频对应的 ground truth JSON 内容"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM videos WHERE id = ?', (video_id,))
    video = cursor.fetchone()
    if not video:
        return jsonify({'error': '视频不存在'}), 404

    vid = video['video_id']
    if not vid:
        return jsonify({'error': '请先设置视频ID'}), 400

    gt_path = Path(current_app.config['GROUND_TRUTH_DIR']) / f'{vid}.json'
    if not gt_path.exists():
        return jsonify({'error': 'JSON文件不存在，请先生成'}), 404

    with open(str(gt_path), 'r', encoding='utf-8') as f:
        data = json.load(f)

    return jsonify(data)
