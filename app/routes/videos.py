"""视频相关路由"""
from flask import Blueprint, request, jsonify, render_template, current_app, send_file, after_this_request
from pathlib import Path
import re
import os
import json
import zipfile
import tempfile
import subprocess
import threading
import time
import random

from app.database import get_db, DATABASE_PATH
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


def get_video_resolution(video_path):
    """获取视频分辨率，返回格式如 '1920x1080'"""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=width,height', '-of', 'csv=p=0', str(video_path)],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            dims = result.stdout.strip()
            if ',' in dims or 'x' in dims:
                return dims.replace(',', 'x')
    except Exception:
        pass
    return None


def get_video_duration(video_path):
    """获取视频时长（秒）"""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', str(video_path)],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return float(result.stdout.strip())
    except Exception:
        pass
    return None


def extract_thumbnail(video_path, output_path):
    """提取视频第一帧作为封面"""
    try:
        subprocess.run([
            'ffmpeg', '-i', str(video_path),
            '-ss', '0',
            '-vframes', '1',
            '-y',
            '-f', 'image2',
            '-loglevel', 'error',
            str(output_path)
        ], timeout=30, check=True)
        return output_path.exists()
    except Exception:
        return False


def generate_ground_truth_json(video_db_id):
    """根据当前事件重新生成JSON，返回生成的内容"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM videos WHERE id = ?', (video_db_id,))
    video = cursor.fetchone()
    if not video or not video['video_id']:
        return None

    cursor.execute(
        'SELECT event_type, start_seconds, end_seconds FROM events WHERE video_db_id = ? ORDER BY start_seconds',
        (video_db_id,)
    )
    events = cursor.fetchall()

    gt_data = {
        'file': video['filename'],
        'id': video['video_id'],
        'events': [
            {'type': e['event_type'], 'start': e['start_seconds'], 'end': e['end_seconds']}
            for e in events
        ]
    }

    gt_dir = Path(current_app.config['GROUND_TRUTH_DIR'])
    gt_dir.mkdir(parents=True, exist_ok=True)
    gt_path = gt_dir / f"{video['video_id']}.json"

    with open(str(gt_path), 'w', encoding='utf-8') as f:
        json.dump(gt_data, f, ensure_ascii=False, indent=2)

    return gt_data


# ── 页面路由 ──────────────────────────────────────────────────────────────────

@bp.route('/')
def videos_page():
    """视频管理页（网格展示打水印视频）"""
    return render_template('videos.html')


@bp.route('/upload/')
def video_upload_page():
    """视频上传页"""
    return render_template('video_upload.html')


@bp.route('/<int:video_id>/annotate/')
def annotate_page(video_id):
    """视频标注页"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM videos WHERE id = ?', (video_id,))
    video = cursor.fetchone()
    if not video:
        return '视频不存在', 404

    # 检查是否有水印版本
    cursor.execute('SELECT * FROM watermarked_videos WHERE original_video_id = ? ORDER BY created_at DESC LIMIT 1', (video_id,))
    watermarked = cursor.fetchone()
    if not watermarked:
        return '请先打水印后再标注', 400

    return render_template('video_annotate.html', video=dict(video))


# ── 视频列表 API ──────────────────────────────────────────────────────────────

@bp.route('/api/all', methods=['GET'])
def list_all_videos():
    """获取所有视频（用于上传页）"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM videos ORDER BY created_at DESC')
    videos = cursor.fetchall()

    result = []
    for video in videos:
        video_dict = dict(video)
        # 检查是否有水印版本
        cursor.execute('SELECT * FROM watermarked_videos WHERE original_video_id = ? ORDER BY created_at DESC LIMIT 1', (video['id'],))
        wm = cursor.fetchone()
        video_dict['has_watermark'] = wm is not None
        if wm:
            video_dict['watermarked'] = dict(wm)
        result.append(video_dict)

    return jsonify(result)


@bp.route('/api/search', methods=['GET'])
def search_videos():
    """搜索视频（用于上传页，按文件名、video_id）"""
    q = request.args.get('q', '').strip()
    if not q:
        return list_all_videos()

    db = get_db()
    cursor = db.cursor()
    like = f'%{q}%'
    cursor.execute('''
        SELECT * FROM videos
        WHERE filename LIKE ? OR video_id LIKE ?
        ORDER BY created_at DESC
    ''', (like, like))
    videos = cursor.fetchall()

    result = []
    for video in videos:
        video_dict = dict(video)
        cursor.execute('SELECT * FROM watermarked_videos WHERE original_video_id = ? ORDER BY created_at DESC LIMIT 1', (video['id'],))
        wm = cursor.fetchone()
        video_dict['has_watermark'] = wm is not None
        if wm:
            video_dict['watermarked'] = dict(wm)
        result.append(video_dict)

    return jsonify(result)


@bp.route('/api/watermarked', methods=['GET'])
def list_watermarked_videos():
    """获取所有打水印视频（用于管理页网格，带事件标签聚合），支持按评测集筛选"""
    db = get_db()
    cursor = db.cursor()

    eval_set_id = request.args.get('eval_set_id')
    filter_video_ids = None

    # 如果指定了评测集，获取该评测集包含的视频ID
    if eval_set_id:
        cursor.execute('SELECT video_ids FROM eval_video_sets WHERE id = ?', (eval_set_id,))
        row = cursor.fetchone()
        if row and row['video_ids']:
            try:
                filter_video_ids = json.loads(row['video_ids'])
            except Exception:
                filter_video_ids = []
        else:
            filter_video_ids = []

    # 查询打水印视频，关联原视频信息和事件类型
    # 只取每个原始视频的最新打水印版本
    cursor.execute('''
        SELECT
            w.id as wm_id, w.filename as wm_filename, w.output_path, w.file_size as wm_file_size,
            w.thumbnail_path, w.resolution, w.duration as wm_duration,
            v.id as video_id, v.video_id as vid, v.duration as orig_duration,
            GROUP_CONCAT(DISTINCT e.event_type) as event_types
        FROM watermarked_videos w
        JOIN videos v ON v.id = w.original_video_id
        LEFT JOIN events e ON e.video_db_id = v.id
        WHERE w.created_at = (
            SELECT MAX(w2.created_at)
            FROM watermarked_videos w2
            WHERE w2.original_video_id = w.original_video_id
        )
        GROUP BY w.id
        ORDER BY w.created_at DESC
    ''')
    rows = cursor.fetchall()

    result = []
    for row in rows:
        # 如果按评测集筛选，只包含该评测集的视频
        if filter_video_ids is not None and row['video_id'] not in filter_video_ids:
            continue

        item = {
            'id': row['wm_id'],
            'video_db_id': row['video_id'],
            'video_id': row['vid'],
            'filename': row['wm_filename'],
            'output_path': row['output_path'],
            'file_size': row['wm_file_size'],
            'thumbnail_path': row['thumbnail_path'],
            'resolution': row['resolution'],
            'duration': row['wm_duration'] or row['orig_duration'],
            'event_types': row['event_types'].split(',') if row['event_types'] else []
        }
        result.append(item)

    return jsonify(result)


# ── 视频上传、重命名、删除 ─────────────────────────────────────────────────────

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
    duration = get_video_duration(str(save_path))

    cursor.execute('''
        INSERT INTO videos (filename, original_path, video_id, file_size, duration)
        VALUES (?, ?, ?, ?, ?)
    ''', (filename, str(save_path), video_id, file_size, duration))
    db.commit()

    video_db_id = cursor.lastrowid

    return jsonify({
        'success': True,
        'video': {
            'id': video_db_id,
            'filename': filename,
            'video_id': video_id,
            'duration': duration
        }
    })


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


@bp.route('/api/<int:video_id>', methods=['DELETE'])
def delete_video(video_id):
    """删除视频（包括文件和数据库记录）"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM videos WHERE id = ?', (video_id,))
    video = cursor.fetchone()
    if not video:
        return jsonify({'error': '视频不存在'}), 404

    # 删除原始视频文件
    try:
        Path(video['original_path']).unlink(missing_ok=True)
    except Exception:
        pass

    # 删除打水印视频文件
    cursor.execute('SELECT output_path FROM watermarked_videos WHERE original_video_id = ?', (video_id,))
    for row in cursor.fetchall():
        try:
            Path(row['output_path']).unlink(missing_ok=True)
        except Exception:
            pass

    # 删除数据库记录（关联的events、gt_frames等通过外键级联删除）
    cursor.execute('DELETE FROM videos WHERE id = ?', (video_id,))
    db.commit()

    return jsonify({'success': True})


