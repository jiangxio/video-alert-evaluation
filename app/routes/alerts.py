"""告警数据集相关路由"""
from flask import Blueprint, request, jsonify, render_template, current_app
from pathlib import Path
import json
import os
import threading
import zipfile
import tempfile
import shutil

from app.database import get_db, DATABASE_PATH
from app.services.verification_service import (
    parse_alert_config, extract_alert_type_id, run_ocr
)
from app.routes import send_file_with_cache

bp = Blueprint('alerts', __name__, url_prefix='/alerts')

# 批量 OCR 进度状态（内存存储，key=dataset_id）
_ocr_progress = {}
_ocr_lock = threading.Lock()


def allowed_file(filename, allowed_extensions):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed_extensions


def _get_image_size(file_path):
    """用 Pillow 读取图片尺寸，失败返回 (None, None)"""
    try:
        from PIL import Image
        with Image.open(file_path) as img:
            return img.size  # (width, height)
    except Exception:
        return None, None


def _load_alert_config():
    """加载告警类型配置"""
    import sqlite3
    config_path = Path(current_app.config['REPORT_DIR']) / 'config.json'
    if config_path.exists():
        return parse_alert_config(str(config_path))
    return {}


# ── 数据集列表页 ──────────────────────────────────────────────────────────────

@bp.route('/')
def alerts_page():
    return render_template('alerts.html')


@bp.route('/api/event-types', methods=['GET'])
def get_event_types():
    """返回配置文件中的事件类型列表"""
    config = _load_alert_config()
    # 按 ID 排序，返回 [{id, name}] 列表
    types = sorted(
        [{'id': k, 'name': v} for k, v in config.items()],
        key=lambda x: x['id']
    )
    return jsonify(types)


@bp.route('/api/datasets', methods=['GET'])
def list_datasets():
    db = get_db()
    cursor = db.cursor()
    cursor.execute('''
        SELECT d.*, COUNT(a.id) AS image_count
        FROM datasets d
        LEFT JOIN alert_images a ON a.dataset_id = d.id
        GROUP BY d.id
        ORDER BY d.created_at DESC
    ''')
    rows = cursor.fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route('/api/datasets', methods=['POST'])
def create_dataset():
    data = request.get_json() or {}
    name = data.get('name', '').strip()
    notes = data.get('notes', '').strip()
    if not name:
        return jsonify({'error': '数据集名称不能为空'}), 400

    db = get_db()
    cursor = db.cursor()
    cursor.execute('INSERT INTO datasets (name, notes) VALUES (?, ?)', (name, notes or None))
    db.commit()
    dataset_id = cursor.lastrowid

    cursor.execute('SELECT * FROM datasets WHERE id = ?', (dataset_id,))
    row = dict(cursor.fetchone())
    row['image_count'] = 0
    return jsonify({'success': True, 'dataset': row})


