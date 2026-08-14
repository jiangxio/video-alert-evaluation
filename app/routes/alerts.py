"""告警数据集相关路由"""
from flask import Blueprint, request, jsonify, render_template, current_app, after_this_request
from pathlib import Path
import json
import os
import threading
import zipfile
import tarfile
import tempfile
import shutil

from app.database import get_db, DATABASE_PATH
from app.services.verification_service import (
    parse_alert_config, extract_alert_type_id, run_ocr
)
from app.routes import send_file_with_cache, send_image_with_thumbnail
from app.utils import allowed_file

bp = Blueprint('alerts', __name__, url_prefix='/alerts')

# 批量 OCR 进度状态（内存存储，key=dataset_id）
_ocr_progress = {}
_ocr_lock = threading.Lock()


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
    config_path = Path(current_app.config['ALERT_TYPES_CONFIG'])
    if config_path.exists():
        return parse_alert_config(str(config_path))
    return {}


def _log_image_action(db, dataset_id, action, image_count, details=None):
    """记录数据集图片操作日志"""
    cursor = db.cursor()
    cursor.execute('''
        INSERT INTO dataset_image_logs (dataset_id, action, image_count, details)
        VALUES (?, ?, ?, ?)
    ''', (dataset_id, action, image_count, details))
    db.commit()


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
    rows = [dict(r) for r in cursor.fetchall()]
    # 加载每个数据集关联的算法版本
    for row in rows:
        cursor.execute('''
            SELECT av.id, av.algorithm_type, av.name, av.version_date
            FROM dataset_algorithm_versions dav
            JOIN algorithm_versions av ON dav.algorithm_version_id = av.id
            WHERE dav.dataset_id = ? AND dav.is_active = 1
            ORDER BY av.algorithm_type
        ''', (row['id'],))
        row['algorithm_versions'] = [dict(v) for v in cursor.fetchall()]
    return jsonify(rows)


@bp.route('/api/datasets', methods=['POST'])
def create_dataset():
    data = request.get_json() or {}
    name = data.get('name', '').strip()
    notes = data.get('notes', '').strip()
    mode = data.get('mode', 'normal')
    algorithm_version_ids = data.get('algorithm_version_ids', [])
    if not name:
        return jsonify({'error': '数据集名称不能为空'}), 400

    db = get_db()
    cursor = db.cursor()
    cursor.execute('INSERT INTO datasets (name, notes, mode) VALUES (?, ?, ?)', (name, notes or None, mode))
    db.commit()
    dataset_id = cursor.lastrowid

    # 关联算法版本
    if algorithm_version_ids:
        _set_dataset_algorithm_versions(db, dataset_id, algorithm_version_ids)

    cursor.execute('SELECT id, name, notes, mode, created_at FROM datasets WHERE id = ?', (dataset_id,))
    row = dict(cursor.fetchone())
    row['image_count'] = 0
    return jsonify({'success': True, 'dataset': row})


def _set_dataset_algorithm_versions(db, dataset_id, algorithm_version_ids):
    """设置数据集关联的算法版本（先禁用旧的，再添加新的）"""
    cursor = db.cursor()
    # 1. 标记旧记录为历史
    cursor.execute(
        'UPDATE dataset_algorithm_versions SET is_active = 0 WHERE dataset_id = ?',
        (dataset_id,)
    )
    # 2. 添加新记录
    for vid in algorithm_version_ids:
        cursor.execute(
            'INSERT INTO dataset_algorithm_versions (dataset_id, algorithm_version_id, is_active) VALUES (?, ?, 1)',
            (dataset_id, int(vid))
        )
    db.commit()


def _get_dataset_algorithm_versions(db, dataset_id):
    """获取数据集当前启用的算法版本"""
    cursor = db.cursor()
    cursor.execute('''
        SELECT av.id, av.algorithm_type, av.name, av.version_date
        FROM dataset_algorithm_versions dav
        JOIN algorithm_versions av ON dav.algorithm_version_id = av.id
        WHERE dav.dataset_id = ? AND dav.is_active = 1
        ORDER BY av.algorithm_type
    ''', (dataset_id,))
    return [dict(v) for v in cursor.fetchall()]