# ── 下载 ──────────────────────────────────────────────────────────────────────

@bp.route('/api/<int:video_id>/download', methods=['GET'])
def download_video(video_id):
    """下载或播放单个视频，type=original（默认）或 watermarked"""
    download_type = request.args.get('type', 'original')
    inline = request.args.get('inline', 'false').lower() == 'true'
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

    # inline=true 用于浏览器直接播放，inline=false（默认）用于下载
    if inline:
        return send_file(str(file_path), mimetype='video/mp4')
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


# ── 视频ID设置和水印 ───────────────────────────────────────────────────────────

@bp.route('/api/<int:video_id>/video-id', methods=['PUT'])
def set_video_id(video_id):
    """设置视频ID（必须为10位数字，不重复）"""
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

    # 检查是否重复
    cursor.execute('SELECT id FROM videos WHERE video_id = ? AND id != ?', (new_vid, video_id))
    if cursor.fetchone():
        return jsonify({'error': '该video_id已被其他视频使用，请使用不同的ID'}), 400

    cursor.execute('UPDATE videos SET video_id = ? WHERE id = ?', (new_vid, video_id))
    db.commit()

    return jsonify({'success': True, 'video_id': new_vid})


@bp.route('/api/<int:video_id>/confirm-video-id', methods=['POST'])
def confirm_video_id(video_id):
    """确认视频ID（确认后无法修改）"""
    data = request.get_json() or {}
    vid = data.get('video_id', '').strip()

    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, video_id, video_id_confirmed FROM videos WHERE id = ?', (video_id,))
    video = cursor.fetchone()
    if not video:
        return jsonify({'error': '视频不存在'}), 404

    # 检查video_id是否匹配
    if video['video_id'] != vid:
        return jsonify({'error': 'video_id不匹配'}), 400

    # 标记为已确认
    cursor.execute('UPDATE videos SET video_id_confirmed = 1 WHERE id = ?', (video_id,))
    db.commit()

    return jsonify({'success': True})


