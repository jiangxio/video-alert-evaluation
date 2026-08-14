import os
import re
import json
import uuid
import sqlite3
from pathlib import Path
from datetime import datetime

from flask import Flask, request, jsonify, send_from_directory, render_template, abort
from werkzeug.utils import secure_filename
from PIL import Image

import config

app = Flask(__name__, template_folder='templates', static_folder='static')

VALID_NAME = re.compile(r'^[A-Za-z0-9_.-]+$')
DB_FILE = os.path.join(config.DB_DIR, 'annotations.db')


# ── Database initialization ─────────────────────────────────
def init_db():
    """Initialize database schema if it doesn't exist."""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()

        # Projects table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                classes TEXT NOT NULL,
                images_dir TEXT NOT NULL,
                labels_dir TEXT NOT NULL,
                labels_format TEXT DEFAULT 'json',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        ''')

        # Images table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                name TEXT NOT NULL,
                filename TEXT NOT NULL,
                image_path TEXT NOT NULL,
                image_width INTEGER,
                image_height INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id),
                UNIQUE(project_id, name)
            )
        ''')

        # Labels table (latest state)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS labels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                image_id INTEGER NOT NULL,
                image_path TEXT,
                image_width INTEGER,
                image_height INTEGER,
                shapes TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id),
                FOREIGN KEY (image_id) REFERENCES images(id),
                UNIQUE(project_id, image_id)
            )
        ''')

        # Label backups table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS label_backups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                image_id INTEGER NOT NULL,
                shapes TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id),
                FOREIGN KEY (image_id) REFERENCES images(id)
            )
        ''')

        # Evaluation results table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS eval_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                name TEXT NOT NULL,
                pred_dir TEXT NOT NULL,
                conf_threshold REAL NOT NULL,
                iou_threshold REAL NOT NULL,
                metrics TEXT NOT NULL,
                images TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id)
            )
        ''')

        conn.commit()


def get_db():
    """Get database connection."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


# ── Project DB helpers ─────────────────────────────────────
def db_get_projects():
    """Get all projects from database."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM projects ORDER BY created_at DESC')
        rows = cursor.fetchall()
        projects = []
        for row in rows:
            p = dict(row)
            p['classes'] = json.loads(p['classes'])
            projects.append(p)
        return projects


def db_get_project(project_id):
    """Get single project by id."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM projects WHERE id = ?', (project_id,))
        row = cursor.fetchone()
        if not row:
            return None
        p = dict(row)
        p['classes'] = json.loads(p['classes'])
        return p


def db_insert_project(project):
    """Insert new project into database."""
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        cursor.execute('''
            INSERT INTO projects (id, name, classes, images_dir, labels_dir, labels_format, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            project['id'],
            project['name'],
            json.dumps(project['classes'], ensure_ascii=False),
            project['images_dir'],
            project['labels_dir'],
            project.get('labels_format', 'json'),
            now,
            now
        ))
        conn.commit()
    return db_get_project(project['id'])


def db_update_project(project_id, updates):
    """Update existing project."""
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.now().isoformat()

        set_clauses = []
        params = []

        if 'name' in updates:
            set_clauses.append('name = ?')
            params.append(updates['name'])
        if 'classes' in updates:
            set_clauses.append('classes = ?')
            params.append(json.dumps(updates['classes'], ensure_ascii=False))
        if 'images_dir' in updates:
            set_clauses.append('images_dir = ?')
            params.append(updates['images_dir'])
        if 'labels_dir' in updates:
            set_clauses.append('labels_dir = ?')
            params.append(updates['labels_dir'])
        if 'labels_format' in updates:
            set_clauses.append('labels_format = ?')
            params.append(updates['labels_format'])

        set_clauses.append('updated_at = ?')
        params.append(now)
        params.append(project_id)

        if len(set_clauses) > 1:
            cursor.execute(f'''
                UPDATE projects SET {', '.join(set_clauses)} WHERE id = ?
            ''', params)
            conn.commit()

    return db_get_project(project_id)