def _validate_algorithm_versions(db, algorithm_version_ids):
    """校验算法版本：每种类型只能选一个，返回 (ok, error_msg)"""
    if not algorithm_version_ids:
        return True, None
    cursor = db.cursor()
    placeholders = ','.join('?' * len(algorithm_version_ids))
    cursor.execute(
        f'SELECT id, algorithm_type FROM algorithm_versions WHERE id IN ({placeholders})',
        algorithm_version_ids
    )
    type_map = {}
    for row in cursor.fetchall():
        t = row['algorithm_type']
        if t in type_map:
            return False, f"算法类型 '{t}' 选择了多个版本（ID {type_map[t]} 和 {row['id']}），每种类型只能选一个"
        type_map[t] = row['id']
    return True, None


@bp.route('/api/datasets/<int:dataset_id>/algorithm-versions', methods=['GET'])
def get_dataset_algorithm_versions(dataset_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id FROM datasets WHERE id = ?', (dataset_id,))
    if not cursor.fetchone():
        return jsonify({'error': '数据集不存在'}), 404
    return jsonify(_get_dataset_algorithm_versions(db, dataset_id))


@bp.route('/api/datasets/<int:dataset_id>/algorithm-versions', methods=['POST'])
def set_dataset_algorithm_versions(dataset_id):
    data = request.get_json() or {}
    algorithm_version_ids = data.get('algorithm_version_ids', [])

    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id FROM datasets WHERE id = ?', (dataset_id,))
    if not cursor.fetchone():
        return jsonify({'error': '数据集不存在'}), 404

    # 校验
    ok, err = _validate_algorithm_versions(db, algorithm_version_ids)
    if not ok:
        return jsonify({'error': err}), 400

    _set_dataset_algorithm_versions(db, dataset_id, algorithm_version_ids)
    return jsonify({'success': True, 'algorithm_versions': _get_dataset_algorithm_versions(db, dataset_id)})


@bp.route('/api/datasets/<int:dataset_id>/mode', methods=['PUT'])
def update_dataset_mode(dataset_id):
    """切换数据集模式（normal 或 realtime）"""
    data = request.get_json() or {}
    mode = data.get('mode')
    if mode not in ('normal', 'realtime'):
        return jsonify({'error': '无效的模式，必须是 normal 或 realtime'}), 400

    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id FROM datasets WHERE id = ?', (dataset_id,))
    if not cursor.fetchone():
        return jsonify({'error': '数据集不存在'}), 404

    cursor.execute('UPDATE datasets SET mode = ? WHERE id = ?', (mode, dataset_id))
    db.commit()
    return jsonify({'success': True, 'mode': mode})


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


@bp.route('/api/datasets/<int:dataset_id>/download', methods=['POST'])
def download_dataset(dataset_id):
    """打包下载数据集全部图片"""
    db = get_db()
    cursor = db.cursor()

    cursor.execute('SELECT id, name FROM datasets WHERE id = ?', (dataset_id,))
    dataset = cursor.fetchone()
    if not dataset:
        return jsonify({'error': '数据集不存在'}), 404

    cursor.execute('SELECT file_path, filename FROM alert_images WHERE dataset_id = ?', (dataset_id,))
    images = cursor.fetchall()
    if not images:
        return jsonify({'error': '数据集为空'}), 404

    tmp_fd, tmp_path = tempfile.mkstemp(suffix='.zip')
    os.close(tmp_fd)

    try:
        added = 0
        with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_STORED) as zf:
            for img in images:
                file_path = Path(img['file_path'])
                if file_path.exists():
                    zf.write(str(file_path), img['filename'])
                    added += 1

        if added == 0:
            os.unlink(tmp_path)
            return jsonify({'error': '没有可下载的图片文件'}), 404

        @after_this_request
        def cleanup(response):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            return response

        dataset_name = dataset['name'] or f'dataset_{dataset_id}'
        return send_file_with_cache(
            tmp_path,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f'{dataset_name}_images.zip'
        )

    except Exception as e:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return jsonify({'error': str(e)}), 500