@bp.route('/api/<int:video_id>/watermark', methods=['POST'])
def apply_watermark(video_id):
    """给视频添加水印（异步处理）"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM videos WHERE id = ?', (video_id,))
    video = cursor.fetchone()

    if not video:
        return jsonify({'error': '视频不存在'}), 404

    if not video['video_id']:
        return jsonify({'error': '请先设置视频ID后再添加水印'}), 400

    if not video['video_id_confirmed']:
        return jsonify({'error': '视频ID尚未确认，请先确认视频ID后再添加水印'}), 400

    with watermark_lock:
        # 检查是否已有正在进行的打水印任务
        for tid, task in watermark_tasks.items():
            if task.get('video_id') == video_id and task.get('status') == 'processing':
                return jsonify({
                    'error': '该视频正在打水印中，请勿重复提交',
                    'task_id': tid
                }), 429

        # 创建任务ID
        task_id = f"watermark_{int(time.time())}_{random.randint(1000,9999)}"
        watermark_tasks[task_id] = {
            'video_id': video_id,
            'status': 'processing',
            'progress': 0,
            'error': None
        }

    # 启动后台线程
    project_root = current_app.config['PROJECT_ROOT']
    output_dir = current_app.config['OUTPUT_DIR']
    thread = threading.Thread(
        target=_do_watermark_async,
        args=(task_id, video_id, video['original_path'], video['video_id'], project_root, output_dir)
    )
    thread.start()

    return jsonify({'success': True, 'task_id': task_id})


@bp.route('/api/watermark-tasks/<task_id>/progress', methods=['GET'])
def get_watermark_progress(task_id):
    """获取打水印任务进度"""
    task = watermark_tasks.get(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404

    return jsonify({
        'status': task['status'],
        'progress': task['progress'],
        'error': task['error']
    })


def _do_watermark_async(task_id, video_id, video_path, video_id_str, project_root, output_dir):
    """后台异步执行打水印"""
    import sqlite3

    conn = sqlite3.connect(str(DATABASE_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    stop_flag = [False]

    def update_progress_smoothly(start_p, end_p, duration_sec):
        """平滑更新进度"""
        import time
        steps = 20
        step_duration = duration_sec / steps
        for i in range(steps + 1):
            if stop_flag[0]:
                break
            progress = start_p + int((end_p - start_p) * (i / steps))
            watermark_tasks[task_id]['progress'] = progress
            time.sleep(step_duration)

    try:
        # 阶段1: 准备 (0-15%)
        watermark_tasks[task_id]['progress'] = 5
        time.sleep(0.3)
        watermark_tasks[task_id]['progress'] = 10
        time.sleep(0.2)
        watermark_tasks[task_id]['progress'] = 15

        # 阶段2: 执行打水印 (15-70%) - 在后台持续更新进度直到完成
        stop_flag[0] = False
        def update_progress_while_processing():
            """持续更新进度直到FFmpeg完成"""
            import time
            current_progress = 15
            while not stop_flag[0]:
                # 缓慢增长到70%，然后保持
                if current_progress < 70:
                    current_progress += 0.5
                    watermark_tasks[task_id]['progress'] = int(current_progress)
                time.sleep(0.5)

        progress_thread = threading.Thread(
            target=update_progress_while_processing,
            daemon=True
        )
        progress_thread.start()

        # 调用水印服务
        result = add_watermark(video_path, output_dir, video_id=video_id_str)

        stop_flag[0] = True
        progress_thread.join(timeout=0.1)

        if not result['success']:
            watermark_tasks[task_id]['status'] = 'failed'
            watermark_tasks[task_id]['error'] = result.get('error', '水印添加失败')
            conn.close()
            return

        # 阶段3: 后处理 (70-95%)
        watermark_tasks[task_id]['progress'] = 75
        time.sleep(0.2)
        watermark_tasks[task_id]['progress'] = 80
        time.sleep(0.2)
        watermark_tasks[task_id]['progress'] = 85
        time.sleep(0.2)
        watermark_tasks[task_id]['progress'] = 90
        time.sleep(0.2)
        watermark_tasks[task_id]['progress'] = 95

        original_path = Path(video_path)
        ext = original_path.suffix
        output_path = Path(output_dir) / f"{video_id_str}{ext}"

        if output_path.exists():
            filename = output_path.name
            file_size = output_path.stat().st_size

            # 提取分辨率和时长
            resolution = get_video_resolution(output_path)
            duration = get_video_duration(output_path)

            cursor.execute('''
                INSERT INTO watermarked_videos
                (original_video_id, filename, output_path, file_size, resolution, duration)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (video_id, filename, str(output_path), file_size, resolution, duration))
            conn.commit()

            watermark_tasks[task_id]['progress'] = 100
            watermark_tasks[task_id]['status'] = 'done'
        else:
            watermark_tasks[task_id]['status'] = 'failed'
            watermark_tasks[task_id]['error'] = '输出文件不存在'

    except Exception as e:
        watermark_tasks[task_id]['status'] = 'failed'
        watermark_tasks[task_id]['error'] = str(e)
    finally:
        conn.close()


# ── 封面图 ────────────────────────────────────────────────────────────────────

@bp.route('/api/<int:wm_id>/thumbnail', methods=['GET'])
def get_thumbnail(wm_id):
    """获取视频封面图（不存在则实时生成）"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM watermarked_videos WHERE id = ?', (wm_id,))
    wm = cursor.fetchone()
    if not wm:
        return jsonify({'error': '水印视频不存在'}), 404

    thumbnail_path = wm['thumbnail_path']

    # 如果已有封面且文件存在，直接返回
    if thumbnail_path and Path(thumbnail_path).exists():
        return send_file(str(thumbnail_path))

    # 否则实时生成
    video_path = Path(wm['output_path'])
    if not video_path.exists():
        return jsonify({'error': '视频文件不存在'}), 404

    # 创建缩略图目录
    thumbs_dir = Path(current_app.config.get('THUMBNAILS_DIR', 'thumbnails'))
    thumbs_dir.mkdir(parents=True, exist_ok=True)

    thumb_path = thumbs_dir / f"{wm['filename']}_thumb.jpg"

    if extract_thumbnail(video_path, thumb_path):
        # 更新数据库
        cursor.execute('UPDATE watermarked_videos SET thumbnail_path = ? WHERE id = ?',
                       (str(thumb_path), wm_id))
        db.commit()
        return send_file(str(thumb_path))
    else:
        return jsonify({'error': '生成封面失败'}), 500


# ── 标注页数据 ─────────────────────────────────────────────────────────────────

@bp.route('/api/<int:video_id>/annotate/', methods=['GET'])
def get_annotate_data(video_id):
    """获取视频标注页所需的完整数据"""
    db = get_db()
    cursor = db.cursor()

    # 获取视频基本信息
    cursor.execute('SELECT * FROM videos WHERE id = ?', (video_id,))
    video = cursor.fetchone()
    if not video:
        return jsonify({'error': '视频不存在'}), 404

    # 获取水印视频信息
    cursor.execute('SELECT * FROM watermarked_videos WHERE original_video_id = ? ORDER BY created_at DESC LIMIT 1', (video_id,))
    wm = cursor.fetchone()
    if not wm:
        return jsonify({'error': '尚未生成水印视频'}), 404

    # 获取事件列表
    cursor.execute(
        'SELECT * FROM events WHERE video_db_id = ? ORDER BY start_seconds',
        (video_id,)
    )
    events = [dict(e) for e in cursor.fetchall()]

    # 获取该视频加入的所有评测集
    cursor.execute('SELECT * FROM eval_video_sets ORDER BY created_at DESC')
    all_sets = cursor.fetchall()
    belonging_sets = []
    for s in all_sets:
        video_ids = []
        if s['video_ids']:
            try:
                video_ids = json.loads(s['video_ids'])
            except Exception:
                pass
        if video_id in video_ids:
            belonging_sets.append({'id': s['id'], 'name': s['name']})

    # 获取JSON内容（如果存在）
    gt_data = None
    if video['video_id']:
        gt_path = Path(current_app.config['GROUND_TRUTH_DIR']) / f"{video['video_id']}.json"
        if gt_path.exists():
            try:
                with open(str(gt_path), 'r', encoding='utf-8') as f:
                    gt_data = json.load(f)
            except Exception:
                pass

    return jsonify({
        'video': dict(video),
        'watermarked': dict(wm),
        'events': events,
        'belonging_sets': belonging_sets,
        'ground_truth': gt_data
    })