def db_delete_project(project_id):
    """Delete project (cascades via foreign keys if configured, but we do explicit cleanup)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM label_backups WHERE project_id = ?', (project_id,))
        cursor.execute('DELETE FROM labels WHERE project_id = ?', (project_id,))
        cursor.execute('DELETE FROM images WHERE project_id = ?', (project_id,))
        cursor.execute('DELETE FROM projects WHERE id = ?', (project_id,))
        conn.commit()


# ── Image DB helpers ───────────────────────────────────────
def db_list_images(project_id):
    """Get all images for a project, with label status."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT i.*,
                   CASE WHEN l.id IS NOT NULL THEN 1 ELSE 0 END AS has_label
            FROM images i
            LEFT JOIN labels l ON i.id = l.image_id AND i.project_id = l.project_id
            WHERE i.project_id = ?
            ORDER BY i.name
        ''', (project_id,))
        rows = cursor.fetchall()
        result = []
        for row in rows:
            r = dict(row)
            result.append({
                'name': r['name'],
                'filename': r['filename'],
                'has_label': bool(r['has_label'])
            })
        return result


def db_get_image(project_id, name):
    """Get single image by project_id and base name."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM images WHERE project_id = ? AND name = ?', (project_id, name))
        row = cursor.fetchone()
        return dict(row) if row else None


def db_get_image_by_id(image_id):
    """Get single image by id."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM images WHERE id = ?', (image_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def db_insert_image(project_id, name, filename, image_path, image_width=None, image_height=None):
    """Insert or update image in database."""
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        try:
            cursor.execute('''
                INSERT INTO images (project_id, name, filename, image_path, image_width, image_height, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (project_id, name, filename, image_path, image_width, image_height, now))
        except sqlite3.IntegrityError:
            cursor.execute('''
                UPDATE images
                SET filename = ?, image_path = ?, image_width = ?, image_height = ?
                WHERE project_id = ? AND name = ?
            ''', (filename, image_path, image_width, image_height, project_id, name))
        conn.commit()
        return db_get_image(project_id, name)


def db_delete_image(project_id, name):
    """Delete image and associated labels/backups."""
    img = db_get_image(project_id, name)
    if not img:
        return
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM label_backups WHERE project_id = ? AND image_id = ?', (project_id, img['id']))
        cursor.execute('DELETE FROM labels WHERE project_id = ? AND image_id = ?', (project_id, img['id']))
        cursor.execute('DELETE FROM images WHERE id = ?', (img['id'],))
        conn.commit()


# ── Label DB helpers ───────────────────────────────────────
def db_get_label(project_id, image_name):
    """Get label for an image by project and image name."""
    img = db_get_image(project_id, image_name)
    if not img:
        return None
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM labels WHERE project_id = ? AND image_id = ?', (project_id, img['id']))
        row = cursor.fetchone()
        if not row:
            return None
        lbl = dict(row)
        lbl['shapes'] = json.loads(lbl['shapes'])
        return lbl