def _extract_archive(archive_path, dest_dir, filename=None):
    """根据扩展名自动解压 zip / tar / tar.gz 到目标目录"""
    name = (filename or archive_path).lower()
    if name.endswith('.zip'):
        with zipfile.ZipFile(archive_path, 'r') as zf:
            zf.extractall(dest_dir)
    elif name.endswith('.tar'):
        with tarfile.open(archive_path, 'r:') as tf:
            tf.extractall(dest_dir)
    elif name.endswith('.tar.gz') or name.endswith('.tgz'):
        with tarfile.open(archive_path, 'r:gz') as tf:
            tf.extractall(dest_dir)
    else:
        raise ValueError('不支持的压缩格式')


def _find_image_root(base_dir):
    """如果 base_dir 下只有一个子目录且没有文件，则返回该子目录"""
    base = Path(base_dir)
    entries = [e for e in base.iterdir() if e.name != '__MACOSX']
    dirs = [e for e in entries if e.is_dir()]
    files = [e for e in entries if e.is_file()]
    if len(dirs) == 1 and not files:
        return dirs[0]
    return base


@bp.route('/api/datasets/<int:dataset_id>/import', methods=['POST'])
def import_zip(dataset_id):
    """从压缩包（zip / tar / tar.gz）导入图片到数据集"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id FROM datasets WHERE id = ?', (dataset_id,))
    if not cursor.fetchone():
        return jsonify({'error': '数据集不存在'}), 404

    if 'file' not in request.files:
        return jsonify({'error': '没有上传文件'}), 400
    f = request.files['file']
    fname = f.filename.lower()
    supported = ('.zip', '.tar', '.tar.gz', '.tgz')
    if not any(fname.endswith(ext) for ext in supported):
        return jsonify({'error': '仅支持 .zip / .tar / .tar.gz / .tgz 格式'}), 400

    config = _load_alert_config()
    IMAGE_EXTS = current_app.config['ALLOWED_IMAGE_EXTENSIONS']

    tmp_dir = tempfile.mkdtemp()
    try:
        archive_path = os.path.join(tmp_dir, 'upload')
        f.save(archive_path)
        try:
            _extract_archive(archive_path, tmp_dir, f.filename)
        except (zipfile.BadZipFile, tarfile.TarError, ValueError) as e:
            # 损坏或不支持的压缩包：明确 400 拒绝，不让异常逃逸成 500
            return jsonify({'error': f'压缩包解析失败：{type(e).__name__}'}), 400
        except Exception as e:
            return jsonify({'error': f'解压失败：{type(e).__name__}'}), 400

        # 定位图片搜索根目录（处理压缩包内套单层文件夹的情况）
        search_root = _find_image_root(tmp_dir)

        imported, skipped = [], []

        for src in sorted(search_root.rglob('*')):
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

            dataset_dir = Path(current_app.config['UPLOAD_ALERTS']) / str(dataset_id)
            dataset_dir.mkdir(parents=True, exist_ok=True)
            dest = dataset_dir / filename
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

    details = f'跳过 {len(skipped)} 张' if skipped else None
    _log_image_action(db, dataset_id, 'import', len(imported), details)

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
    cursor.execute('SELECT id, name, notes, mode, created_at FROM datasets WHERE id = ?', (dataset_id,))
    dataset = cursor.fetchone()
    if not dataset:
        return '数据集不存在', 404
    dataset_dict = dict(dataset)
    # 确保 created_at 为字符串，避免模板切片报错
    if hasattr(dataset_dict.get('created_at'), 'strftime'):
        dataset_dict['created_at'] = dataset_dict['created_at'].strftime('%Y-%m-%d %H:%M:%S')
    else:
        dataset_dict['created_at'] = str(dataset_dict.get('created_at', ''))
    # 确保 mode 有默认值
    if not dataset_dict.get('mode'):
        dataset_dict['mode'] = 'normal'
    return render_template('dataset_detail.html', dataset=dataset_dict)


@bp.route('/api/datasets/<int:dataset_id>/images', methods=['GET'])
def list_dataset_images(dataset_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id FROM datasets WHERE id = ?', (dataset_id,))
    if not cursor.fetchone():
        return jsonify({'error': '数据集不存在'}), 404

    cursor.execute(
        'SELECT id, filename, file_path, alert_type_id, alert_type, file_size, uploaded_at, dataset_id, image_width, image_height, event_label FROM alert_images WHERE dataset_id = ? ORDER BY uploaded_at ASC',
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


# ── 告警评测集管理 ───────────────────────────────────────────────────────────

def _parse_id_list(raw):
    if not raw:
        return []
    try:
        ids = json.loads(raw)
        return ids if isinstance(ids, list) else []
    except Exception:
        return []


@bp.route('/api/eval-sets', methods=['GET'])
def list_alert_eval_sets():
    """获取所有告警评测集"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, name, notes, dataset_ids, created_at FROM eval_alert_sets ORDER BY created_at DESC')

    result = []
    for row in cursor.fetchall():
        item = dict(row)
        dataset_ids = _parse_id_list(item.get('dataset_ids'))
        item['dataset_ids'] = dataset_ids
        item['dataset_count'] = len(dataset_ids)
        item['image_count'] = 0
        item['dataset_names'] = []
        if dataset_ids:
            placeholders = ','.join('?' for _ in dataset_ids)
            cursor.execute(f'''
                SELECT d.id, d.name, COUNT(a.id) AS image_count
                FROM datasets d
                LEFT JOIN alert_images a ON a.dataset_id = d.id
                WHERE d.id IN ({placeholders})
                GROUP BY d.id
            ''', dataset_ids)
            rows = cursor.fetchall()
            item['image_count'] = sum(r['image_count'] or 0 for r in rows)
            names_by_id = {r['id']: r['name'] for r in rows}
            item['dataset_names'] = [names_by_id.get(i) for i in dataset_ids if names_by_id.get(i)]
        result.append(item)

    return jsonify({'sets': result})