# ── 事件标注 ──────────────────────────────────────────────────────────────────

@bp.route('/api/<int:video_id>/events/', methods=['GET'])
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


@bp.route('/api/<int:video_id>/events/', methods=['POST'])
def add_event(video_id):
    """添加事件（GT帧异步后台生成，自动更新JSON）"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM videos WHERE id = ?', (video_id,))
    video = cursor.fetchone()
    if not video:
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
        'INSERT INTO events (video_db_id, event_type, start_seconds, end_seconds, gt_frames_status) VALUES (?, ?, ?, ?, ?)',
        (video_id, event_type, start, end, 'pending')
    )
    db.commit()
    cursor.execute('UPDATE videos SET updated_at = CURRENT_TIMESTAMP WHERE id = ?', (video_id,))
    db.commit()
    event_id = cursor.lastrowid

    # 自动更新JSON
    if video['video_id']:
        generate_ground_truth_json(video_id)

    # 提前获取配置，传给后台线程
    project_root = current_app.config['PROJECT_ROOT']

    # 后台异步生成GT帧
    thread = threading.Thread(
        target=_capture_gt_frames_async,
        args=(video_id, event_id, event_type, start, end, project_root),
        daemon=True
    )
    thread.start()

    return jsonify({
        'success': True,
        'event': {
            'id': event_id,
            'event_type': event_type,
            'start_seconds': start,
            'end_seconds': end,
            'gt_frames_status': 'pending'
        }
    })


@bp.route('/api/<int:video_id>/events/<int:event_id>/', methods=['DELETE'])
def delete_event(video_id, event_id):
    """删除事件（同时删除关联的GT帧，自动更新JSON）"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM events WHERE id = ? AND video_db_id = ?', (event_id, video_id))
    event = cursor.fetchone()
    if not event:
        return jsonify({'error': '事件不存在'}), 404

    # 先获取该事件对应的GT帧文件路径并删除磁盘文件
    cursor.execute('SELECT file_path FROM gt_frames WHERE event_id = ?', (event_id,))
    frames = cursor.fetchall()
    for frame in frames:
        try:
            Path(frame['file_path']).unlink(missing_ok=True)
        except Exception:
            pass

    # 删除数据库中的GT帧记录
    cursor.execute('DELETE FROM gt_frames WHERE event_id = ?', (event_id,))

    # 删除事件
    cursor.execute('DELETE FROM events WHERE id = ?', (event_id,))
    cursor.execute('UPDATE videos SET updated_at = CURRENT_TIMESTAMP WHERE id = ?', (video_id,))
    db.commit()

    # 自动更新JSON
    cursor.execute('SELECT video_id FROM videos WHERE id = ?', (video_id,))
    v = cursor.fetchone()
    if v and v['video_id']:
        generate_ground_truth_json(video_id)

    return jsonify({'success': True})


@bp.route('/api/<int:video_id>/events/<int:event_id>/gt-frames/', methods=['GET'])
def get_event_gt_frames(video_id, event_id):
    """获取事件的所有GT帧"""
    db = get_db()
    cursor = db.cursor()

    # 验证事件存在且属于该视频
    cursor.execute('SELECT id FROM events WHERE id = ? AND video_db_id = ?', (event_id, video_id))
    if not cursor.fetchone():
        return jsonify({'error': '事件不存在'}), 404

    # 获取GT帧列表
    cursor.execute('''
        SELECT id, timestamp_sec, file_path, filename
        FROM gt_frames
        WHERE event_id = ?
        ORDER BY timestamp_sec
    ''', (event_id,))
    frames = cursor.fetchall()

    result = []
    for frame in frames:
        result.append({
            'id': frame['id'],
            'timestamp_sec': frame['timestamp_sec'],
            'file_path': frame['file_path'],
            'filename': frame['filename']
        })

    return jsonify({'frames': result})