def db_save_label(project_id, image_name, shapes, image_path=None, image_width=None, image_height=None):
    """Save label (create or update), creating backup first if exists."""
    img = db_get_image(project_id, image_name)
    if not img:
        return None

    # Create backup of existing label
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM labels WHERE project_id = ? AND image_id = ?', (project_id, img['id']))
        existing = cursor.fetchone()
        if existing:
            cursor.execute('''
                INSERT INTO label_backups (project_id, image_id, shapes, created_at)
                VALUES (?, ?, ?, ?)
            ''', (project_id, img['id'], existing['shapes'], datetime.now().isoformat()))

        now = datetime.now().isoformat()
        shapes_json = json.dumps(shapes, ensure_ascii=False)

        try:
            cursor.execute('''
                INSERT INTO labels (project_id, image_id, image_path, image_width, image_height, shapes, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (project_id, img['id'], image_path, image_width, image_height, shapes_json, now))
        except sqlite3.IntegrityError:
            cursor.execute('''
                UPDATE labels
                SET image_path = ?, image_width = ?, image_height = ?, shapes = ?, updated_at = ?
                WHERE project_id = ? AND image_id = ?
            ''', (image_path, image_width, image_height, shapes_json, now, project_id, img['id']))

        conn.commit()

    return db_get_label(project_id, image_name)


# ── Helpers (file system, safety) ─────────────────────────
def safe_basename(name):
    if not name or not VALID_NAME.match(name):
        return None
    return name


def get_image_path_from_filesystem(name, images_dir):
    """Fallback: resolve image file from filesystem with multiple extensions."""
    safe = safe_basename(name)
    if not safe:
        return None
    candidate = Path(images_dir) / safe
    if candidate.exists():
        return candidate
    for ext in config.IMAGE_EXTS:
        candidate = Path(images_dir) / f"{safe}{ext}"
        if candidate.exists():
            return candidate
    return None


def sync_images_from_filesystem(project_id, images_dir):
    """Sync image records from filesystem to database."""
    if not os.path.exists(images_dir):
        return

    try:
        image_files = sorted([f for f in os.listdir(images_dir)
                               if f.lower().endswith(tuple(config.IMAGE_EXTS))])
    except (FileNotFoundError, PermissionError):
        return

    for filename in image_files:
        base, _ = os.path.splitext(filename)
        img_path = Path(images_dir) / filename
        width, height = None, None
        try:
            with Image.open(img_path) as img:
                width, height = img.size
        except Exception:
            pass
        db_insert_image(project_id, base, filename, str(img_path), width, height)


def make_label_template_from_db(img):
    """Make label template from database image record."""
    if not img:
        return {"imagePath": "", "imageHeight": 0, "imageWidth": 0, "shapes": []}
    return {
        "imagePath": img['filename'],
        "imageHeight": img['image_height'] or 0,
        "imageWidth": img['image_width'] or 0,
        "shapes": []
    }


# ── Project context resolution ─────────────────────────────
def resolve_context():
    """Return (images_dir, labels_dir, classes, project) for the current request."""
    project_id = request.args.get('project_id', '')
    if not project_id:
        try:
            project_id = (request.get_json(force=True, silent=True) or {}).get('project_id', '')
        except Exception:
            pass
    if not project_id:
        project_id = request.form.get('project_id', '')

    if project_id:
        p = db_get_project(project_id)
        if p:
            return p['images_dir'], p['labels_dir'], p['classes'], p
    return config.IMAGES_DIR, config.LABELS_DIR, config.CLASSES, None


# ── Format import helper (YOLO only) ───────────────────────
def _import_labels(images_dir, labels_dir, fmt, project_id=None, project_classes=None):
    """Import YOLO labels from filesystem into DB (if project_id provided)."""
    if fmt != 'yolo':
        return 0
    if not os.path.exists(labels_dir):
        return 0
    if project_classes is None:
        project_classes = config.CLASSES

    imported = 0
    for txt_file in sorted(f for f in os.listdir(labels_dir)
                           if f.endswith('.txt') and f != 'classes.txt'):
        base = os.path.splitext(txt_file)[0]
        img_path = get_image_path_from_filesystem(base, images_dir)
        if not img_path:
            continue
        try:
            with Image.open(img_path) as im:
                W, H = im.size
            with open(os.path.join(labels_dir, txt_file), 'r', encoding='utf-8') as f:
                lines = [l.strip() for l in f if l.strip()]
            shapes = []
            for line in lines:
                parts = line.split()
                if len(parts) != 5:
                    continue
                cls_id = int(parts[0])
                if cls_id < 0 or cls_id >= len(project_classes):
                    continue
                cx, cy, bw, bh = map(float, parts[1:])
                shapes.append({
                    "class_idx": cls_id,
                    "points": [[(cx-bw/2)*W, (cy-bh/2)*H],
                               [(cx+bw/2)*W, (cy+bh/2)*H]],
                    "group_id": None, "shape_type": "rectangle", "flags": {}
                })
            if project_id:
                sync_images_from_filesystem(project_id, images_dir)
                db_save_label(project_id, base, shapes, img_path.name, W, H)
            imported += 1
        except Exception:
            continue

    return imported


# ── Pages ──────────────────────────────────────────────────
@app.route('/')
def home():
    return render_template('home.html', base_dir=config.BASE_DIR)


@app.route('/project/<project_id>')
def project_page(project_id):
    p = db_get_project(project_id)
    if not p:
        abort(404)
    # Sync images from filesystem when entering project
    sync_images_from_filesystem(project_id, p['images_dir'])
    return render_template('index.html',
                           classes=p['classes'],
                           base_dir=config.BASE_DIR,
                           project_id=project_id,
                           project_name=p.get('name', ''))


# ── Project API ────────────────────────────────────────────
@app.route('/api/projects', methods=['GET'])
def api_get_projects():
    return jsonify(db_get_projects())


@app.route('/api/projects', methods=['POST'])
def api_create_project():
    payload = request.get_json(force=True) or {}
    name = payload.get('name', '').strip()
    images_dir = payload.get('images_dir', '').strip()
    labels_dir = payload.get('labels_dir', '').strip()
    classes_raw = payload.get('classes', config.CLASSES)
    labels_format = payload.get('labels_format', 'json')

    if not name:
        return jsonify({"error": "name required"}), 400
    if not images_dir:
        return jsonify({"error": "images_dir required"}), 400

    if not os.path.isabs(images_dir):
        images_dir = os.path.normpath(os.path.join(config.BASE_DIR, images_dir))
    else:
        images_dir = os.path.normpath(images_dir)

    if not labels_dir:
        labels_dir = os.path.normpath(os.path.join(images_dir, '..', 'labels'))
    elif not os.path.isabs(labels_dir):
        labels_dir = os.path.normpath(os.path.join(config.BASE_DIR, labels_dir))
    else:
        labels_dir = os.path.normpath(labels_dir)

    classes = classes_raw if isinstance(classes_raw, list) and classes_raw else config.CLASSES

    os.makedirs(images_dir, exist_ok=True)
    os.makedirs(labels_dir, exist_ok=True)

    project = {
        "id": str(uuid.uuid4())[:8],
        "name": name,
        "images_dir": images_dir,
        "labels_dir": labels_dir,
        "classes": classes,
        "labels_format": labels_format
    }

    db_insert_project(project)
    sync_images_from_filesystem(project['id'], images_dir)

    imported_count = None
    if labels_format != 'json':
        imported_count = _import_labels(images_dir, labels_dir, labels_format, project['id'], classes)

    result = dict(project)
    if imported_count is not None:
        result['imported_count'] = imported_count
    return jsonify(result)


@app.route('/api/projects/<project_id>', methods=['DELETE'])
def api_delete_project(project_id):
    db_delete_project(project_id)
    return jsonify({"success": True})


@app.route('/api/projects/<project_id>', methods=['PUT'])
def api_update_project(project_id):
    payload = request.get_json(force=True) or {}
    p = db_get_project(project_id)
    if not p:
        return jsonify({"error": "project not found"}), 404

    # 编辑项目时只允许修改名称和类别
    updates = {}
    if 'name' in payload:
        updates['name'] = payload['name'].strip()
    if 'classes' in payload and isinstance(payload['classes'], list):
        updates['classes'] = payload['classes']

    updated = db_update_project(project_id, updates)
    result = dict(updated or db_get_project(project_id))
    return jsonify(result)


# ── Existing API routes (DB-aware) ────────────────────────
@app.route('/api/browse_dir')
def api_browse_dir():
    path = request.args.get('path', config.BASE_DIR)
    path = os.path.normpath(os.path.abspath(path))
    show_files = request.args.get('show_files', '0') == '1'
    file_ext = request.args.get('ext', '')
    try:
        parent = str(Path(path).parent)
        entries = []
        for entry in sorted(os.scandir(path), key=lambda e: e.name.lower()):
            if entry.is_dir() and not entry.name.startswith('.'):
                entries.append({'name': entry.name, 'path': str(entry.path), 'is_file': False})
            elif show_files and entry.is_file():
                if not file_ext or entry.name.endswith(file_ext):
                    entries.append({'name': entry.name, 'path': str(entry.path), 'is_file': True})
        return jsonify({'path': path, 'parent': parent, 'entries': entries})
    except PermissionError:
        return jsonify({'error': 'Permission denied'}), 403
    except FileNotFoundError:
        return jsonify({'error': 'Path not found'}), 404


@app.route('/api/images')
def api_images():
    images_dir, _, _, project = resolve_context()
    if not project:
        # Fallback to filesystem listing if no project
        result = []
        try:
            image_files = sorted([f for f in os.listdir(images_dir)
                                   if f.lower().endswith(tuple(config.IMAGE_EXTS))])
        except FileNotFoundError:
            image_files = []
        for image_name in image_files:
            base, _ = os.path.splitext(image_name)
            result.append({"name": base, "filename": image_name, "has_label": False})
        return jsonify(result)

    sync_images_from_filesystem(project['id'], images_dir)
    return jsonify(db_list_images(project['id']))


@app.route('/image/<name>')
def image(name):
    images_dir, _, _, project = resolve_context()

    # Try DB first for path
    if project:
        img = db_get_image(project['id'], name)
        if img and os.path.exists(img['image_path']):
            return send_from_directory(os.path.dirname(img['image_path']), os.path.basename(img['image_path']))

    # Fallback to filesystem
    safe = safe_basename(name)
    if not safe:
        abort(404)
    candidate = Path(images_dir) / safe
    if candidate.exists():
        return send_from_directory(images_dir, candidate.name)
    for ext in config.IMAGE_EXTS:
        candidate = Path(images_dir) / f"{safe}{ext}"
        if candidate.exists():
            return send_from_directory(images_dir, candidate.name)
    abort(404)


@app.route('/api/labels/<name>', methods=['GET'])
def api_get_labels(name):
    images_dir, _, _, project = resolve_context()

    if project:
        lbl = db_get_label(project['id'], name)
        if lbl:
            img = db_get_image(project['id'], name)
            # Convert class_idx -> label using project's classes list
            shapes_out = []
            for shape in lbl['shapes']:
                s = dict(shape)
                class_idx = s.pop('class_idx', 0)
                if 0 <= class_idx < len(project['classes']):
                    s['label'] = project['classes'][class_idx]
                else:
                    s['label'] = project['classes'][0] if project['classes'] else 'unknown'
                shapes_out.append(s)
            return jsonify({
                "imagePath": lbl['image_path'] or (img['filename'] if img else name),
                "imageHeight": lbl['image_height'] or (img['image_height'] if img else 0),
                "imageWidth": lbl['image_width'] or (img['image_width'] if img else 0),
                "shapes": shapes_out
            })
        img = db_get_image(project['id'], name)
        if img:
            return jsonify(make_label_template_from_db(img))
        return jsonify(make_label_template_from_db(None))

    # Fallback to filesystem
    safe = safe_basename(name)
    if not safe:
        return jsonify({"error": "invalid name"}), 400

    # Try to get image dimensions
    image_file = get_image_path_from_filesystem(name, images_dir)
    if not image_file:
        return jsonify({"imagePath": "", "imageHeight": 0, "imageWidth": 0, "shapes": []})

    with Image.open(image_file) as img:
        width, height = img.size
    return jsonify({"imagePath": image_file.name, "imageHeight": height, "imageWidth": width, "shapes": []})


@app.route('/api/labels/<name>', methods=['POST'])
def api_post_labels(name):
    images_dir, _, _, project = resolve_context()

    payload = request.get_json(force=True)
    if not payload or not isinstance(payload, dict):
        return jsonify({"error": "invalid JSON body"}), 400
    if 'shapes' not in payload or not isinstance(payload['shapes'], list):
        return jsonify({"error": "shapes is required and must be list"}), 400

    if project:
        img = db_get_image(project['id'], name)
        if not img:
            # Try to add image from filesystem if not present
            sync_images_from_filesystem(project['id'], images_dir)
            img = db_get_image(project['id'], name)
        if not img:
            return jsonify({"error": "image not found in project"}), 404

        # Process shapes: convert label -> class_idx using project's classes list
        shapes_to_save = []
        for shape in payload['shapes']:
            if not isinstance(shape, dict):
                continue
            label = shape.get('label', '').strip()
            if not label:
                continue
            points = shape.get('points')
            if not isinstance(points, list) or len(points) != 2:
                continue
            # Find class_idx, default to 0 if not found
            class_idx = project['classes'].index(label) if label in project['classes'] else 0
            shapes_to_save.append({
                "class_idx": class_idx,
                "points": [[float(points[0][0]), float(points[0][1])],
                           [float(points[1][0]), float(points[1][1])]],
                "group_id": None,
                "shape_type": "rectangle",
                "flags": {}
            })

        db_save_label(project['id'], name, shapes_to_save, img['filename'], img['image_width'], img['image_height'])
        return jsonify({"success": True, "saved_to": "database"})

    # Fallback to filesystem (no project context - legacy)
    return jsonify({"error": "project required for label save"}), 400


@app.route('/api/export/yolo', methods=['POST'])
def api_export_yolo():
    _, _, classes, project = resolve_context()
    payload = request.get_json(force=True) or {}
    out_dir = payload.get('output_dir', os.path.join(config.BASE_DIR, 'yolo_export'))
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    (Path(out_dir) / 'classes.txt').write_text('\n'.join(classes), encoding='utf-8')

    exported = []

    if project:
        images = db_list_images(project['id'])
        for item in images:
            base = item['name']
            lbl = db_get_label(project['id'], base)
            img = db_get_image(project['id'], base)
            if not img:
                continue
            W = img['image_width'] or 0
            H = img['image_height'] or 0
            lines = []
            if lbl and W and H:
                for shape in lbl['shapes']:
                    pts = shape.get('points')
                    cls_id = shape.get('class_idx', 0)
                    if not pts or len(pts) != 2:
                        continue
                    x1, y1 = pts[0]
                    x2, y2 = pts[1]
                    xmin, ymin = min(x1, x2), min(y1, y2)
                    xmax, ymax = max(x1, x2), max(y1, y2)
                    cx = (xmin + xmax) / 2.0 / W
                    cy = (ymin + ymax) / 2.0 / H
                    w = (xmax - xmin) / W
                    h = (ymax - ymin) / H
                    lines.append(f"{cls_id} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")
            (Path(out_dir) / f"{base}.txt").write_text('\n'.join(lines), encoding='utf-8')
            exported.append(base)

    return jsonify({"success": True, "output_dir": out_dir, "exported_count": len(exported)})


@app.route('/api/import/yolo', methods=['POST'])
def api_import_yolo():
    images_dir, _, classes, project = resolve_context()
    if 'file' not in request.files:
        return jsonify({"error": "file parameter required"}), 400
    text = request.files['file'].read().decode('utf-8')
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return jsonify({"error": "empty file"}), 400

    image_name = request.form.get('image_name')
    if not image_name:
        return jsonify({"error": "image_name required"}), 400

    base, ext = os.path.splitext(image_name)
    if ext.lower() not in config.IMAGE_EXTS:
        return jsonify({"error": "image_name must be valid image"}), 400

    if project:
        img = db_get_image(project['id'], base)
        if img:
            W = img['image_width'] or 0
            H = img['image_height'] or 0
        else:
            img_path = get_image_path_from_filesystem(base, images_dir)
            if not img_path:
                return jsonify({"error": "image not found"}), 404
            with Image.open(img_path) as im:
                W, H = im.size
    else:
        img_path = get_image_path_from_filesystem(base, images_dir)
        if not img_path:
            return jsonify({"error": "image not found"}), 404
        with Image.open(img_path) as im:
            W, H = im.size

    shapes = []
    for line in lines:
        parts = line.split()
        if len(parts) != 5:
            continue
        cls_id = int(parts[0])
        if cls_id < 0 or cls_id >= len(classes):
            continue
        cx, cy, w, h = map(float, parts[1:5])
        shapes.append({
            "class_idx": cls_id,
            "points": [[(cx-w/2)*W, (cy-h/2)*H], [(cx+w/2)*W, (cy+h/2)*H]],
            "group_id": None, "shape_type": "rectangle", "flags": {}
        })

    if project:
        db_save_label(project['id'], base, shapes, image_name, W, H)
        return jsonify({"success": True, "saved_to": "database"})

    return jsonify({"success": True})


@app.route('/api/import/yolo_dir', methods=['POST'])
def api_import_yolo_dir():
    images_dir, _, classes, project = resolve_context()
    payload = request.get_json(force=True) or {}
    src_dir = payload.get('src_dir', '').strip()

    if not src_dir or not os.path.isdir(src_dir):
        return jsonify({"error": "src_dir required and must exist"}), 400
    if not project:
        return jsonify({"error": "project required"}), 400

    imported = 0
    skipped = []

    for txt_file in sorted(f for f in os.listdir(src_dir)
                           if f.endswith('.txt') and f != 'classes.txt'):
        base = os.path.splitext(txt_file)[0]
        img = db_get_image(project['id'], base)
        if img:
            W = img['image_width'] or 0
            H = img['image_height'] or 0
        else:
            img_path = get_image_path_from_filesystem(base, images_dir)
            if not img_path:
                skipped.append(base)
                continue
            try:
                with Image.open(img_path) as im:
                    W, H = im.size
            except Exception:
                skipped.append(base)
                continue

        try:
            with open(os.path.join(src_dir, txt_file), 'r', encoding='utf-8') as f:
                lines = [l.strip() for l in f if l.strip()]
            shapes = []
            for line in lines:
                parts = line.split()
                if len(parts) != 5:
                    continue
                cls_id = int(parts[0])
                if cls_id < 0 or cls_id >= len(classes):
                    continue
                cx, cy, bw, bh = map(float, parts[1:])
                shapes.append({
                    "class_idx": cls_id,
                    "points": [[(cx-bw/2)*W, (cy-bh/2)*H],
                               [(cx+bw/2)*W, (cy+bh/2)*H]],
                    "group_id": None, "shape_type": "rectangle", "flags": {}
                })
            db_save_label(project['id'], base, shapes, f"{base}.txt", W, H)
            imported += 1
        except Exception:
            skipped.append(base)
            continue

    return jsonify({"success": True, "imported_count": imported, "skipped": skipped})


@app.route('/api/delete_image/<name>', methods=['DELETE'])
def api_delete_image(name):
    _, _, _, project = resolve_context()
    if project:
        db_delete_image(project['id'], name)
    return jsonify({"success": True})


@app.route('/api/upload_images', methods=['POST'])
def api_upload_images():
    images_dir, _, _, project = resolve_context()
    files = request.files.getlist('images')
    uploaded = 0
    for f in files:
        if f and f.filename:
            safe_name = secure_filename(f.filename)
            f.save(os.path.join(images_dir, safe_name))
            base, _ = os.path.splitext(safe_name)
            if project:
                img_path = os.path.join(images_dir, safe_name)
                width, height = None, None
                try:
                    with Image.open(img_path) as im:
                        width, height = im.size
                except Exception:
                    pass
                db_insert_image(project['id'], base, safe_name, img_path, width, height)
            uploaded += 1
    return jsonify({"success": True, "uploaded": uploaded})


# ── Evaluate page & API ────────────────────────────────────

@app.route('/project/<project_id>/evaluate')
def evaluate_page(project_id):
    p = db_get_project(project_id)
    if not p:
        abort(404)
    return render_template('evaluate.html',
                           classes=p['classes'],
                           base_dir=config.BASE_DIR,
                           project_id=project_id,
                           project_name=p.get('name', ''))


def _compute_iou(pts_a, pts_b):
    ax1, ay1 = pts_a[0]; ax2, ay2 = pts_a[1]
    bx1, by1 = pts_b[0]; bx2, by2 = pts_b[1]
    ax1, ax2 = min(ax1, ax2), max(ax1, ax2)
    ay1, ay2 = min(ay1, ay2), max(ay1, ay2)
    bx1, bx2 = min(bx1, bx2), max(bx1, bx2)
    by1, by2 = min(by1, by2), max(by1, by2)
    inter = max(0, min(ax2, bx2) - max(ax1, bx1)) * max(0, min(ay2, by2) - max(ay1, by1))
    union = (ax2-ax1)*(ay2-ay1) + (bx2-bx1)*(by2-by1) - inter
    return inter / union if union > 0 else 0.0


@app.route('/api/evaluate', methods=['POST'])
def api_evaluate():
    payload = request.get_json(force=True) or {}
    project_id = payload.get('project_id', '')
    pred_dir = payload.get('pred_dir', '').strip()
    conf_threshold = float(payload.get('conf_threshold', 0.25))
    iou_threshold = float(payload.get('iou_threshold', 0.5))
    classes = payload.get('classes', config.CLASSES)

    if not pred_dir or not os.path.isdir(pred_dir):
        return jsonify({"error": "pred_dir required and must exist"}), 400
    if not project_id:
        return jsonify({"error": "project_id required"}), 400

    project = db_get_project(project_id)
    if not project:
        return jsonify({"error": "project not found"}), 404

    pred_files = {os.path.splitext(f)[0]: f for f in os.listdir(pred_dir)
                  if f.endswith('.txt') and f != 'classes.txt'}

    # Union of pred names and all DB images (so images with GT but no pred are included)
    db_image_names = {item['name'] for item in db_list_images(project_id)}
    all_names = sorted(db_image_names | set(pred_files.keys()))

    class_metrics = {cls: {'tp': 0, 'fp': 0, 'fn': 0} for cls in classes}
    images_out = []

    for name in all_names:
        img_row = db_get_image(project_id, name)
        W = (img_row['image_width'] or 640) if img_row else 640
        H = (img_row['image_height'] or 480) if img_row else 480

        # GT from DB labels
        gt_boxes = []
        lbl = db_get_label(project_id, name)
        if lbl:
            for shape in lbl['shapes']:
                cls_id = shape.get('class_idx', 0)
                pts = shape.get('points')
                if pts and len(pts) == 2 and 0 <= cls_id < len(classes):
                    gt_boxes.append({
                        'label': classes[cls_id],
                        'points': pts,
                        'matched': False
                    })

        # Pred from filesystem
        pred_boxes = []
        if name in pred_files:
            try:
                with open(os.path.join(pred_dir, pred_files[name]), 'r') as f:
                    for line in f:
                        parts = line.strip().split()
                        if len(parts) < 5:
                            continue
                        cls_id = int(parts[0])
                        if cls_id < 0 or cls_id >= len(classes):
                            continue
                        cx, cy, bw, bh = map(float, parts[1:5])
                        conf = float(parts[5]) if len(parts) >= 6 else 1.0
                        if conf < conf_threshold:
                            continue
                        pred_boxes.append({
                            'label': classes[cls_id],
                            'points': [[(cx-bw/2)*W, (cy-bh/2)*H],
                                       [(cx+bw/2)*W, (cy+bh/2)*H]],
                            'conf': conf, 'matched': False
                        })
            except Exception:
                pass

        for cls in classes:
            gt_cls = [b for b in gt_boxes if b['label'] == cls]
            pred_cls_sorted = sorted([b for b in pred_boxes if b['label'] == cls],
                                     key=lambda b: b['conf'], reverse=True)
            gt_matched = [False] * len(gt_cls)
            for pred in pred_cls_sorted:
                best_iou, best_idx = iou_threshold, -1
                for i, gt in enumerate(gt_cls):
                    if gt_matched[i]:
                        continue
                    v = _compute_iou(pred['points'], gt['points'])
                    if v >= best_iou:
                        best_iou, best_idx = v, i
                if best_idx >= 0:
                    pred['matched'] = True
                    gt_cls[best_idx]['matched'] = True
                    gt_matched[best_idx] = True
            tp = sum(1 for b in pred_cls_sorted if b['matched'])
            fp = sum(1 for b in pred_cls_sorted if not b['matched'])
            fn = sum(1 for b in gt_cls if not b['matched'])
            class_metrics[cls]['tp'] += tp
            class_metrics[cls]['fp'] += fp
            class_metrics[cls]['fn'] += fn

        img_filename = img_row['filename'] if img_row else name
        images_out.append({'name': name, 'filename': img_filename,
                           'gt_boxes': gt_boxes, 'pred_boxes': pred_boxes})

    metrics = {}
    total_tp = total_fp = total_fn = 0
    for cls in classes:
        tp = class_metrics[cls]['tp']
        fp = class_metrics[cls]['fp']
        fn = class_metrics[cls]['fn']
        metrics[cls] = {
            'precision': tp / (tp + fp) if (tp + fp) > 0 else 0.0,
            'recall': tp / (tp + fn) if (tp + fn) > 0 else 0.0,
            'tp': tp, 'fp': fp, 'fn': fn
        }
        total_tp += tp; total_fp += fp; total_fn += fn
    metrics['_overall'] = {
        'precision': total_tp / (total_tp + total_fp) if (total_tp + total_fp) > 0 else 0.0,
        'recall': total_tp / (total_tp + total_fn) if (total_tp + total_fn) > 0 else 0.0,
        'tp': total_tp, 'fp': total_fp, 'fn': total_fn
    }

    return jsonify({'images': images_out, 'metrics': metrics})


@app.route('/api/eval/save_result', methods=['POST'])
def api_eval_save_result():
    payload = request.get_json(force=True) or {}
    project_id = payload.get('project_id', '')
    name = payload.get('name', '').strip()
    pred_dir = payload.get('pred_dir', '')
    conf_threshold = float(payload.get('conf_threshold', 0.25))
    iou_threshold = float(payload.get('iou_threshold', 0.5))
    metrics = payload.get('metrics', {})
    images = payload.get('images', [])

    if not project_id or not name:
        return jsonify({'error': 'project_id and name required'}), 400

    now = datetime.now().isoformat()
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute('''
            INSERT INTO eval_results (project_id, name, pred_dir, conf_threshold, iou_threshold,
                                      metrics, images, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (project_id, name, pred_dir, conf_threshold, iou_threshold,
              json.dumps(metrics, ensure_ascii=False),
              json.dumps(images, ensure_ascii=False), now))
        conn.commit()
    return jsonify({'success': True})


@app.route('/api/eval/list_results')
def api_eval_list_results():
    project_id = request.args.get('project_id', '')
    if not project_id:
        return jsonify([])
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            'SELECT id, name, pred_dir, conf_threshold, iou_threshold, created_at '
            'FROM eval_results WHERE project_id = ? ORDER BY created_at DESC',
            (project_id,)
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route('/api/eval/load_result')
def api_eval_load_result():
    result_id = request.args.get('id', '')
    if not result_id:
        return jsonify({'error': 'id required'}), 400
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT * FROM eval_results WHERE id = ?', (result_id,)).fetchone()
    if not row:
        return jsonify({'error': 'result not found'}), 404
    d = dict(row)
    d['metrics'] = json.loads(d['metrics'])
    d['images'] = json.loads(d['images'])
    return jsonify(d)


@app.route('/api/eval/delete_result/<int:result_id>', methods=['DELETE'])
def api_eval_delete_result(result_id):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute('DELETE FROM eval_results WHERE id = ?', (result_id,))
        conn.commit()
    return jsonify({'success': True})


if __name__ == '__main__':
    init_db()
    os.makedirs(config.IMAGES_DIR, exist_ok=True)
    os.makedirs(config.LABELS_DIR, exist_ok=True)
    app.run(host='0.0.0.0', port=5000, debug=False)