@bp.route('/api/eval-sets', methods=['POST'])
def create_alert_eval_set():
    """创建新的告警评测集"""
    data = request.get_json() or {}
    name = data.get('name', '').strip()
    dataset_ids = data.get('dataset_ids', [])

    if not name:
        return jsonify({'error': '评测集名称不能为空'}), 400
    if not isinstance(dataset_ids, list):
        dataset_ids = []

    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        'INSERT INTO eval_alert_sets (name, notes, dataset_ids) VALUES (?, ?, ?)',
        (name, data.get('notes', ''), json.dumps(dataset_ids))
    )
    db.commit()

    return jsonify({'success': True, 'id': cursor.lastrowid})


@bp.route('/api/eval-sets/batch-add', methods=['POST'])
def batch_add_to_alert_eval_set():
    """批量添加告警数据集到评测集"""
    data = request.get_json() or {}
    set_id = data.get('set_id')
    dataset_ids = data.get('dataset_ids', [])

    if not set_id:
        return jsonify({'error': '请选择评测集'}), 400
    if not dataset_ids:
        return jsonify({'error': '请选择要添加的数据集'}), 400

    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, name, notes, dataset_ids, created_at FROM eval_alert_sets WHERE id = ?', (set_id,))
    eval_set = cursor.fetchone()
    if not eval_set:
        return jsonify({'error': '评测集不存在'}), 404

    current_ids = _parse_id_list(eval_set['dataset_ids'])
    added_count = 0
    for dataset_id in dataset_ids:
        if dataset_id not in current_ids:
            current_ids.append(dataset_id)
            added_count += 1

    cursor.execute(
        'UPDATE eval_alert_sets SET dataset_ids = ? WHERE id = ?',
        (json.dumps(current_ids), set_id)
    )
    db.commit()
    return jsonify({'success': True, 'added_count': added_count})