@bp.route('/api/datasets/<int:dataset_id>', methods=['DELETE'])
def delete_dataset(dataset_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id FROM datasets WHERE id = ?', (dataset_id,))
    if not cursor.fetchone():
        return jsonify({'error': '数据集不存在'}), 404

    # 删除磁盘上的图片文件
    cursor.execute('SELECT file_path FROM alert_images WHERE dataset_id = ?', (dataset_id,))
    for row in cursor.fetchall():
        try:
            os.unlink(row['file_path'])
        except Exception:
            pass

    cursor.execute('DELETE FROM alert_images WHERE dataset_id = ?', (dataset_id,))
    cursor.execute('DELETE FROM datasets WHERE id = ?', (dataset_id,))
    db.commit()
    return jsonify({'success': True})


@bp.route('/api/datasets/<int:dataset_id>/import', methods=['POST'])
def import_zip(dataset_id):
    """从 ZIP 压缩包导入图片到数据集"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id FROM datasets WHERE id = ?', (dataset_id,))
    if not cursor.fetchone():
        return jsonify({'error': '数据集不存在'}), 404

    if 'file' not in request.files:
        return jsonify({'error': '没有上传文件'}), 400
    f = request.files['file']
    if not f.filename.lower().endswith('.zip'):
        return jsonify({'error': '仅支持 .zip 格式'}), 400

    config = _load_alert_config()
    IMAGE_EXTS = current_app.config['ALLOWED_IMAGE_EXTENSIONS']

    tmp_dir = tempfile.mkdtemp()
    try:
        zip_path = os.path.join(tmp_dir, 'upload.zip')
        f.save(zip_path)

        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(tmp_dir)

        imported, skipped = [], []

        for src in sorted(Path(tmp_dir).rglob('*')):
            if not src.is_file():
                continue
            if src.suffix.lower().lstrip('.') not in IMAGE_EXTS:
                continue
            filename = src.name

            # 同数据集内防重复
            cursor.execute(
                'SELECT id FROM alert_images WHERE dataset_id = ? AND filename = ?',
                (dataset_id, filename)
            )
            if cursor.fetchone():
                skipped.append(filename)
                continue

            dest = Path(current_app.config['UPLOAD_ALERTS']) / filename
            if dest.exists():
                import uuid
                dest = dest.parent / f'{dest.stem}_{uuid.uuid4().hex[:6]}{dest.suffix}'
                filename = dest.name

            shutil.copy2(str(src), str(dest))

            width, height = _get_image_size(str(dest))
            alert_type_id = extract_alert_type_id(filename)
            alert_type = config.get(alert_type_id) if alert_type_id else None
            file_size = dest.stat().st_size

            cursor.execute('''
                INSERT INTO alert_images
                (filename, file_path, alert_type_id, alert_type, file_size,
                 dataset_id, image_width, image_height, event_label)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (filename, str(dest), alert_type_id, alert_type, file_size,
                  dataset_id, width, height, alert_type))
            db.commit()
            imported.append(filename)

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return jsonify({
        'success': True,
        'imported': len(imported),
        'skipped': len(skipped),
        'skipped_files': skipped,
    })


# ── 数据集详情页 ──────────────────────────────────────────────────────────────

@bp.route('/<int:dataset_id>/')
def dataset_detail_page(dataset_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM datasets WHERE id = ?', (dataset_id,))
    dataset = cursor.fetchone()
    if not dataset:
        return '数据集不存在', 404
    dataset_dict = dict(dataset)
    # 确保 created_at 为字符串，避免模板切片报错
    if hasattr(dataset_dict.get('created_at'), 'strftime'):
        dataset_dict['created_at'] = dataset_dict['created_at'].strftime('%Y-%m-%d %H:%M:%S')
    else:
        dataset_dict['created_at'] = str(dataset_dict.get('created_at', ''))
    return render_template('dataset_detail.html', dataset=dataset_dict)


@bp.route('/api/datasets/<int:dataset_id>/images', methods=['GET'])
def list_dataset_images(dataset_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id FROM datasets WHERE id = ?', (dataset_id,))
    if not cursor.fetchone():
        return jsonify({'error': '数据集不存在'}), 404

    cursor.execute(
        'SELECT * FROM alert_images WHERE dataset_id = ? ORDER BY uploaded_at ASC',
        (dataset_id,)
    )
    images = []
    for row in cursor.fetchall():
        img = dict(row)
        # 附上最新 OCR 结果
        cursor.execute('''
            SELECT video_id, timestamp, timestamp_seconds, success
            FROM ocr_results WHERE alert_image_id = ?
            ORDER BY created_at DESC LIMIT 1
        ''', (img['id'],))
        ocr = cursor.fetchone()
        img['ocr'] = dict(ocr) if ocr else None
        images.append(img)
    return jsonify(images)


@bp.route('/api/datasets/<int:dataset_id>/upload', methods=['POST'])
def upload_to_dataset(dataset_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id FROM datasets WHERE id = ?', (dataset_id,))
    if not cursor.fetchone():
        return jsonify({'error': '数据集不存在'}), 404

    files = request.files.getlist('image')
    if not files:
        return jsonify({'error': '没有上传文件'}), 400

    config = _load_alert_config()
    results = []
    errors = []

    for file in files:
        if not file.filename:
            continue
        if not allowed_file(file.filename, current_app.config['ALLOWED_IMAGE_EXTENSIONS']):
            errors.append(f'{file.filename}: 不支持的格式')
            continue

        filename = file.filename

        # 同数据集内防重复
        cursor.execute(
            'SELECT id FROM alert_images WHERE dataset_id = ? AND filename = ?',
            (dataset_id, filename)
        )
        if cursor.fetchone():
            errors.append(f'{filename}: 已存在于该数据集')
            continue

        save_path = Path(current_app.config['UPLOAD_ALERTS']) / filename
        # 磁盘上文件名冲突时加后缀
        if save_path.exists():
            stem = save_path.stem
            suffix = save_path.suffix
            import uuid
            save_path = save_path.parent / f'{stem}_{uuid.uuid4().hex[:6]}{suffix}'
            filename = save_path.name

        file.save(str(save_path))

        width, height = _get_image_size(str(save_path))
        alert_type_id = extract_alert_type_id(filename)
        alert_type = config.get(alert_type_id) if alert_type_id else None
        file_size = save_path.stat().st_size

        cursor.execute('''
            INSERT INTO alert_images
            (filename, file_path, alert_type_id, alert_type, file_size,
             dataset_id, image_width, image_height, event_label)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (filename, str(save_path), alert_type_id, alert_type, file_size,
              dataset_id, width, height, alert_type))
        db.commit()

        results.append({
            'id': cursor.lastrowid,
            'filename': filename,
            'alert_type': alert_type,
            'image_width': width,
            'image_height': height,
        })

    return jsonify({'success': True, 'uploaded': results, 'errors': errors})


# ── 图片操作 ──────────────────────────────────────────────────────────────────

@bp.route('/api/images/<int:image_id>', methods=['GET'])
def get_image_detail(image_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM alert_images WHERE id = ?', (image_id,))
    img = cursor.fetchone()
    if not img:
        return jsonify({'error': '图片不存在'}), 404

    result = dict(img)
    cursor.execute('''
        SELECT video_id, timestamp, timestamp_seconds, success, full_result, raw_ocr_text
        FROM ocr_results WHERE alert_image_id = ?
        ORDER BY created_at DESC LIMIT 1
    ''', (image_id,))
    ocr_row = cursor.fetchone()
    if ocr_row:
        ocr = dict(ocr_row)
        # 如果有 full_result，优先使用它
        if ocr.get('full_result'):
            try:
                full_result = json.loads(ocr['full_result'])
                for key, value in full_result.items():
                    if key not in ocr or ocr[key] is None:
                        ocr[key] = value
            except (json.JSONDecodeError, TypeError):
                pass
        result['ocr'] = ocr
    else:
        result['ocr'] = None
    return jsonify(result)


@bp.route('/api/images/<int:image_id>/file', methods=['GET'])
def serve_image(image_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT file_path, filename FROM alert_images WHERE id = ?', (image_id,))
    row = cursor.fetchone()
    if not row:
        return jsonify({'error': '图片不存在'}), 404
    path = Path(row['file_path'])
    if not path.exists():
        return jsonify({'error': '文件不存在于磁盘'}), 404
    return send_file_with_cache(str(path))


@bp.route('/api/images/<int:image_id>/label', methods=['PUT'])
def set_label(image_id):
    data = request.get_json() or {}
    label = data.get('event_label', '').strip()
    if not label:
        return jsonify({'error': 'event_label 不能为空'}), 400

    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id FROM alert_images WHERE id = ?', (image_id,))
    if not cursor.fetchone():
        return jsonify({'error': '图片不存在'}), 404

    cursor.execute('UPDATE alert_images SET event_label = ? WHERE id = ?', (label, image_id))
    db.commit()
    return jsonify({'success': True, 'event_label': label})


@bp.route('/api/images/<int:image_id>', methods=['DELETE'])
def delete_image(image_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT file_path FROM alert_images WHERE id = ?', (image_id,))
    row = cursor.fetchone()
    if not row:
        return jsonify({'error': '图片不存在'}), 404

    try:
        os.unlink(row['file_path'])
    except Exception:
        pass
    cursor.execute('DELETE FROM alert_images WHERE id = ?', (image_id,))
    db.commit()
    return jsonify({'success': True})


# ── OCR ───────────────────────────────────────────────────────────────────────

@bp.route('/api/images/<int:image_id>/ocr', methods=['POST'])
def ocr_single(image_id):
    """对单张图片执行 OCR"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT * FROM alert_images WHERE id = ?', (image_id,))
    img = cursor.fetchone()
    if not img:
        return jsonify({'error': '图片不存在'}), 404

    ocr_result = run_ocr(img['file_path'])

    if 'error' not in ocr_result:
        cursor.execute('''
            INSERT INTO ocr_results
            (alert_image_id, raw_ocr_text, video_id, timestamp, timestamp_seconds, success, full_result)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            image_id,
            ocr_result.get('raw_ocr_text'),
            ocr_result.get('video_id'),
            ocr_result.get('timestamp'),
            ocr_result.get('timestamp_seconds'),
            ocr_result.get('success', False),
            json.dumps(ocr_result, ensure_ascii=False)
        ))
        db.commit()

    return jsonify({'success': 'error' not in ocr_result, 'ocr': ocr_result})


@bp.route('/api/images/<int:image_id>/ocr/manual', methods=['POST'])
def ocr_save_manual(image_id):
    """手动保存OCR结果"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id FROM alert_images WHERE id = ?', (image_id,))
    if not cursor.fetchone():
        return jsonify({'error': '图片不存在'}), 404

    data = request.get_json() or {}
    video_id = data.get('video_id')
    timestamp = data.get('timestamp')
    timestamp_seconds = data.get('timestamp_seconds')
    success = data.get('success', False)

    # 构建完整结果对象
    ocr_result = {
        'raw_ocr_text': '手动输入',
        'video_id': video_id,
        'timestamp': timestamp,
        'timestamp_seconds': timestamp_seconds,
        'success': success
    }

    cursor.execute('''
        INSERT INTO ocr_results
        (alert_image_id, raw_ocr_text, video_id, timestamp, timestamp_seconds, success, full_result)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (
        image_id,
        '手动输入',
        video_id,
        timestamp,
        timestamp_seconds,
        success,
        json.dumps(ocr_result, ensure_ascii=False)
    ))
    db.commit()

    return jsonify({'success': True, 'ocr': ocr_result})


@bp.route('/api/datasets/<int:dataset_id>/ocr/batch', methods=['POST'])
def ocr_batch(dataset_id):
    """批量 OCR：后台线程逐张处理，可通过 /ocr/status 查询进度"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id FROM datasets WHERE id = ?', (dataset_id,))
    if not cursor.fetchone():
        return jsonify({'error': '数据集不存在'}), 404

    cursor.execute(
        'SELECT id, file_path FROM alert_images WHERE dataset_id = ?',
        (dataset_id,)
    )
    images = [dict(r) for r in cursor.fetchall()]
    if not images:
        return jsonify({'error': '数据集内没有图片'}), 400

    # 获取 stop_on_failure 参数，默认为 False
    stop_on_failure = False
    if request.is_json:
        data = request.get_json() or {}
        stop_on_failure = data.get('stop_on_failure', False)

    with _ocr_lock:
        if _ocr_progress.get(dataset_id, {}).get('running'):
            return jsonify({'error': 'OCR 正在运行中'}), 409
        _ocr_progress[dataset_id] = {
            'total': len(images),
            'done': 0,
            'running': True,
            'cancelled': False,
            'stopped': False,
            'results': [],
        }

    def _worker():
        import sqlite3
        # 线程内使用独立数据库连接
        conn = sqlite3.connect(str(DATABASE_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        has_stopped = False
        for img in images:
            # 检查是否需要取消或停止
            with _ocr_lock:
                if _ocr_progress[dataset_id].get('cancelled'):
                    break
                if _ocr_progress[dataset_id].get('stopped'):
                    has_stopped = True

            skipped = has_stopped
            ocr_result = {}
            success = False

            if not skipped:
                ocr_result = run_ocr(img['file_path'])
                success = 'error' not in ocr_result and ocr_result.get('success', False)

                if 'error' not in ocr_result:
                    cur.execute('''
                        INSERT INTO ocr_results
                        (alert_image_id, raw_ocr_text, video_id, timestamp,
                         timestamp_seconds, success, full_result)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        img['id'],
                        ocr_result.get('raw_ocr_text'),
                        ocr_result.get('video_id'),
                        ocr_result.get('timestamp'),
                        ocr_result.get('timestamp_seconds'),
                        ocr_result.get('success', False),
                        json.dumps(ocr_result, ensure_ascii=False)
                    ))
                    conn.commit()

                # 如果启用了 stop_on_failure 且识别失败，停止后续处理
                if stop_on_failure and not success:
                    with _ocr_lock:
                        _ocr_progress[dataset_id]['stopped'] = True
                    has_stopped = True

            with _ocr_lock:
                prog = _ocr_progress[dataset_id]
                prog['done'] += 1
                prog['results'].append({
                    'image_id': img['id'],
                    'success': success,
                    'skipped': skipped,
                    'video_id': ocr_result.get('video_id') if not skipped else None,
                    'timestamp': ocr_result.get('timestamp') if not skipped else None,
                    'timestamp_seconds': ocr_result.get('timestamp_seconds') if not skipped else None,
                    'raw_ocr_text': ocr_result.get('raw_ocr_text') if not skipped else None,
                    'error': ocr_result.get('error') if not skipped else ('跳过' if skipped else None),
                })

        conn.close()
        with _ocr_lock:
            _ocr_progress[dataset_id]['running'] = False

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()

    return jsonify({'success': True, 'total': len(images)})


@bp.route('/api/datasets/<int:dataset_id>/ocr/cancel', methods=['POST'])
def ocr_cancel(dataset_id):
    """中断批量 OCR（已成功的保留）"""
    with _ocr_lock:
        prog = _ocr_progress.get(dataset_id)
        if prog and prog.get('running'):
            prog['cancelled'] = True
    return jsonify({'success': True})


@bp.route('/api/datasets/<int:dataset_id>/ocr/status', methods=['GET'])
def ocr_status(dataset_id):
    """查询批量 OCR 进度"""
    with _ocr_lock:
        prog = _ocr_progress.get(dataset_id)
    if not prog:
        return jsonify({'error': '没有正在进行的 OCR 任务'}), 404
    return jsonify({
        'total': prog['total'],
        'done': prog['done'],
        'running': prog['running'],
        'cancelled': prog.get('cancelled', False),
        'stopped': prog.get('stopped', False),
        'results': prog['results'],
    })