def _capture_gt_frames_async(video_db_id, event_id, event_type, start_sec, end_sec, project_root):
    """后台异步：在事件范围内每秒截取一帧，保存为 GT 帧"""
    import sqlite3

    # 后台线程需要自己创建数据库连接
    conn = sqlite3.connect(str(DATABASE_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        # 更新状态为 processing
        cursor.execute('UPDATE events SET gt_frames_status = ? WHERE id = ?', ('processing', event_id))
        conn.commit()

        cursor.execute('SELECT * FROM videos WHERE id = ?', (video_db_id,))
        video = cursor.fetchone()
        if not video or not video['video_id']:
            cursor.execute('UPDATE events SET gt_frames_status = ? WHERE id = ?', ('failed', event_id))
            conn.commit()
            return

        # 优先使用打水印后的视频
        cursor.execute('''
            SELECT output_path FROM watermarked_videos
            WHERE original_video_id = ?
            ORDER BY created_at DESC LIMIT 1
        ''', (video_db_id,))
        wm = cursor.fetchone()
        video_path = Path(wm['output_path']) if wm else Path(video['original_path'])
        if not video_path.exists():
            cursor.execute('UPDATE events SET gt_frames_status = ? WHERE id = ?', ('failed', event_id))
            conn.commit()
            return

        # 创建输出目录（使用传入的 project_root）
        frames_dir = Path(project_root) / 'ground_truth_frames' / video['video_id']
        frames_dir.mkdir(parents=True, exist_ok=True)

        # 用 FFmpeg 每秒截一帧
        for t in range(int(start_sec), int(end_sec) + 1):
            filename = f'{event_id}_{t}.png'
            out_path = frames_dir / filename

            # 检查是否已经存在（防重复）
            if out_path.exists():
                # 检查数据库中是否已有记录
                cursor.execute('''
                    SELECT id FROM gt_frames
                    WHERE event_id = ? AND timestamp_sec = ?
                ''', (event_id, float(t)))
                if cursor.fetchone():
                    continue  # 已存在，跳过

            try:
                subprocess.run([
                    'ffmpeg', '-ss', str(t), '-i', str(video_path),
                    '-vframes', '1', '-y', '-f', 'image2',
                    '-loglevel', 'error',
                    str(out_path)
                ], timeout=30, check=True)
            except Exception:
                continue

            if out_path.exists():
                cursor.execute('''
                    INSERT INTO gt_frames
                    (video_db_id, event_id, event_type, timestamp_sec, file_path, filename)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (video_db_id, event_id, event_type, float(t), str(out_path), filename))
                conn.commit()

        # 完成
        cursor.execute('UPDATE events SET gt_frames_status = ? WHERE id = ?', ('done', event_id))
        conn.commit()

    except Exception as e:
        # 出错
        cursor.execute('UPDATE events SET gt_frames_status = ? WHERE id = ?', ('failed', event_id))
        conn.commit()
    finally:
        conn.close()


# ── Ground Truth JSON ─────────────────────────────────────────────────────────

@bp.route('/api/<int:video_id>/ground-truth/generate', methods=['POST'])
def generate_ground_truth(video_id):
    """根据事件标注生成 ground truth JSON 文件"""
    result = generate_ground_truth_json(video_id)
    if result is None:
        return jsonify({'error': '生成失败，请检查视频ID是否已设置'}), 400
    return jsonify({'success': True, 'data': result})


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


@bp.route('/api/<int:video_id>/ground-truth', methods=['PUT'])
def save_ground_truth(video_id):
    """保存 ground truth JSON 文件内容"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM videos WHERE id = ?', (video_id,))
    video = cursor.fetchone()
    if not video:
        return jsonify({'error': '视频不存在'}), 404

    vid = video['video_id']
    if not vid:
        return jsonify({'error': '请先设置视频ID'}), 400

    data = request.get_json()
    if not data or 'events' not in data:
        return jsonify({'error': '数据格式错误'}), 400

    # 验证数据格式
    for event in data.get('events', []):
        if 'type' not in event or 'start' not in event or 'end' not in event:
            return jsonify({'error': '事件格式错误，需要type/start/end字段'}), 400

    # 保存到文件
    gt_dir = Path(current_app.config['GROUND_TRUTH_DIR'])
    gt_dir.mkdir(parents=True, exist_ok=True)
    gt_path = gt_dir / f'{vid}.json'

    save_data = {
        'file': video['filename'],
        'id': vid,
        'events': data['events']
    }

    with open(str(gt_path), 'w', encoding='utf-8') as f:
        json.dump(save_data, f, ensure_ascii=False, indent=2)

    # 可选：同步更新数据库中的events（如果需要双向同步）
    # 这里暂不实现，保持文件为主

    return jsonify({'success': True})


# ── 评测视频集管理 ───────────────────────────────────────────────────────────

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
        result.append(s_dict)

    return jsonify({'sets': result})


@bp.route('/api/eval-sets', methods=['POST'])
def create_eval_set():
    """创建新的评测视频集"""
    data = request.get_json() or {}
    name = data.get('name', '').strip()
    video_ids = data.get('video_ids', [])

    if not name:
        return jsonify({'error': '评测集名称不能为空'}), 400

    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        'INSERT INTO eval_video_sets (name, notes, video_ids) VALUES (?, ?, ?)',
        (name, data.get('notes', ''), json.dumps(video_ids))
    )
    db.commit()

    return jsonify({'success': True, 'id': cursor.lastrowid})


@bp.route('/api/eval-sets/<int:set_id>/add', methods=['POST'])
def add_video_to_eval_set(set_id):
    """添加单个视频到评测集"""
    data = request.get_json() or {}
    video_id = data.get('video_id')

    if not video_id:
        return jsonify({'error': '视频ID不能为空'}), 400

    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM eval_video_sets WHERE id = ?', (set_id,))
    eval_set = cursor.fetchone()

    if not eval_set:
        return jsonify({'error': '评测集不存在'}), 404

    current_ids = []
    if eval_set['video_ids']:
        try:
            current_ids = json.loads(eval_set['video_ids'])
        except Exception:
            current_ids = []

    if video_id not in current_ids:
        current_ids.append(video_id)
        cursor.execute(
            'UPDATE eval_video_sets SET video_ids = ? WHERE id = ?',
            (json.dumps(current_ids), set_id)
        )
        db.commit()

    return jsonify({'success': True})


@bp.route('/api/eval-sets/batch-add', methods=['POST'])
def batch_add_to_eval_set():
    """批量添加视频到评测集"""
    data = request.get_json() or {}
    set_id = data.get('set_id')
    video_ids = data.get('video_ids', [])

    if not set_id:
        return jsonify({'error': '请选择评测集'}), 400
    if not video_ids:
        return jsonify({'error': '请选择要添加的视频'}), 400

    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM eval_video_sets WHERE id = ?', (set_id,))
    eval_set = cursor.fetchone()

    if not eval_set:
        return jsonify({'error': '评测集不存在'}), 404

    current_ids = []
    if eval_set['video_ids']:
        try:
            current_ids = json.loads(eval_set['video_ids'])
        except Exception:
            current_ids = []

    added_count = 0
    for vid in video_ids:
        if vid not in current_ids:
            current_ids.append(vid)
            added_count += 1

    cursor.execute(
        'UPDATE eval_video_sets SET video_ids = ? WHERE id = ?',
        (json.dumps(current_ids), set_id)
    )
    db.commit()

    return jsonify({'success': True, 'added_count': added_count})


@bp.route('/api/eval-sets/batch-remove', methods=['POST'])
def batch_remove_from_eval_set():
    """批量从评测集移除视频"""
    data = request.get_json() or {}
    set_id = data.get('set_id')
    video_ids = data.get('video_ids', [])

    if not set_id:
        return jsonify({'error': '请选择评测集'}), 400
    if not video_ids:
        return jsonify({'error': '请选择要移出的视频'}), 400

    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM eval_video_sets WHERE id = ?', (set_id,))
    eval_set = cursor.fetchone()

    if not eval_set:
        return jsonify({'error': '评测集不存在'}), 404

    current_ids = []
    if eval_set['video_ids']:
        try:
            current_ids = json.loads(eval_set['video_ids'])
        except Exception:
            current_ids = []

    removed_count = 0
    for vid in video_ids:
        if vid in current_ids:
            current_ids.remove(vid)
            removed_count += 1

    cursor.execute(
        'UPDATE eval_video_sets SET video_ids = ? WHERE id = ?',
        (json.dumps(current_ids), set_id)
    )
    db.commit()

    return jsonify({'success': True, 'removed_count': removed_count})


@bp.route('/api/eval-sets/<int:set_id>/remove', methods=['POST'])
def remove_video_from_eval_set(set_id):
    """从评测集移除视频"""
    data = request.get_json() or {}
    video_id = data.get('video_id')

    if not video_id:
        return jsonify({'error': '视频ID不能为空'}), 400

    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM eval_video_sets WHERE id = ?', (set_id,))
    eval_set = cursor.fetchone()

    if not eval_set:
        return jsonify({'error': '评测集不存在'}), 404

    current_ids = []
    if eval_set['video_ids']:
        try:
            current_ids = json.loads(eval_set['video_ids'])
        except Exception:
            current_ids = []

    if video_id in current_ids:
        current_ids.remove(video_id)
        cursor.execute(
            'UPDATE eval_video_sets SET video_ids = ? WHERE id = ?',
            (json.dumps(current_ids), set_id)
        )
        db.commit()

    return jsonify({'success': True})


@bp.route('/api/eval-sets/<int:set_id>', methods=['PUT'])
def rename_eval_set(set_id):
    """重命名评测集"""
    data = request.get_json() or {}
    new_name = data.get('name', '').strip()

    if not new_name:
        return jsonify({'error': '名称不能为空'}), 400

    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id FROM eval_video_sets WHERE id = ?', (set_id,))
    if not cursor.fetchone():
        return jsonify({'error': '评测集不存在'}), 404

    cursor.execute('UPDATE eval_video_sets SET name = ? WHERE id = ?', (new_name, set_id))
    db.commit()
    return jsonify({'success': True})


@bp.route('/api/eval-sets/<int:set_id>', methods=['DELETE'])
def delete_eval_set(set_id):
    """删除评测集"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id FROM eval_video_sets WHERE id = ?', (set_id,))
    if not cursor.fetchone():
        return jsonify({'error': '评测集不存在'}), 404

    cursor.execute('DELETE FROM eval_video_sets WHERE id = ?', (set_id,))
    db.commit()
    return jsonify({'success': True})


# ── 视频处理（拼接/打包）────────────────────────────────────────────────────────

# 内存中的任务进度存储
video_process_tasks = {}
watermark_tasks = {}
watermark_lock = threading.Lock()


def concat_videos_ffmpeg(video_paths, output_path):
    """使用FFmpeg拼接多个视频"""
    list_file = tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8')
    for path in video_paths:
        list_file.write(f"file '{path}'\n")
    list_file.close()

    try:
        # 先尝试 copy 模式
        cmd = [
            'ffmpeg', '-f', 'concat', '-safe', '0',
            '-i', list_file.name,
            '-c', 'copy',
            '-y',
            str(output_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return True

        # 如果失败，使用重新编码模式
        cmd = [
            'ffmpeg', '-f', 'concat', '-safe', '0',
            '-i', list_file.name,
            '-c:v', 'libx264',
            '-c:a', 'aac',
            '-pix_fmt', 'yuv420p',
            '-y',
            str(output_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.returncode == 0
    finally:
        try:
            os.unlink(list_file.name)
        except:
            pass


def package_videos_zip(video_paths, output_path):
    """将多个视频打包为ZIP"""
    try:
        with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for path in video_paths:
                if Path(path).exists():
                    zf.write(path, Path(path).name)
        return True
    except Exception:
        return False


def _do_concat_task(task_id, video_ids, project_root, generated_dir):
    """后台执行拼接任务"""
    import sqlite3
    import time
    conn = sqlite3.connect(str(DATABASE_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    stop_flag = [False]

    def update_progress_smoothly(start_p, end_p, duration_sec):
        """平滑更新进度"""
        steps = 20
        step_duration = duration_sec / steps
        for i in range(steps + 1):
            if stop_flag[0]:
                break
            progress = start_p + int((end_p - start_p) * (i / steps))
            video_process_tasks[task_id]['progress'] = progress
            time.sleep(step_duration)

    try:
        video_process_tasks[task_id]['progress'] = 10

        # 查询视频路径
        video_paths = []
        video_names = []
        for vid in video_ids:
            cursor.execute('''
                SELECT w.output_path, v.video_id, v.filename
                FROM watermarked_videos w
                JOIN videos v ON v.id = w.original_video_id
                WHERE w.original_video_id = ?
                ORDER BY w.created_at DESC LIMIT 1
            ''', (vid,))
            row = cursor.fetchone()
            if row and Path(row['output_path']).exists():
                video_paths.append(row['output_path'])
                video_names.append(row['video_id'] or Path(row['filename']).stem)

        if len(video_paths) < 2:
            video_process_tasks[task_id]['status'] = 'failed'
            video_process_tasks[task_id]['error'] = '没有足够的视频进行拼接'
            conn.close()
            return

        video_process_tasks[task_id]['progress'] = 20

        # 生成输出文件名
        timestamp = int(time.time())
        output_name = f"concat_{timestamp}_{'_'.join(video_names[:3])}.mp4"
        if len(output_name) > 100:
            output_name = f"concat_{timestamp}_{len(video_ids)}videos.mp4"
        output_path = Path(generated_dir) / output_name

        # 阶段1: 准备 (20-30%)
        stop_flag[0] = False
        progress_thread = threading.Thread(
            target=update_progress_smoothly,
            args=(20, 30, 2),
            daemon=True
        )
        progress_thread.start()
        time.sleep(0.5)
        stop_flag[0] = True
        progress_thread.join(timeout=0.1)

        video_process_tasks[task_id]['progress'] = 30

        # 阶段2: 执行拼接 (30-80%)
        # 启动后台线程平滑更新进度
        concat_stop_flag = [False]
        def update_progress_during_concat():
            for i in range(45):  # 45 * 0.3s = 13.5s
                if concat_stop_flag[0]:
                    break
                progress = 30 + i
                if progress < 75:
                    video_process_tasks[task_id]['progress'] = progress
                time.sleep(0.3)

        progress_thread = threading.Thread(target=update_progress_during_concat)
        progress_thread.start()

        # 执行拼接（阻塞）
        result = concat_videos_ffmpeg(video_paths, output_path)

        concat_stop_flag[0] = True
        progress_thread.join()

        if result:
            video_process_tasks[task_id]['progress'] = 85

            # 保存到数据库
            file_size = output_path.stat().st_size
            cursor.execute('''
                INSERT INTO generated_videos (name, type, file_path, file_size, source_video_ids, status)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (output_name, 'concat', str(output_path), file_size, json.dumps(video_ids), 'done'))
            conn.commit()
            gen_id = cursor.lastrowid

            video_process_tasks[task_id]['status'] = 'done'
            video_process_tasks[task_id]['progress'] = 100
            video_process_tasks[task_id]['generated_id'] = gen_id
            video_process_tasks[task_id]['name'] = output_name
        else:
            video_process_tasks[task_id]['status'] = 'failed'
            video_process_tasks[task_id]['error'] = 'FFmpeg拼接失败'

    except Exception as e:
        video_process_tasks[task_id]['status'] = 'failed'
        video_process_tasks[task_id]['error'] = str(e)
    finally:
        conn.close()


def _do_package_task(task_id, video_ids, project_root, generated_dir):
    """后台执行打包任务"""
    import sqlite3
    import time
    conn = sqlite3.connect(str(DATABASE_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    stop_flag = [False]

    def update_progress_smoothly(start_p, end_p, duration_sec):
        """平滑更新进度"""
        steps = 20
        step_duration = duration_sec / steps
        for i in range(steps + 1):
            if stop_flag[0]:
                break
            progress = start_p + int((end_p - start_p) * (i / steps))
            video_process_tasks[task_id]['progress'] = progress
            time.sleep(step_duration)

    try:
        video_process_tasks[task_id]['progress'] = 10

        # 查询视频路径
        video_paths = []
        video_names = []
        for vid in video_ids:
            cursor.execute('''
                SELECT w.output_path, v.video_id, v.filename
                FROM watermarked_videos w
                JOIN videos v ON v.id = w.original_video_id
                WHERE w.original_video_id = ?
                ORDER BY w.created_at DESC LIMIT 1
            ''', (vid,))
            row = cursor.fetchone()
            if row and Path(row['output_path']).exists():
                video_paths.append(row['output_path'])
                video_names.append(row['video_id'] or Path(row['filename']).stem)

        if not video_paths:
            video_process_tasks[task_id]['status'] = 'failed'
            video_process_tasks[task_id]['error'] = '没有可打包的视频'
            conn.close()
            return

        video_process_tasks[task_id]['progress'] = 20

        # 生成输出文件名
        timestamp = int(time.time())
        output_name = f"package_{timestamp}_{len(video_ids)}videos.zip"
        output_path = Path(generated_dir) / output_name

        video_process_tasks[task_id]['progress'] = 30

        # 阶段2: 执行打包 (30-80%)
        # 启动后台线程平滑更新进度
        pack_stop_flag = [False]
        def update_progress_during_pack():
            for i in range(50):  # 50 * 0.2s = 10s
                if pack_stop_flag[0]:
                    break
                progress = 30 + i
                if progress < 75:
                    video_process_tasks[task_id]['progress'] = progress
                time.sleep(0.2)

        progress_thread = threading.Thread(target=update_progress_during_pack)
        progress_thread.start()

        # 执行打包（阻塞）
        result = package_videos_zip(video_paths, output_path)

        pack_stop_flag[0] = True
        progress_thread.join()

        if result:
            video_process_tasks[task_id]['progress'] = 85

            # 保存到数据库
            file_size = output_path.stat().st_size
            cursor.execute('''
                INSERT INTO generated_videos (name, type, file_path, file_size, source_video_ids, status)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (output_name, 'package', str(output_path), file_size, json.dumps(video_ids), 'done'))
            conn.commit()
            gen_id = cursor.lastrowid

            video_process_tasks[task_id]['status'] = 'done'
            video_process_tasks[task_id]['progress'] = 100
            video_process_tasks[task_id]['generated_id'] = gen_id
            video_process_tasks[task_id]['name'] = output_name
        else:
            video_process_tasks[task_id]['status'] = 'failed'
            video_process_tasks[task_id]['error'] = '打包失败'

    except Exception as e:
        video_process_tasks[task_id]['status'] = 'failed'
        video_process_tasks[task_id]['error'] = str(e)
    finally:
        conn.close()


@bp.route('/api/concat', methods=['POST'])
def concat_videos():
    """开始拼接视频（最多10个）"""
    data = request.get_json() or {}
    video_ids = data.get('video_ids', [])

    if not video_ids:
        return jsonify({'error': '请选择要拼接的视频'}), 400
    if len(video_ids) > 10:
        return jsonify({'error': '最多只能选择10个视频进行拼接'}), 400
    if len(video_ids) < 2:
        return jsonify({'error': '至少需要选择2个视频进行拼接'}), 400

    # 生成任务ID
    task_id = f"concat_{int(time.time())}_{random.randint(1000, 9999)}"
    video_process_tasks[task_id] = {
        'type': 'concat',
        'status': 'processing',
        'progress': 10,
        'output_path': None,
        'error': None,
        'generated_id': None,
        'name': None
    }

    # 启动后台线程
    generated_dir = current_app.config['GENERATED_VIDEOS_DIR']
    project_root = current_app.config['PROJECT_ROOT']
    thread = threading.Thread(
        target=_do_concat_task,
        args=(task_id, video_ids, project_root, generated_dir)
    )
    thread.start()

    return jsonify({'success': True, 'task_id': task_id})


@bp.route('/api/package', methods=['POST'])
def package_videos():
    """开始打包视频（最多10个）"""
    data = request.get_json() or {}
    video_ids = data.get('video_ids', [])

    if not video_ids:
        return jsonify({'error': '请选择要打包的视频'}), 400
    if len(video_ids) > 10:
        return jsonify({'error': '最多只能选择10个视频进行打包'}), 400

    # 生成任务ID
    task_id = f"package_{int(time.time())}_{random.randint(1000, 9999)}"
    video_process_tasks[task_id] = {
        'type': 'package',
        'status': 'processing',
        'progress': 10,
        'output_path': None,
        'error': None,
        'generated_id': None,
        'name': None
    }

    # 启动后台线程
    generated_dir = current_app.config['GENERATED_VIDEOS_DIR']
    project_root = current_app.config['PROJECT_ROOT']
    thread = threading.Thread(
        target=_do_package_task,
        args=(task_id, video_ids, project_root, generated_dir)
    )
    thread.start()

    return jsonify({'success': True, 'task_id': task_id})


@bp.route('/api/tasks/<task_id>/progress', methods=['GET'])
def get_task_progress(task_id):
    """获取任务进度"""
    task = video_process_tasks.get(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404

    return jsonify({
        'type': task['type'],
        'status': task['status'],
        'progress': task['progress'],
        'error': task['error'],
        'generated_id': task['generated_id'],
        'name': task['name']
    })


# ── 生成视频管理 ──────────────────────────────────────────────────────────────

@bp.route('/api/generated', methods=['GET'])
def list_generated_videos():
    """获取所有生成的视频列表"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM generated_videos ORDER BY created_at DESC')
    rows = cursor.fetchall()

    result = []
    for row in rows:
        item = dict(row)
        if item.get('source_video_ids'):
            try:
                item['source_video_ids'] = json.loads(item['source_video_ids'])
            except Exception:
                item['source_video_ids'] = []
        else:
            item['source_video_ids'] = []
        result.append(item)

    return jsonify(result)


@bp.route('/api/generated/<int:gen_id>', methods=['DELETE'])
def delete_generated_video(gen_id):
    """删除生成的视频"""
    db = get_db()
    cursor = db.cursor()

    cursor.execute('SELECT * FROM generated_videos WHERE id = ?', (gen_id,))
    row = cursor.fetchone()
    if not row:
        return jsonify({'error': '生成的视频不存在'}), 404

    # 删除文件
    try:
        Path(row['file_path']).unlink(missing_ok=True)
    except Exception:
        pass

    # 删除数据库记录
    cursor.execute('DELETE FROM generated_videos WHERE id = ?', (gen_id,))
    db.commit()

    return jsonify({'success': True})


@bp.route('/api/generated/<int:gen_id>/download', methods=['GET'])
def download_generated_video(gen_id):
    """下载生成的视频"""
    db = get_db()
    cursor = db.cursor()

    cursor.execute('SELECT * FROM generated_videos WHERE id = ?', (gen_id,))
    row = cursor.fetchone()
    if not row:
        return jsonify({'error': '生成的视频不存在'}), 404

    file_path = Path(row['file_path'])
    if not file_path.exists():
        return jsonify({'error': '文件不存在'}), 404

    return send_file(
        str(file_path),
        as_attachment=True,
        download_name=row['name']
    )


# ── 视频剪辑 ──────────────────────────────────────────────────────────────

def trim_video_ffmpeg(input_path, output_path, start_sec, end_sec):
    """使用FFmpeg裁剪视频"""
    try:
        cmd = [
            'ffmpeg', '-i', str(input_path),
            '-ss', str(start_sec),
            '-to', str(end_sec),
            '-c', 'copy',
            '-y',
            '-loglevel', 'error',
            str(output_path)
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and output_path.exists():
            return True

        # 如果 copy 模式失败，尝试重新编码
        cmd = [
            'ffmpeg', '-i', str(input_path),
            '-ss', str(start_sec),
            '-to', str(end_sec),
            '-c:v', 'libx264',
            '-c:a', 'aac',
            '-y',
            '-loglevel', 'error',
            str(output_path)
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.returncode == 0 and output_path.exists()

    except Exception as e:
        current_app.logger.error(f'视频裁剪失败: {str(e)}')
        return False


@bp.route('/api/<int:video_id>/trim', methods=['POST'])
def trim_video(video_id):
    """裁剪视频"""
    data = request.get_json() or {}
    start_sec = data.get('start_seconds')
    end_sec = data.get('end_seconds')
    new_video_id = data.get('new_video_id', '').strip()

    if start_sec is None or end_sec is None:
        return jsonify({'error': '开始和结束时间不能为空'}), 400
    try:
        start_sec = float(start_sec)
        end_sec = float(end_sec)
    except Exception:
        return jsonify({'error': '时间格式错误'}), 400
    if start_sec >= end_sec:
        return jsonify({'error': '开始时间必须小于结束时间'}), 400
    if not new_video_id:
        return jsonify({'error': '新视频ID不能为空'}), 400
    if not re.fullmatch(r'\d{10}', new_video_id):
        return jsonify({'error': '新视频ID必须为10位数字'}), 400

    db = get_db()
    cursor = db.cursor()

    # 验证原视频存在
    cursor.execute('SELECT * FROM videos WHERE id = ?', (video_id,))
    original_video = cursor.fetchone()
    if not original_video:
        return jsonify({'error': '原视频不存在'}), 404

    # 检查新视频ID是否已被使用
    cursor.execute('SELECT id FROM videos WHERE video_id = ?', (new_video_id,))
    if cursor.fetchone():
        return jsonify({'error': '该视频ID已被使用，请使用其他ID'}), 400

    # 获取视频源文件路径（优先使用水印后的视频）
    cursor.execute('''
        SELECT output_path FROM watermarked_videos
        WHERE original_video_id = ?
        ORDER BY created_at DESC LIMIT 1
    ''', (video_id,))
    watermarked = cursor.fetchone()
    source_path = None
    if watermarked:
        source_path = Path(watermarked['output_path'])
    else:
        source_path = Path(original_video['original_path'])

    if not source_path.exists():
        return jsonify({'error': '视频文件不存在'}), 404

    # 确定输出路径
    upload_dir = Path(current_app.config['UPLOAD_VIDEOS'])
    upload_dir.mkdir(parents=True, exist_ok=True)

    original_ext = source_path.suffix
    new_filename = f"trimmed_{new_video_id}{original_ext}"
    output_path = upload_dir / new_filename

    # 执行裁剪
    if not trim_video_ffmpeg(source_path, output_path, start_sec, end_sec):
        return jsonify({'error': '视频裁剪失败'}), 500

    # 保存新视频到数据库
    file_size = output_path.stat().st_size
    duration = end_sec - start_sec

    cursor.execute('''
        INSERT INTO videos (filename, original_path, video_id, file_size, duration, video_id_confirmed)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (new_filename, str(output_path), new_video_id, file_size, duration, 1))
    db.commit()

    new_video_db_id = cursor.lastrowid

    # 自动生成水印版本（可选：这里可以让用户手动打水印，或者自动打水印）
    # 这里选择让用户手动打水印，因为打水印需要时间且可能有配置

    return jsonify({
        'success': True,
        'new_video_id': new_video_db_id,
        'video_id': new_video_id,
        'filename': new_filename,
        'duration': duration
    })