@bp.route('/api/eval-sets/batch-remove', methods=['POST'])
def batch_remove_from_alert_eval_set():
    """批量从告警评测集移出数据集"""
    data = request.get_json() or {}
    set_id = data.get('set_id')
    dataset_ids = data.get('dataset_ids', [])

    if not set_id:
        return jsonify({'error': '请选择评测集'}), 400
    if not dataset_ids:
        return jsonify({'error': '请选择要移出的数据集'}), 400

    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, name, notes, dataset_ids, created_at FROM eval_alert_sets WHERE id = ?', (set_id,))
    eval_set = cursor.fetchone()
    if not eval_set:
        return jsonify({'error': '评测集不存在'}), 404

    current_ids = _parse_id_list(eval_set['dataset_ids'])
    removed_count = 0
    for dataset_id in dataset_ids:
        if dataset_id in current_ids:
            current_ids.remove(dataset_id)
            removed_count += 1

    cursor.execute(
        'UPDATE eval_alert_sets SET dataset_ids = ? WHERE id = ?',
        (json.dumps(current_ids), set_id)
    )
    db.commit()
    return jsonify({'success': True, 'removed_count': removed_count})


@bp.route('/api/eval-sets/<int:set_id>', methods=['PUT'])
def rename_alert_eval_set(set_id):
    """重命名告警评测集"""
    data = request.get_json() or {}
    new_name = data.get('name', '').strip()
    if not new_name:
        return jsonify({'error': '名称不能为空'}), 400

    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id FROM eval_alert_sets WHERE id = ?', (set_id,))
    if not cursor.fetchone():
        return jsonify({'error': '评测集不存在'}), 404

    cursor.execute('UPDATE eval_alert_sets SET name = ? WHERE id = ?', (new_name, set_id))
    db.commit()
    return jsonify({'success': True})


@bp.route('/api/eval-sets/<int:set_id>', methods=['DELETE'])
def delete_alert_eval_set(set_id):
    """删除告警评测集"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id FROM eval_alert_sets WHERE id = ?', (set_id,))
    if not cursor.fetchone():
        return jsonify({'error': '评测集不存在'}), 404

    cursor.execute('DELETE FROM eval_alert_sets WHERE id = ?', (set_id,))
    db.commit()
    return jsonify({'success': True})


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

        dataset_dir = Path(current_app.config['UPLOAD_ALERTS']) / str(dataset_id)
        dataset_dir.mkdir(parents=True, exist_ok=True)
        save_path = dataset_dir / filename
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

    details = '; '.join(errors) if errors else None
    _log_image_action(db, dataset_id, 'upload', len(results), details)

    return jsonify({'success': True, 'uploaded': results, 'errors': errors})


# ── 图片操作 ──────────────────────────────────────────────────────────────────

@bp.route('/api/images/<int:image_id>', methods=['GET'])
def get_image_detail(image_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, filename, file_path, alert_type_id, alert_type, file_size, uploaded_at, dataset_id, image_width, image_height, event_label FROM alert_images WHERE id = ?', (image_id,))
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
    """提供告警图片文件，支持 ?w= 和 ?h= 生成缩略图。"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT file_path, filename FROM alert_images WHERE id = ?', (image_id,))
    row = cursor.fetchone()
    if not row:
        return jsonify({'error': '图片不存在'}), 404
    path = Path(row['file_path'])
    if not path.exists():
        return jsonify({'error': '文件不存在于磁盘'}), 404
    max_w = request.args.get('w', type=int)
    max_h = request.args.get('h', type=int)
    if max_w or max_h:
        return send_image_with_thumbnail(str(path), max_width=max_w, max_height=max_h)
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


@bp.route('/api/datasets/<int:dataset_id>/images/batch-delete', methods=['POST'])
def batch_delete_images(dataset_id):
    """批量删除数据集图片，支持按视频ID和事件类型筛选"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id FROM datasets WHERE id = ?', (dataset_id,))
    if not cursor.fetchone():
        return jsonify({'error': '数据集不存在'}), 404

    data = request.get_json() or {}
    video_id = data.get('video_id', '').strip()
    event_type = data.get('event_type', '').strip()

    # 构建查询条件
    conditions = ['dataset_id = ?']
    params = [dataset_id]

    if video_id:
        # 按视频ID筛选（从OCR结果中查找）
        conditions.append('''
            id IN (
                SELECT alert_image_id FROM ocr_results 
                WHERE video_id = ?
            )
        ''')
        params.append(video_id)
    
    if event_type:
        conditions.append('alert_type = ?')
        params.append(event_type)

    # 查询符合条件的图片
    where_clause = ' AND '.join(conditions)
    cursor.execute(f'''
        SELECT id, file_path FROM alert_images 
        WHERE {where_clause}
    ''', params)
    images = cursor.fetchall()

    if not images:
        return jsonify({'error': '没有找到符合条件的图片'}), 404

    # 删除文件和数据库记录
    deleted_count = 0
    for row in images:
        try:
            os.unlink(row['file_path'])
        except Exception:
            pass
        deleted_count += 1

    cursor.execute(f'''
        DELETE FROM alert_images 
        WHERE {where_clause}
    ''', params)
    db.commit()

    details_parts = []
    if video_id:
        details_parts.append(f'视频ID: {video_id}')
    if event_type:
        details_parts.append(f'事件类型: {event_type}')
    details = '; '.join(details_parts) if details_parts else None
    _log_image_action(db, dataset_id, 'batch_delete', deleted_count, details)

    return jsonify({
        'success': True,
        'deleted_count': deleted_count
    })


@bp.route('/api/datasets/<int:dataset_id>/image-logs', methods=['GET'])
def list_image_logs(dataset_id):
    """获取数据集图片操作日志"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id FROM datasets WHERE id = ?', (dataset_id,))
    if not cursor.fetchone():
        return jsonify({'error': '数据集不存在'}), 404

    cursor.execute('''
        SELECT id, action, image_count, details, created_at
        FROM dataset_image_logs
        WHERE dataset_id = ?
        ORDER BY created_at DESC
        LIMIT 50
    ''', (dataset_id,))
    logs = []
    for row in cursor.fetchall():
        logs.append({
            'id': row['id'],
            'action': row['action'],
            'image_count': row['image_count'],
            'details': row['details'],
            'created_at': row['created_at'],
        })
    return jsonify({'logs': logs})


# ── OCR ───────────────────────────────────────────────────────────────────────

@bp.route('/api/images/<int:image_id>/ocr', methods=['POST'])
def ocr_single(image_id):
    """对单张图片执行 OCR"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, filename, file_path, alert_type_id, alert_type, file_size, uploaded_at, dataset_id, image_width, image_height, event_label FROM alert_images WHERE id = ?', (image_id,))
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

    # 获取参数：默认只对标注成功以外的图片执行OCR
    force_all = False
    stop_on_failure = False
    if request.is_json:
        data = request.get_json() or {}
        force_all = data.get('force_all', False)
        stop_on_failure = data.get('stop_on_failure', False)

    if force_all:
        cursor.execute(
            'SELECT id, file_path FROM alert_images WHERE dataset_id = ?',
            (dataset_id,)
        )
    else:
        cursor.execute(
            '''SELECT id, file_path FROM alert_images WHERE dataset_id = ?
               AND id NOT IN (
                   SELECT alert_image_id FROM ocr_results WHERE success = 1
               )''',
            (dataset_id,)
        )
    images = [dict(r) for r in cursor.fetchall()]
    if not images:
        return jsonify({'error': '没有需要OCR的图片（所有图片已标注成功）'}), 400

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
