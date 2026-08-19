import os
import re
import json
import time
import uuid
import threading
import sqlite3
from pathlib import Path
from datetime import datetime
import csv

from flask import Flask, request, jsonify, send_from_directory, render_template, abort, Response, stream_with_context
from werkzeug.utils import secure_filename
from PIL import Image, ImageFilter, ImageStat

import config

app = Flask(__name__, template_folder='templates', static_folder='static')

# Progress tracking for background tasks (e.g. version zip upload)
progress_store = {}  # task_id → {status, percent, message, done, error}
progress_lock = threading.Lock()


def _set_progress(task_id, status, percent, message, done=False, error=None):
    with progress_lock:
        progress_store[task_id] = {
            'status': status, 'percent': percent, 'message': message,
            'done': done, 'error': error
        }

VALID_NAME = re.compile(r'^[A-Za-z0-9_.-]+$')
DB_FILE = os.path.join(config.DB_DIR, 'annotations.db')


def _rel_path(abs_path):
    """Convert absolute path under BASE_DIR to relative path. Keep external paths as-is."""
    if abs_path and abs_path.startswith(config.BASE_DIR + os.sep):
        return os.path.relpath(abs_path, config.BASE_DIR)
    return abs_path


def _abs_path(path):
    """Resolve path: relative → joined with BASE_DIR; absolute → used as-is."""
    if path and not os.path.isabs(path):
        return os.path.join(config.BASE_DIR, path)
    return path or ''


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
                mode TEXT DEFAULT 'detection',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        ''')

        # Versions table (version-level isolation of images/labels)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS versions (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                name TEXT NOT NULL,
                images_dir TEXT NOT NULL,
                labels_dir TEXT NOT NULL,
                note TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id)
            )
        ''')

        # Images table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                version_id TEXT NOT NULL,
                name TEXT NOT NULL,
                filename TEXT NOT NULL,
                image_path TEXT NOT NULL,
                image_width INTEGER,
                image_height INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY (project_id) REFERENCES projects(id),
                UNIQUE(version_id, name)
            )
        ''')

        # Labels table (latest state)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS labels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id TEXT NOT NULL,
                version_id TEXT,
                image_id INTEGER NOT NULL,
                image_path TEXT,
                image_width INTEGER,
                image_height INTEGER,
                shapes TEXT NOT NULL,
                class_label TEXT,
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
                version_id TEXT,
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
                version_id TEXT,
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

        # Add version_id columns to existing tables (idempotent)
        for table in ('images', 'labels', 'label_backups', 'eval_results'):
            try:
                cursor.execute(f'ALTER TABLE {table} ADD COLUMN version_id TEXT')
            except sqlite3.OperationalError:
                pass  # Column already exists

        # Add classify columns to existing tables (idempotent)
        for table, col, definition in (
            ('projects', 'mode', "TEXT DEFAULT 'detection'"),
            ('labels', 'class_label', 'TEXT'),
        ):
            try:
                cursor.execute(f'ALTER TABLE {table} ADD COLUMN {col} {definition}')
            except sqlite3.OperationalError:
                pass  # Column already exists

        conn.commit()

    # Migrate existing data into a default version per project
    migrate_to_versions()


def migrate_to_versions():
    """One-time migration: create a default version for each existing project
    and backfill version_id on all images/labels/backups/eval_results."""
    with get_db() as conn:
        cursor = conn.cursor()
        # Skip if versions already populated
        cursor.execute('SELECT COUNT(*) FROM versions')
        if cursor.fetchone()[0] > 0:
            return
        # Skip if no projects yet
        cursor.execute('SELECT id, images_dir, labels_dir FROM projects')
        projects = cursor.fetchall()
        now = datetime.now().isoformat()
        for p in projects:
            pid = p['id']
            # Default version id = project id (simple, stable)
            cursor.execute('''
                INSERT OR IGNORE INTO versions (id, project_id, name, images_dir, labels_dir, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (pid, pid, '默认版本', p['images_dir'], p['labels_dir'], '迁移自原有数据', now))
            # Backfill version_id
            cursor.execute('UPDATE images SET version_id = ? WHERE project_id = ? AND version_id IS NULL', (pid, pid))
            cursor.execute('UPDATE labels SET version_id = ? WHERE project_id = ? AND version_id IS NULL', (pid, pid))
            cursor.execute('UPDATE label_backups SET version_id = ? WHERE project_id = ? AND version_id IS NULL', (pid, pid))
            cursor.execute('UPDATE eval_results SET version_id = ? WHERE project_id = ? AND version_id IS NULL', (pid, pid))
        conn.commit()
    print(f"[migrate] Created default versions for {len(projects)} projects")


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
            INSERT INTO projects (id, name, classes, images_dir, labels_dir, labels_format, mode, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            project['id'],
            project['name'],
            json.dumps(project['classes'], ensure_ascii=False),
            _rel_path(project['images_dir']),
            _rel_path(project['labels_dir']),
            project.get('labels_format', 'json'),
            project.get('mode', 'detection'),
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
            params.append(_rel_path(updates['images_dir']))
        if 'labels_dir' in updates:
            set_clauses.append('labels_dir = ?')
            params.append(_rel_path(updates['labels_dir']))
        if 'labels_format' in updates:
            set_clauses.append('labels_format = ?')
            params.append(updates['labels_format'])
        if 'mode' in updates:
            set_clauses.append('mode = ?')
            params.append(updates['mode'])

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
        cursor.execute('DELETE FROM versions WHERE project_id = ?', (project_id,))
        cursor.execute('DELETE FROM projects WHERE id = ?', (project_id,))
        conn.commit()


# ── Version DB helpers ─────────────────────────────────────
def db_list_versions(project_id):
    """List all versions of a project with image/label counts."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT v.*,
                   (SELECT COUNT(*) FROM images i WHERE i.version_id = v.id) AS image_count,
                   (SELECT COUNT(*) FROM labels l WHERE l.version_id = v.id) AS label_count
            FROM versions v
            WHERE v.project_id = ?
            ORDER BY v.created_at ASC
        ''', (project_id,))
        return [dict(r) for r in cursor.fetchall()]


def db_get_version(version_id):
    """Get single version by id, joined with project classes."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT v.*, p.classes AS project_classes, p.name AS project_name, p.labels_format
            FROM versions v
            JOIN projects p ON v.project_id = p.id
            WHERE v.id = ?
        ''', (version_id,))
        row = cursor.fetchone()
        if not row:
            return None
        v = dict(row)
        v['classes'] = json.loads(v['project_classes'])
        return v


def db_insert_version(project_id, name, images_dir, labels_dir, note=''):
    """Insert a new version."""
    vid = str(uuid.uuid4())[:8]
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO versions (id, project_id, name, images_dir, labels_dir, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (vid, project_id, name, _rel_path(images_dir), _rel_path(labels_dir), note, datetime.now().isoformat()))
        conn.commit()
    return db_get_version(vid)


def db_update_version(version_id, updates):
    """Update version name/note."""
    with get_db() as conn:
        cursor = conn.cursor()
        if 'name' in updates:
            cursor.execute('UPDATE versions SET name = ? WHERE id = ?', (updates['name'], version_id))
        if 'note' in updates:
            cursor.execute('UPDATE versions SET note = ? WHERE id = ?', (updates['note'], version_id))
        conn.commit()
    return db_get_version(version_id)


def db_delete_version(version_id):
    """Delete version and its images/labels/backups."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT id FROM images WHERE version_id = ?', (version_id,))
        image_ids = [r[0] for r in cursor.fetchall()]
        if image_ids:
            placeholders = ','.join('?' * len(image_ids))
            cursor.execute(f'DELETE FROM label_backups WHERE image_id IN ({placeholders})', image_ids)
            cursor.execute(f'DELETE FROM labels WHERE image_id IN ({placeholders})', image_ids)
        cursor.execute('DELETE FROM images WHERE version_id = ?', (version_id,))
        cursor.execute('DELETE FROM eval_results WHERE version_id = ?', (version_id,))
        cursor.execute('DELETE FROM versions WHERE id = ?', (version_id,))
        conn.commit()


# ── Image DB helpers ───────────────────────────────────────
def db_list_images(version_id):
    """Get all images for a version, with label status and classes."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT i.*,
                   CASE WHEN l.id IS NOT NULL THEN 1 ELSE 0 END AS has_label,
                   l.shapes,
                   l.class_label
            FROM images i
            LEFT JOIN labels l ON i.id = l.image_id AND i.version_id = l.version_id
            WHERE i.version_id = ?
            ORDER BY i.name
        ''', (version_id,))
        rows = cursor.fetchall()
        result = []
        for row in rows:
            r = dict(row)
            # Extract unique class indices from shapes
            classes = []
            if r.get('shapes'):
                try:
                    shapes = json.loads(r['shapes'])
                    classes = list(set(s.get('class_idx', 0) for s in shapes if isinstance(s, dict)))
                except:
                    pass
            result.append({
                'name': r['name'],
                'filename': r['filename'],
                'has_label': bool(r['has_label']),
                'classes': classes,
                'class_label': r.get('class_label')
            })
        return result


def db_get_image(version_id, name):
    """Get single image by version_id and base name."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM images WHERE version_id = ? AND name = ?', (version_id, name))
        row = cursor.fetchone()
        return dict(row) if row else None


def db_get_image_index(version_id):
    """Get {name: {id, project_id, image_width, image_height}} for all images in a version.
    Lightweight lookup for batch operations."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT id, name, project_id, image_width, image_height FROM images WHERE version_id = ?',
                       (version_id,))
        return {row['name']: dict(row) for row in cursor.fetchall()}


def db_get_image_by_id(image_id):
    """Get single image by id."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM images WHERE id = ?', (image_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def db_insert_image(version_id, name, filename, image_path, image_width=None, image_height=None, project_id=None):
    """Insert or update image in database. project_id auto-derived from version if not given."""
    if project_id is None:
        v = db_get_version(version_id)
        project_id = v['project_id'] if v else None
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        try:
            cursor.execute('''
                INSERT INTO images (project_id, version_id, name, filename, image_path, image_width, image_height, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (project_id, version_id, name, filename, _rel_path(image_path), image_width, image_height, now))
        except sqlite3.IntegrityError:
            cursor.execute('''
                UPDATE images
                SET filename = ?, image_path = ?, image_width = ?, image_height = ?
                WHERE version_id = ? AND name = ?
            ''', (filename, _rel_path(image_path), image_width, image_height, version_id, name))
        conn.commit()
        return db_get_image(version_id, name)


def db_insert_images_batch(version_id, entries, project_id=None):
    """Batch insert/update images. entries = [(name, filename, image_path, width, height), ...].
    Uses a single transaction — MUCH faster for bulk imports."""
    if not entries:
        return
    if project_id is None:
        v = db_get_version(version_id)
        project_id = v['project_id'] if v else None
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        cursor.execute('BEGIN')
        for name, filename, image_path, width, height in entries:
            try:
                cursor.execute('''
                    INSERT INTO images (project_id, version_id, name, filename, image_path, image_width, image_height, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (project_id, version_id, name, filename, _rel_path(image_path), width, height, now))
            except sqlite3.IntegrityError:
                cursor.execute('''
                    UPDATE images
                    SET filename = ?, image_path = ?, image_width = ?, image_height = ?
                    WHERE version_id = ? AND name = ?
                ''', (filename, _rel_path(image_path), width, height, version_id, name))
        conn.commit()


def db_delete_image(version_id, name):
    """Delete image and associated labels/backups."""
    img = db_get_image(version_id, name)
    if not img:
        return
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM label_backups WHERE image_id = ?', (img['id'],))
        cursor.execute('DELETE FROM labels WHERE image_id = ?', (img['id'],))
        cursor.execute('DELETE FROM images WHERE id = ?', (img['id'],))
        conn.commit()


# ── Label DB helpers ───────────────────────────────────────
def db_get_label(version_id, image_name):
    """Get label for an image by version and image name."""
    img = db_get_image(version_id, image_name)
    if not img:
        return None
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM labels WHERE version_id = ? AND image_id = ?', (version_id, img['id']))
        row = cursor.fetchone()
        if not row:
            return None
        lbl = dict(row)
        lbl['shapes'] = json.loads(lbl['shapes'])
        return lbl


def db_save_label(version_id, image_name, shapes, image_path=None, image_width=None, image_height=None):
    """Save label (create or update), creating backup first if exists."""
    img = db_get_image(version_id, image_name)
    if not img:
        return None

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM labels WHERE version_id = ? AND image_id = ?', (version_id, img['id']))
        existing = cursor.fetchone()
        if existing:
            cursor.execute('''
                INSERT INTO label_backups (project_id, version_id, image_id, shapes, created_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (img['project_id'], version_id, img['id'], existing['shapes'], datetime.now().isoformat()))

        now = datetime.now().isoformat()
        shapes_json = json.dumps(shapes, ensure_ascii=False)

        try:
            cursor.execute('''
                INSERT INTO labels (project_id, version_id, image_id, image_path, image_width, image_height, shapes, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (img['project_id'], version_id, img['id'], _rel_path(image_path), image_width, image_height, shapes_json, now))
        except sqlite3.IntegrityError:
            cursor.execute('''
                UPDATE labels
                SET image_path = ?, image_width = ?, image_height = ?, shapes = ?, updated_at = ?
                WHERE version_id = ? AND image_id = ?
            ''', (_rel_path(image_path), image_width, image_height, shapes_json, now, version_id, img['id']))

        conn.commit()

    return db_get_label(version_id, image_name)


def db_save_class_label(version_id, image_name, class_label, image_path=None, image_width=None, image_height=None):
    """Save whole-image classification label (class_label), reusing the labels table
    with shapes='[]'. A detection project leaves class_label NULL; here we set it."""
    img = db_get_image(version_id, image_name)
    if not img:
        return None

    image_path = image_path or img['filename']
    image_width = image_width or img['image_width']
    image_height = image_height or img['image_height']

    with get_db() as conn:
        cursor = conn.cursor()
        # Create backup of existing label
        cursor.execute('SELECT * FROM labels WHERE version_id = ? AND image_id = ?', (version_id, img['id']))
        existing = cursor.fetchone()
        if existing:
            cursor.execute('''
                INSERT INTO label_backups (project_id, version_id, image_id, shapes, created_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (img['project_id'], version_id, img['id'], existing['shapes'], datetime.now().isoformat()))

        now = datetime.now().isoformat()
        shapes_json = json.dumps([], ensure_ascii=False)
        # Empty label -> store NULL so frontend treats as unlabeled
        cls_val = class_label.strip() if class_label else None

        try:
            cursor.execute('''
                INSERT INTO labels (project_id, version_id, image_id, image_path, image_width, image_height,
                                    shapes, class_label, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (img['project_id'], version_id, img['id'], _rel_path(image_path), image_width, image_height, shapes_json, cls_val, now))
        except sqlite3.IntegrityError:
            cursor.execute('''
                UPDATE labels
                SET image_path = ?, image_width = ?, image_height = ?, shapes = ?, class_label = ?, updated_at = ?
                WHERE version_id = ? AND image_id = ?
            ''', (_rel_path(image_path), image_width, image_height, shapes_json, cls_val, now, version_id, img['id']))

        conn.commit()

    return db_get_label(version_id, image_name)


def db_save_labels_batch(version_id, labels, images_index=None):
    """Batch save labels. labels = [(image_name, shapes, image_path, width, height), ...].
    images_index = {image_name: img_row_dict} to avoid per-label image lookups.
    Skips backup creation (bulk import only)."""
    if not labels:
        return 0
    if images_index is None:
        images_index = {}
    saved = 0
    with get_db() as conn:
        cursor = conn.cursor()
        now = datetime.now().isoformat()
        cursor.execute('BEGIN')
        for image_name, shapes, image_path, width, height in labels:
            img = images_index.get(image_name)
            if not img:
                img = db_get_image(version_id, image_name)
                if not img:
                    continue
            shapes_json = json.dumps(shapes, ensure_ascii=False)
            try:
                cursor.execute('''
                    INSERT INTO labels (project_id, version_id, image_id, image_path, image_width, image_height, shapes, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (img['project_id'], version_id, img['id'], _rel_path(image_path),
                      width, height, shapes_json, now))
            except sqlite3.IntegrityError:
                cursor.execute('''
                    UPDATE labels
                    SET image_path = ?, image_width = ?, image_height = ?, shapes = ?, updated_at = ?
                    WHERE version_id = ? AND image_id = ?
                ''', (_rel_path(image_path), width, height, shapes_json, now, version_id, img['id']))
            saved += 1
        conn.commit()
    return saved


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


def sync_images_from_filesystem(version_id, images_dir, project_id=None):
    """Sync image records from filesystem to database for a version."""
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
        db_insert_image(version_id, base, filename, str(img_path), width, height, project_id=project_id)


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
    """Return (images_dir, labels_dir, classes, project, version_id, version).

    Version-aware: prefer version_id (resolved from query/json/form). Falls back
    to project_id (legacy) using its default version.
    """
    version_id = request.args.get('version_id', '')
    if not version_id:
        try:
            version_id = (request.get_json(force=True, silent=True) or {}).get('version_id', '')
        except Exception:
            pass
    if not version_id:
        version_id = request.form.get('version_id', '')

    project_id = request.args.get('project_id', '')
    if not project_id:
        try:
            project_id = (request.get_json(force=True, silent=True) or {}).get('project_id', '')
        except Exception:
            pass
    if not project_id:
        project_id = request.form.get('project_id', '')

    # Version takes precedence
    if version_id:
        v = db_get_version(version_id)
        if v:
            p = db_get_project(v['project_id'])
            if p:
                return _abs_path(v['images_dir']), _abs_path(v['labels_dir']), p['classes'], p, version_id, v

    # Legacy: resolve via project's default version
    if project_id:
        p = db_get_project(project_id)
        if p:
            # Try project's first version (default version)
            versions = db_list_versions(project_id)
            if versions:
                v = versions[0]
                return _abs_path(v['images_dir']), _abs_path(v['labels_dir']), p['classes'], p, v['id'], v
            return _abs_path(p['images_dir']), _abs_path(p['labels_dir']), p['classes'], p, None, None
    return config.IMAGES_DIR, config.LABELS_DIR, config.CLASSES, None, None, None


# ── Zip upload & import helpers ─────────────────────────────
def _detect_label_format(labels_dir):
    """Detect label format from files in labels directory. Returns 'json', 'yolo', or 'coco'."""
    if not os.path.isdir(labels_dir):
        return 'json'
    files = os.listdir(labels_dir)
    for f in files:
        lower = f.lower()
        if lower.endswith('.json'):
            try:
                with open(os.path.join(labels_dir, f), 'r', encoding='utf-8') as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    if 'annotations' in data and 'images' in data and 'categories' in data:
                        return 'coco'
                    if 'shapes' in data or 'imagePath' in data:
                        return 'json'
            except Exception:
                pass
        if lower.endswith('.txt') and lower != 'classes.txt':
            return 'yolo'
    return 'json'


def _extract_zip_to_temp(zip_file_storage):
    """Extract uploaded zip file to a temp directory. Returns the temp dir path."""
    import tempfile
    import zipfile
    tmp = tempfile.mkdtemp(prefix='version_upload_')
    with zipfile.ZipFile(zip_file_storage, 'r') as zf:
        zf.extractall(tmp)
    return tmp


def _extract_zip_to_temp_path(zip_path):
    """Extract zip from a filesystem path to a temp directory. Returns the temp dir path."""
    import tempfile
    import zipfile
    tmp = tempfile.mkdtemp(prefix='version_upload_')
    with zipfile.ZipFile(zip_path, 'r') as zf:
        zf.extractall(tmp)
    return tmp


def _categorize_files(temp_dir):
    """Scan temp_dir and return (images_src, labels_src, label_fmt).

    If 'images/' and 'labels/' subdirs exist, use them directly.
    Otherwise, categorize files by extension into images/ and labels/.
    """
    contents = os.listdir(temp_dir)
    has_images_dir = 'images' in contents and os.path.isdir(os.path.join(temp_dir, 'images'))
    has_labels_dir = 'labels' in contents and os.path.isdir(os.path.join(temp_dir, 'labels'))

    if has_images_dir and has_labels_dir:
        images_src = os.path.join(temp_dir, 'images')
        labels_src = os.path.join(temp_dir, 'labels')
    else:
        os.makedirs(os.path.join(temp_dir, '_images'), exist_ok=True)
        os.makedirs(os.path.join(temp_dir, '_labels'), exist_ok=True)
        images_src = os.path.join(temp_dir, '_images')
        labels_src = os.path.join(temp_dir, '_labels')

        for root, dirs, files in os.walk(temp_dir):
            # Skip our own image/label dirs
            if root.startswith(images_src) or root.startswith(labels_src):
                continue
            for f in files:
                src = os.path.join(root, f)
                lower = f.lower()
                if lower.endswith(('.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp')):
                    import shutil
                    # Avoid overwrite by prefixing with relative path
                    rel = os.path.relpath(src, temp_dir)
                    dst_name = f if f not in os.listdir(images_src) else rel.replace(os.sep, '_')
                    shutil.copy2(src, os.path.join(images_src, dst_name))
                elif lower.endswith(('.txt', '.json')):
                    import shutil
                    shutil.copy2(src, os.path.join(labels_src, f))

    fmt = _detect_label_format(labels_src)
    return images_src, labels_src, fmt


def _import_coco_from_dir(labels_src, images_dir, version_id, project_id, classes):
    """Import COCO format annotations from a directory containing COCO JSON files."""
    import shutil
    imported = 0
    for f in sorted(os.listdir(labels_src)):
        if not f.lower().endswith('.json'):
            continue
        try:
            with open(os.path.join(labels_src, f), 'r', encoding='utf-8') as fh:
                coco = json.load(fh)
            if not isinstance(coco, dict) or 'annotations' not in coco:
                continue

            # Build lookup: image_id → {file_name, width, height}
            img_map = {}
            for img_info in coco.get('images', []):
                img_map[img_info['id']] = img_info

            # Build lookup: image_id → [annotations]
            ann_map = {}
            for ann in coco.get('annotations', []):
                ann_map.setdefault(ann['image_id'], []).append(ann)

            # Category lookup
            cat_map = {}
            for cat in coco.get('categories', []):
                cat_map[cat['id']] = cat['name']

            for img_id, img_info in img_map.items():
                annotations = ann_map.get(img_id, [])
                if not annotations:
                    continue

                file_name = img_info.get('file_name', '')
                base, ext = os.path.splitext(file_name)
                if not base:
                    continue

                W = img_info.get('width', 0)
                H = img_info.get('height', 0)

                shapes = []
                for ann in annotations:
                    bbox = ann.get('bbox', [])
                    if len(bbox) != 4:
                        continue
                    x, y, w, h = bbox
                    label = cat_map.get(ann.get('category_id', 0), 'unknown')
                    shapes.append({
                        "label": label,
                        "points": [[x, y], [x + w, y + h]],
                        "group_id": None, "shape_type": "rectangle", "flags": {}
                    })

                if shapes:
                    db_save_label(version_id, base, shapes, file_name, W, H)
                    imported += 1

        except Exception:
            continue

    return imported


def _import_and_copy_files(version_id, images_src, labels_src, target_images_dir, target_labels_dir,
                           project_id, classes, label_fmt):
    """Copy files from temp dirs to version's target dirs and import into DB."""
    import shutil
    # Copy image files
    os.makedirs(target_images_dir, exist_ok=True)
    image_count = 0
    for f in sorted(os.listdir(images_src)):
        lower = f.lower()
        if lower.endswith(('.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp')):
            shutil.copy2(os.path.join(images_src, f), os.path.join(target_images_dir, f))
            image_count += 1

    # Copy label files
    if os.path.isdir(labels_src):
        os.makedirs(target_labels_dir, exist_ok=True)
        for f in sorted(os.listdir(labels_src)):
            shutil.copy2(os.path.join(labels_src, f), os.path.join(target_labels_dir, f))

    # Sync images from filesystem into DB
    sync_images_from_filesystem(version_id, target_images_dir, project_id)

    # Import labels
    imported = 0
    if label_fmt == 'json':
        imported = _import_json_labels(version_id, target_images_dir, target_labels_dir, project_id, classes)
    elif label_fmt == 'yolo':
        imported = _import_labels(target_images_dir, target_labels_dir, 'yolo',
                                  version_id=version_id, project_classes=classes, project_id=project_id)
    elif label_fmt == 'coco':
        imported = _import_coco_from_dir(labels_src, target_images_dir, version_id, project_id, classes)

    return image_count, imported


def _import_and_copy_files_with_progress(version_id, images_src, labels_src, target_images_dir, target_labels_dir,
                                         project_id, classes, label_fmt, task_id):
    """Same as _import_and_copy_files but with progress updates via SSE."""
    import shutil

    # Phase 1: Copy image files (40% - 60%)
    os.makedirs(target_images_dir, exist_ok=True)
    image_files = sorted([f for f in os.listdir(images_src)
                          if f.lower().endswith(('.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp'))])
    total_images = len(image_files)
    for i, f in enumerate(image_files):
        shutil.copy2(os.path.join(images_src, f), os.path.join(target_images_dir, f))
        if total_images > 10 and i % max(1, total_images // 10) == 0:
            pct = 40 + int(20 * i / total_images)
            _set_progress(task_id, 'copying', pct, f'正在复制图片 ({i+1}/{total_images})…')

    # Phase 2: Copy label files（无标注时 labels_src=None，跳过）
    if labels_src and os.path.isdir(labels_src):
        os.makedirs(target_labels_dir, exist_ok=True)
        label_files = [f for f in sorted(os.listdir(labels_src))
                       if f.lower().endswith(('.txt', '.json'))]
        for f in label_files:
            shutil.copy2(os.path.join(labels_src, f), os.path.join(target_labels_dir, f))

    # Phase 3: Sync images into DB (60% - 75%) — returns dims dict for reuse
    _set_progress(task_id, 'syncing', 60, f'正在同步 {total_images} 张图片信息到数据库…')
    dims = _sync_images_with_progress(version_id, target_images_dir, project_id, task_id, 60, 75, total_images)

    # Phase 4: Import labels (75% - 100%) — 无标注时跳过
    imported = 0
    if label_fmt and labels_src:
        if label_fmt == 'json':
            imported = _import_json_labels_with_progress(version_id, target_images_dir, target_labels_dir,
                                                         project_id, classes, task_id, 75, 100, dims=dims)
        elif label_fmt == 'yolo':
            imported = _import_labels_with_progress(target_images_dir, target_labels_dir, 'yolo',
                                                   version_id=version_id, project_classes=classes,
                                                   project_id=project_id, task_id=task_id, pct_start=75, pct_end=100,
                                                   dims=dims)
        elif label_fmt == 'coco':
            _set_progress(task_id, 'importing', 75, '正在导入 COCO 标注…')
            imported = _import_coco_from_dir(labels_src, target_images_dir, version_id, project_id, classes)

    return total_images, imported


def _sync_images_with_progress(version_id, images_dir, project_id, task_id, pct_start, pct_end, total_files):
    """Sync images into DB with progress. Returns {base_name: (width, height)} dict for reuse."""
    dims = {}
    if not os.path.exists(images_dir):
        return dims

    image_files = sorted([f for f in os.listdir(images_dir)
                           if f.lower().endswith(tuple(config.IMAGE_EXTS))])
    n = len(image_files)
    if n == 0:
        return dims

    # Collect all entries and dimensions in one pass
    entries = []
    for i, filename in enumerate(image_files):
        base, _ = os.path.splitext(filename)
        img_path = Path(images_dir) / filename
        w, h = None, None
        try:
            with Image.open(img_path) as img:
                w, h = img.size
        except Exception:
            pass
        dims[base] = (w, h)
        entries.append((base, filename, str(img_path), w, h))
        if n > 50 and i % max(1, n // 20) == 0:
            pct = pct_start + int((pct_end - pct_start) * i / n)
            _set_progress(task_id, 'reading', pct, f'正在读取图片信息 ({i+1}/{n})…')

    # Batch insert all at once
    _set_progress(task_id, 'syncing', pct_end - 3, f'正在写入数据库…')
    db_insert_images_batch(version_id, entries, project_id)

    return dims


def _import_json_labels_with_progress(version_id, images_dir, labels_dir, project_id, classes,
                                      task_id, pct_start, pct_end, dims=None):
    """Import JSON labels with progress. dims={base:(w,h)} avoids re-opening images.
    Batched DB writes for performance."""
    imported = 0
    if not os.path.isdir(labels_dir):
        return 0
    if dims is None:
        dims = {}

    json_files = sorted([f for f in os.listdir(labels_dir) if f.lower().endswith('.json')])
    n = len(json_files)
    if n == 0:
        return 0

    # Build image index once: {name: img_row}
    images_index = db_get_image_index(version_id)

    # Collect all labels to batch-save
    batch = []
    for i, f in enumerate(json_files):
        try:
            with open(os.path.join(labels_dir, f), 'r', encoding='utf-8') as fh:
                data = json.load(fh)
        except Exception:
            continue

        if not isinstance(data, dict):
            continue

        shapes = data.get('shapes', [])
        image_path = data.get('imagePath', '')
        base, _ = os.path.splitext(image_path or f)

        if not shapes and not image_path:
            continue

        W, H = dims.get(base, (None, None))
        if not W:
            W = data.get('imageWidth', 0)
            H = data.get('imageHeight', 0)
        if (not W or not H) and image_path:
            img_file = os.path.join(images_dir, image_path)
            if not os.path.exists(img_file):
                for ext in config.IMAGE_EXTS:
                    candidate = os.path.join(images_dir, base + ext)
                    if os.path.exists(candidate):
                        img_file = candidate
                        break
            try:
                with Image.open(img_file) as im:
                    W, H = im.size
            except Exception:
                pass

        normalized = []
        for s in shapes:
            label = s.get('label', s.get('class_name', 'unknown'))
            pts = s.get('points', [])
            if len(pts) >= 2:
                normalized.append({
                    "label": label,
                    "points": pts[:2],
                    "group_id": s.get('group_id'),
                    "shape_type": s.get('shape_type', 'rectangle'),
                    "flags": s.get('flags', {})
                })

        if normalized or shapes:
            batch.append((base, normalized or shapes, image_path or f, W, H))

        if n > 50 and i % max(1, n // 20) == 0:
            pct = pct_start + int((pct_end - pct_start) * i / n)
            _set_progress(task_id, 'importing', pct, f'正在解析标注 ({i+1}/{n})…')

    # Batch save all at once
    if batch:
        _set_progress(task_id, 'saving', pct_end - 2, f'正在写入 {len(batch)} 条标注到数据库…')
        imported = db_save_labels_batch(version_id, batch, images_index=images_index)

    return imported


def _import_labels_with_progress(images_dir, labels_dir, fmt, version_id=None, project_classes=None,
                                 project_id=None, task_id=None, pct_start=0, pct_end=100, dims=None):
    """Import YOLO labels with progress updates. dims={base:(w,h)} avoids re-opening images."""
    if fmt != 'yolo':
        return 0
    if not os.path.exists(labels_dir):
        return 0
    if project_classes is None:
        project_classes = config.CLASSES
    if dims is None:
        dims = {}

    txt_files = sorted([f for f in os.listdir(labels_dir)
                        if f.endswith('.txt') and f != 'classes.txt'])
    n = len(txt_files)
    imported = 0

    for i, txt_file in enumerate(txt_files):
        base = os.path.splitext(txt_file)[0]
        try:
            # Use pre-read dims; fall back to opening image once
            W, H = dims.get(base, (None, None))
            if not W or not H:
                img_path = get_image_path_from_filesystem(base, images_dir)
                if not img_path:
                    continue
                with Image.open(img_path) as im:
                    W, H = im.size
            else:
                img_path = None  # only need name for label
            # Find image filename for the label record
            img_filename = f"{base}.jpg"
            if not os.path.exists(os.path.join(images_dir, img_filename)):
                for ext in config.IMAGE_EXTS:
                    if os.path.exists(os.path.join(images_dir, base + ext)):
                        img_filename = base + ext
                        break

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
                    "points": [[(cx-bw/2)*W, (cy-bh/2)*H], [(cx+bw/2)*W, (cy+bh/2)*H]],
                    "group_id": None, "shape_type": "rectangle", "flags": {}
                })
            if version_id:
                db_save_label(version_id, base, shapes, img_filename, W, H)
            imported += 1
        except Exception:
            continue

        if n > 50 and i % max(1, n // 20) == 0:
            pct = pct_start + int((pct_end - pct_start) * i / n)
            _set_progress(task_id, 'importing', pct, f'正在导入 YOLO 标注 ({i+1}/{n})…')

    return imported


def _import_json_labels(version_id, images_dir, labels_dir, project_id, classes):
    """Import internal JSON format labels from filesystem into DB."""
    imported = 0
    if not os.path.isdir(labels_dir):
        return 0

    for f in sorted(os.listdir(labels_dir)):
        if not f.lower().endswith('.json'):
            continue
        try:
            with open(os.path.join(labels_dir, f), 'r', encoding='utf-8') as fh:
                data = json.load(fh)
        except Exception:
            continue

        if not isinstance(data, dict):
            continue

        shapes = data.get('shapes', [])
        image_path = data.get('imagePath', '')
        base, _ = os.path.splitext(image_path or f)

        if not shapes and not image_path:
            continue

        # Try to find the image to get dimensions
        W = data.get('imageWidth', 0)
        H = data.get('imageHeight', 0)
        if (not W or not H) and image_path:
            img_file = os.path.join(images_dir, image_path)
            if not os.path.exists(img_file):
                # Try matching by base name
                for ext in config.IMAGE_EXTS:
                    candidate = os.path.join(images_dir, base + ext)
                    if os.path.exists(candidate):
                        img_file = candidate
                        break
            try:
                with Image.open(img_file) as im:
                    W, H = im.size
            except Exception:
                pass

        # Normalize shapes to internal format
        normalized = []
        for s in shapes:
            label = s.get('label', s.get('class_name', 'unknown'))
            pts = s.get('points', [])
            if len(pts) >= 2:
                normalized.append({
                    "label": label,
                    "points": pts[:2],
                    "group_id": s.get('group_id'),
                    "shape_type": s.get('shape_type', 'rectangle'),
                    "flags": s.get('flags', {})
                })

        if normalized or shapes:
            db_save_label(version_id, base, normalized or shapes,
                          image_path or f, W, H)
            imported += 1

    return imported


# ── Format import helper (YOLO only) ───────────────────────
def _import_labels(images_dir, labels_dir, fmt, version_id=None, project_classes=None, project_id=None):
    """Import YOLO labels from filesystem into DB (if version_id provided)."""
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
            if version_id:
                sync_images_from_filesystem(version_id, images_dir, project_id)
                db_save_label(version_id, base, shapes, img_path.name, W, H)
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
    """Version list page for a project."""
    p = db_get_project(project_id)
    if not p:
        abort(404)
    versions = db_list_versions(project_id)
    return render_template('versions.html',
                           project=p,
                           versions=versions,
                           base_dir=config.BASE_DIR,
                           project_id=project_id,
                           project_name=p.get('name', ''))


@app.route('/version/<version_id>')
def version_page(version_id):
    """Multi-image annotation page for a specific version.
    Routes by project mode: classification -> classify page, detection -> index page."""
    version = db_get_version(version_id)
    if not version:
        abort(404)
    # Sync images from filesystem when entering version
    sync_images_from_filesystem(version_id, _abs_path(version['images_dir']), version['project_id'])
    ctx = dict(
        classes=version['classes'],
        base_dir=config.BASE_DIR,
        version_id=version_id,
        project_id=version['project_id'],
        project_name=version.get('project_name', '')
    )
    project = db_get_project(version['project_id'])
    if project and project.get('mode') == 'classification':
        return render_template('classify.html', **ctx)
    return render_template('index.html', **ctx)


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

    classes = classes_raw if isinstance(classes_raw, list) and classes_raw else config.CLASSES
    mode = payload.get('mode', 'detection')
    if mode not in ('detection', 'classification'):
        mode = 'detection'

    # New-style project: no data paths, just name + classes
    if not images_dir:
        project = {
            "id": str(uuid.uuid4())[:8],
            "name": name,
            "images_dir": '',
            "labels_dir": '',
            "classes": classes,
            "labels_format": labels_format,
            "mode": mode
        }
        db_insert_project(project)
        result = dict(project)
        result['imported_count'] = 0
        return jsonify(result)

    # Legacy: project with existing data paths
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

    os.makedirs(images_dir, exist_ok=True)
    os.makedirs(labels_dir, exist_ok=True)

    project = {
        "id": str(uuid.uuid4())[:8],
        "name": name,
        "images_dir": images_dir,
        "labels_dir": labels_dir,
        "classes": classes,
        "labels_format": labels_format,
        "mode": mode
    }

    db_insert_project(project)
    # Create default version for new project
    version = db_insert_version(project['id'], '默认版本', images_dir, labels_dir, '项目初始版本')
    if version:
        sync_images_from_filesystem(version['id'], images_dir, project['id'])

    imported_count = None
    if labels_format != 'json':
        imported_count = _import_labels(images_dir, labels_dir, labels_format, project['id'], classes, version_id=version['id'] if version else None)

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


# ── SSE progress endpoint ──────────────────────────────────
@app.route('/api/task_progress/<task_id>')
def api_task_progress(task_id):
    """SSE endpoint: stream progress updates for a background task."""
    def generate():
        last_data = None
        while True:
            with progress_lock:
                data = dict(progress_store.get(task_id, {
                    'status': 'unknown', 'percent': 0, 'message': '', 'done': False, 'error': None
                }))
            # Skip duplicate events to reduce traffic
            if data != last_data:
                yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                last_data = data
            if data['done']:
                break
            time.sleep(0.3)
    return Response(stream_with_context(generate()), mimetype='text/event-stream')


# ── Version API ────────────────────────────────────────────
@app.route('/api/versions/<project_id>', methods=['GET'])
def api_get_versions(project_id):
    """List all versions for a project."""
    versions = db_list_versions(project_id)
    return jsonify(versions)


@app.route('/api/versions/<project_id>', methods=['POST'])
def api_create_version(project_id):
    """Create a new version for a project. Accepts JSON or multipart form with optional zip_file."""
    p = db_get_project(project_id)
    if not p:
        return jsonify({"error": "project not found"}), 404

    # Try JSON first, then form data
    payload = request.get_json(force=True, silent=True) or {}
    is_multipart = not bool(payload)

    if is_multipart:
        name = request.form.get('name', '').strip()
        note = request.form.get('note', '').strip()
        source_version_id = request.form.get('source_version_id', '')
        has_labels = request.form.get('has_labels', '1') == '1'
    else:
        name = payload.get('name', '').strip()
        note = payload.get('note', '').strip()
        source_version_id = payload.get('source_version_id', '')
        has_labels = payload.get('has_labels', True)

    zip_file = request.files.get('zip_file') if is_multipart else None

    if not name:
        return jsonify({"error": "name required"}), 400

    # Determine version directory
    versions = db_list_versions(project_id)
    version_num = len(versions) + 1
    if p.get('images_dir'):
        base_dir = os.path.join(_abs_path(p['images_dir']), f'_versions', f'v{version_num}')
    else:
        base_dir = os.path.join(config.BASE_DIR, 'projects', project_id, f'v{version_num}')
    images_dir = os.path.join(base_dir, 'images')
    labels_dir = os.path.join(base_dir, 'labels')

    # Create directories
    os.makedirs(images_dir, exist_ok=True)
    os.makedirs(labels_dir, exist_ok=True)

    # Create version record first
    version = db_insert_version(project_id, name, images_dir, labels_dir, note)
    if not version:
        return jsonify({"error": "failed to create version"}), 500

    # ── Zip upload: run in background with progress tracking ──
    if zip_file:
        # Save uploaded zip to a temp file (needed for background thread)
        import tempfile
        tmp_fd, tmp_path = tempfile.mkstemp(suffix='.zip', prefix='version_upload_')
        try:
            zip_file.save(tmp_path)
            os.close(tmp_fd)
        except Exception:
            os.close(tmp_fd)
            db_delete_version(version['id'])
            return jsonify({"error": "failed to save uploaded zip"}), 500

        task_id = str(uuid.uuid4())[:8]
        _set_progress(task_id, 'starting', 0, '正在准备处理…')

        def _run_zip_import():
            import shutil
            tmp = None
            try:
                _set_progress(task_id, 'extracting', 10, '正在解压文件…')
                tmp = _extract_zip_to_temp_path(tmp_path)

                _set_progress(task_id, 'analyzing', 25, '正在分析文件结构…')
                images_src, labels_src, label_fmt = _categorize_files(tmp)

                # 标注开关校验：勾选"有标注"但找不到标注文件 → 失败回滚
                if has_labels:
                    label_files = []
                    if os.path.isdir(labels_src):
                        label_files = [f for f in os.listdir(labels_src)
                                       if f.lower().endswith(('.txt', '.json'))]
                    if not label_files:
                        raise ValueError('未查找到标注文件：压缩包内无 labels 目录或 .txt/.json 标注文件')
                else:
                    # 不处理标注：清空 labels_src/label_fmt，后续导入阶段跳过
                    labels_src = None
                    label_fmt = None

                _set_progress(task_id, 'copying', 40, '正在复制文件…')
                image_count, imported_count = _import_and_copy_files_with_progress(
                    version['id'], images_src, labels_src,
                    images_dir, labels_dir, project_id, p['classes'], label_fmt,
                    task_id
                )

                _set_progress(task_id, 'done', 100, f'完成！共导入 {image_count} 张图片，{imported_count} 张标注',
                              done=True)
            except Exception as e:
                _set_progress(task_id, 'error', 0, str(e), done=True, error=str(e))
                db_delete_version(version['id'])
                # db_delete_version 只删 DB 行，补磁盘清理避免孤儿目录
                import shutil as _sh
                _sh.rmtree(base_dir, ignore_errors=True)
            finally:
                if tmp:
                    shutil.rmtree(tmp, ignore_errors=True)
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

        thread = threading.Thread(target=_run_zip_import, daemon=True)
        thread.start()

        result = dict(version)
        result['task_id'] = task_id
        return jsonify(result)

    elif source_version_id:
        # Copy from another version
        source_version = db_get_version(source_version_id)
        if not source_version:
            db_delete_version(version['id'])
            return jsonify({"error": "source version not found"}), 404

        import shutil
        source_images_dir = _abs_path(source_version['images_dir'])
        if os.path.exists(source_images_dir):
            shutil.copytree(source_images_dir, images_dir, dirs_exist_ok=True)

        source_labels_dir = _abs_path(source_version['labels_dir'])
        if os.path.exists(source_labels_dir):
            shutil.copytree(source_labels_dir, labels_dir, dirs_exist_ok=True)

        sync_images_from_filesystem(version['id'], images_dir, project_id)

        source_images = db_list_images(source_version_id)
        for img in source_images:
            old_label = db_get_label(source_version_id, img['name'])
            if old_label:
                new_img = db_get_image(version['id'], img['name'])
                if new_img:
                    db_save_label(
                        version_id=version['id'],
                        image_name=img['name'],
                        shapes=old_label['shapes'],
                        image_path=os.path.join(labels_dir, f'{img["name"]}.json'),
                        image_width=new_img.get('image_width'),
                        image_height=new_img.get('image_height')
                    )
    else:
        sync_images_from_filesystem(version['id'], images_dir, project_id)

    result = dict(version)
    return jsonify(result)


@app.route('/api/versions/<version_id>', methods=['DELETE'])
def api_delete_version(version_id):
    """Delete a version."""
    version = db_get_version(version_id)
    if not version:
        return jsonify({"error": "version not found"}), 404

    # Delete version and all associated data
    db_delete_version(version_id)

    # Optionally delete version directory
    # (commented out to be safe - user can manually delete)
    # import shutil
    # if os.path.exists(version['images_dir']):
    #     shutil.rmtree(os.path.dirname(version['images_dir']))

    return jsonify({"success": True})


@app.route('/api/versions/<version_id>', methods=['PUT'])
def api_update_version(version_id):
    """Update version metadata."""
    version = db_get_version(version_id)
    if not version:
        return jsonify({"error": "version not found"}), 404

    payload = request.get_json(force=True) or {}
    updates = {}
    if 'name' in payload:
        updates['name'] = payload['name'].strip()
    if 'note' in payload:
        updates['note'] = payload['note'].strip()

    updated = db_update_version(version_id, updates)
    result = dict(updated) if updated else {}
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
    images_dir, _, _, project, version_id, _v = resolve_context()
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

    # 不再每次请求都同步，只在进入版本页面时同步（version_page 中）
    return jsonify(db_list_images(version_id))


@app.route('/image/<name>')
def image(name):
    images_dir, _, _, project, version_id, _v = resolve_context()

    # Try DB first for path
    if project and version_id:
        img = db_get_image(version_id, name)
        if img:
            abs_img_path = _abs_path(img['image_path'])
            if os.path.exists(abs_img_path):
                return send_from_directory(os.path.dirname(abs_img_path), os.path.basename(abs_img_path))

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
    images_dir, _, _, project, version_id, _v = resolve_context()

    if project and version_id:
        lbl = db_get_label(version_id, name)
        if lbl:
            img = db_get_image(version_id, name)
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
        img = db_get_image(version_id, name)
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
    images_dir, _, _, project, version_id, _v = resolve_context()

    payload = request.get_json(force=True)
    if not payload or not isinstance(payload, dict):
        return jsonify({"error": "invalid JSON body"}), 400
    if 'shapes' not in payload or not isinstance(payload['shapes'], list):
        return jsonify({"error": "shapes is required and must be list"}), 400

    if project and version_id:
        img = db_get_image(version_id, name)
        if not img:
            # Try to add image from filesystem if not present
            sync_images_from_filesystem(version_id, images_dir, project['id'])
            img = db_get_image(version_id, name)
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

        db_save_label(version_id, name, shapes_to_save, img['filename'], img['image_width'], img['image_height'])
        return jsonify({"success": True, "saved_to": "database"})

    # Fallback to filesystem (no project context - legacy)
    return jsonify({"error": "project required for label save"}), 400


@app.route('/api/export/yolo', methods=['POST'])
def api_export_yolo():
    _, _, classes, project, version_id, _v = resolve_context()
    payload = request.get_json(force=True) or {}
    out_dir = payload.get('output_dir', os.path.join(config.BASE_DIR, 'yolo_export'))
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    (Path(out_dir) / 'classes.txt').write_text('\n'.join(classes), encoding='utf-8')

    exported = []

    if project and version_id:
        images = db_list_images(version_id)
        for item in images:
            base = item['name']
            lbl = db_get_label(version_id, base)
            img = db_get_image(version_id, base)
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
    images_dir, _, classes, project, version_id, _v = resolve_context()
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

    if project and version_id:
        img = db_get_image(version_id, base)
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

    if project and version_id:
        db_save_label(version_id, base, shapes, image_name, W, H)
        return jsonify({"success": True, "saved_to": "database"})

    return jsonify({"success": True})


@app.route('/api/import/yolo_dir', methods=['POST'])
def api_import_yolo_dir():
    images_dir, _, classes, project, version_id, _v = resolve_context()
    payload = request.get_json(force=True) or {}
    src_dir = payload.get('src_dir', '').strip()

    if not src_dir or not os.path.isdir(src_dir):
        return jsonify({"error": "src_dir required and must exist"}), 400
    if not project or not version_id:
        return jsonify({"error": "project required"}), 400

    imported = 0
    skipped = []

    for txt_file in sorted(f for f in os.listdir(src_dir)
                           if f.endswith('.txt') and f != 'classes.txt'):
        base = os.path.splitext(txt_file)[0]
        img = db_get_image(version_id, base)
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
            db_save_label(version_id, base, shapes, f"{base}.txt", W, H)
            imported += 1
        except Exception:
            skipped.append(base)
            continue

    return jsonify({"success": True, "imported_count": imported, "skipped": skipped})


@app.route('/api/delete_image/<name>', methods=['DELETE'])
def api_delete_image(name):
    _, _, _, project, version_id, _v = resolve_context()
    if project and version_id:
        db_delete_image(version_id, name)
    return jsonify({"success": True})


@app.route('/api/upload_images', methods=['POST'])
def api_upload_images():
    images_dir, _, _, project, version_id, _v = resolve_context()
    files = request.files.getlist('images')
    uploaded = 0
    for f in files:
        if f and f.filename:
            safe_name = secure_filename(f.filename)
            f.save(os.path.join(images_dir, safe_name))
            base, _ = os.path.splitext(safe_name)
            if project and version_id:
                img_path = os.path.join(images_dir, safe_name)
                width, height = None, None
                try:
                    with Image.open(img_path) as im:
                        width, height = im.size
                except Exception:
                    pass
                db_insert_image(version_id, base, safe_name, img_path, width, height, project['id'])
            uploaded += 1
    return jsonify({"success": True, "uploaded": uploaded})


# ── Evaluate page & API ────────────────────────────────────

@app.route('/version/<version_id>/evaluate')
def evaluate_page(version_id):
    version = db_get_version(version_id)
    if not version:
        abort(404)
    return render_template('evaluate.html',
                           classes=version['classes'],
                           base_dir=config.BASE_DIR,
                           version_id=version['id'],
                           project_id=version['project_id'],
                           project_name=version.get('project_name', ''))


@app.route('/version/<version_id>/overview')
def overview_page(version_id):
    version = db_get_version(version_id)
    if not version:
        abort(404)
    return render_template('overview.html',
                           classes=version['classes'],
                           base_dir=config.BASE_DIR,
                           version_id=version['id'],
                           project_id=version['project_id'],
                           project_name=version.get('project_name', ''))


@app.route('/api/overview/<version_id>')
def api_overview(version_id):
    """Dataset overview: total boxes, per-class distribution, per-image stats."""
    version = db_get_version(version_id)
    if not version:
        return jsonify({"error": "version not found"}), 404

    classes = version['classes']
    images = db_list_images(version_id)

    per_class_count = [0] * len(classes)
    per_class_images = [0] * len(classes)
    total_boxes = 0
    labeled_count = 0
    per_image = []

    for item in images:
        lbl = db_get_label(version_id, item['name'])
        box_count = 0
        class_counts_img = [0] * len(classes)
        if lbl and lbl['shapes']:
            labeled_count += 1
            for shape in lbl['shapes']:
                if not isinstance(shape, dict):
                    continue
                cls_idx = shape.get('class_idx', 0)
                if not (0 <= cls_idx < len(classes)):
                    cls_idx = 0
                per_class_count[cls_idx] += 1
                class_counts_img[cls_idx] += 1
                total_boxes += 1
                box_count += 1
        per_image.append({
            'name': item['name'],
            'filename': item['filename'],
            'has_label': item['has_label'],
            'box_count': box_count,
            'class_counts': class_counts_img
        })

    # Update per-class image count
    for idx in range(len(classes)):
        per_class_images[idx] = sum(1 for img in per_image if img['class_counts'][idx] > 0)

    class_stats = []
    for idx, cls in enumerate(classes):
        class_stats.append({
            'class': cls,
            'count': per_class_count[idx],
            'image_count': per_class_images[idx],
            'ratio': (per_class_count[idx] / total_boxes) if total_boxes > 0 else 0.0
        })

    return jsonify({
        'total_images': len(images),
        'labeled_images': labeled_count,
        'unlabeled_images': len(images) - labeled_count,
        'total_boxes': total_boxes,
        'classes': classes,
        'class_stats': class_stats,
        'per_image': per_image
    })


# ── Quality check (similarity + blur) ───────────────────────
def _dhash(img_path, size=8):
    """Compute difference hash (dHash) of an image. Returns 64-bit int."""
    try:
        with Image.open(img_path) as im:
            im = im.convert('L').resize((size + 1, size), Image.LANCZOS)
            pixels = list(im.getdata())
            diff = 0
            for row in range(size):
                for col in range(size):
                    left = pixels[row * (size + 1) + col]
                    right = pixels[row * (size + 1) + col + 1]
                    diff = (diff << 1) | (1 if left > right else 0)
            return diff
    except Exception:
        return None


def _hamming(h1, h2):
    """Hamming distance between two 64-bit hashes."""
    return bin(h1 ^ h2).count('1')


def _blur_score(img_path):
    """Laplacian variance as blur score. Lower = more blurry."""
    try:
        with Image.open(img_path) as im:
            im = im.convert('L').resize((256, 256), Image.LANCZOS)
            lap = im.filter(ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], 1, 0))
            stat = ImageStat.Stat(lap)
            var = stat.var[0]
            return float(var)
    except Exception:
        return None


@app.route('/version/<version_id>/qc')
def qc_page(version_id):
    version = db_get_version(version_id)
    if not version:
        abort(404)
    return render_template('qc.html',
                           classes=version['classes'],
                           base_dir=config.BASE_DIR,
                           version_id=version['id'],
                           project_id=version['project_id'],
                           project_name=version.get('project_name', ''))


@app.route('/api/qc/<version_id>')
def api_qc(version_id):
    """Quality check: find similar image groups and blurry images."""
    version = db_get_version(version_id)
    if not version:
        return jsonify({"error": "version not found"}), 404

    images = db_list_images(version_id)
    results = []

    # Compute hashes and blur scores
    for item in images:
        img = db_get_image(version_id, item['name'])
        if not img:
            continue
        abs_path = _abs_path(img['image_path'])
        if not os.path.exists(abs_path):
            continue
        h = _dhash(abs_path)
        blur = _blur_score(abs_path)
        if h is None and blur is None:
            continue
        results.append({
            'name': item['name'],
            'filename': item['filename'],
            'hash': h,
            'blur_score': blur
        })

    # Find similar groups (Hamming distance <= 5)
    distance_threshold = 5
    similar_groups = []
    assigned = [False] * len(results)
    for i in range(len(results)):
        if assigned[i] or results[i]['hash'] is None:
            continue
        group = [results[i]['name']]
        assigned[i] = True
        for j in range(i + 1, len(results)):
            if assigned[j] or results[j]['hash'] is None:
                continue
            d = _hamming(results[i]['hash'], results[j]['hash'])
            if d <= distance_threshold:
                group.append(results[j]['name'])
                assigned[j] = True
        if len(group) > 1:
            similar_groups.append({
                'images': group,
                'representative': group[0]
            })

    # Blurry images - sorted by score ascending (most blurry first)
    blurry = []
    for r in results:
        if r['blur_score'] is not None:
            blurry.append({
                'name': r['name'],
                'filename': r['filename'],
                'blur_score': r['blur_score']
            })
    blurry.sort(key=lambda x: x['blur_score'])

    return jsonify({
        'total_images': len(images),
        'analyzed_images': len(results),
        'similar_groups': similar_groups,
        'blurry_images': blurry,
        'blur_scores': {
            'min': min((r['blur_score'] for r in results if r['blur_score'] is not None), default=0),
            'max': max((r['blur_score'] for r in results if r['blur_score'] is not None), default=0)
        }
    })


# ── Cross-version similarity (train/test split check) ────────
@app.route('/project/<project_id>/cross-qc')
def cross_qc_page(project_id):
    p = db_get_project(project_id)
    if not p:
        abort(404)
    versions = db_list_versions(project_id)
    return render_template('cross_qc.html',
                           project=p,
                           versions=versions,
                           project_id=project_id,
                           project_name=p.get('name', ''))


@app.route('/api/cross-qc/<project_id>')
def api_cross_qc(project_id):
    """Cross-version similarity: compare train vs test versions."""
    train_vid = request.args.get('train_version_id', '').strip()
    test_vid = request.args.get('test_version_id', '').strip()
    threshold = int(request.args.get('threshold', 10))

    if not train_vid or not test_vid:
        return jsonify({"error": "train_version_id and test_version_id required"}), 400

    train_ver = db_get_version(train_vid)
    test_ver = db_get_version(test_vid)
    if not train_ver or not test_ver:
        return jsonify({"error": "version not found"}), 404

    def _hash_version(vid):
        """Compute dHash for all images in a version, return list of {name, filename, hash}."""
        items = db_list_images(vid)
        result = []
        for item in items:
            img = db_get_image(vid, item['name'])
            if not img:
                continue
            abs_path = _abs_path(img['image_path'])
            if not os.path.exists(abs_path):
                continue
            h = _dhash(abs_path)
            if h is not None:
                result.append({'name': item['name'], 'filename': item['filename'], 'hash': h})
        return result

    train_hashes = _hash_version(train_vid)
    test_hashes = _hash_version(test_vid)

    # Cross-compare: train vs test
    similar_pairs = []
    for t in train_hashes:
        for s in test_hashes:
            d = _hamming(t['hash'], s['hash'])
            if d <= threshold:
                similar_pairs.append({
                    'train_name': t['name'],
                    'train_filename': t['filename'],
                    'test_name': s['name'],
                    'test_filename': s['filename'],
                    'distance': d
                })

    similar_pairs.sort(key=lambda x: x['distance'])

    return jsonify({
        'train_version_id': train_vid,
        'train_version_name': train_ver['name'],
        'train_image_count': len(train_hashes),
        'test_version_id': test_vid,
        'test_version_name': test_ver['name'],
        'test_image_count': len(test_hashes),
        'similar_pairs': similar_pairs,
        'threshold': threshold
    })


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
    version_id = payload.get('version_id', '')
    pred_dir = payload.get('pred_dir', '').strip()
    conf_threshold = float(payload.get('conf_threshold', 0.25))
    iou_threshold = float(payload.get('iou_threshold', 0.5))

    if not pred_dir or not os.path.isdir(pred_dir):
        return jsonify({"error": "pred_dir required and must exist"}), 400
    if not version_id:
        return jsonify({"error": "version_id required"}), 400

    version = db_get_version(version_id)
    if not version:
        return jsonify({"error": "version not found"}), 404

    classes = version['classes']

    pred_files = {os.path.splitext(f)[0]: f for f in os.listdir(pred_dir)
                  if f.endswith('.txt') and f != 'classes.txt'}

    # Union of pred names and all DB images (so images with GT but no pred are included)
    db_image_names = {item['name'] for item in db_list_images(version_id)}
    all_names = sorted(db_image_names | set(pred_files.keys()))

    class_metrics = {cls: {'tp': 0, 'fp': 0, 'fn': 0} for cls in classes}
    images_out = []

    for name in all_names:
        img_row = db_get_image(version_id, name)
        W = (img_row['image_width'] or 640) if img_row else 640
        H = (img_row['image_height'] or 480) if img_row else 480

        # GT from DB labels
        gt_boxes = []
        lbl = db_get_label(version_id, name)
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


@app.route('/api/resize_image/<name>', methods=['POST'])
def api_resize_image(name):
    """Resize image height by ratio (width unchanged). Syncs label coordinates."""
    images_dir, _, _, project, version_id, _v = resolve_context()
    payload = request.get_json(force=True) or {}
    ratio = payload.get('ratio')
    preview = payload.get('preview', False)

    if ratio is None:
        return jsonify({"error": "ratio required"}), 400
    try:
        ratio = float(ratio)
    except (TypeError, ValueError):
        return jsonify({"error": "ratio must be a number"}), 400
    if ratio <= 0 or ratio > 5:
        return jsonify({"error": "ratio must be between 0 and 5"}), 400

    if not project or not version_id:
        return jsonify({"error": "project required"}), 400

    img = db_get_image(version_id, name)
    if not img:
        return jsonify({"error": "image not found"}), 404

    abs_img_path = _abs_path(img['image_path'])
    if not os.path.exists(abs_img_path):
        return jsonify({"error": "image file not found"}), 404

    try:
        with Image.open(abs_img_path) as im:
            orig_w, orig_h = im.size
    except Exception:
        return jsonify({"error": "cannot read image"}), 400

    new_h = int(round(orig_h * ratio))
    if new_h < 1:
        new_h = 1

    if preview:
        # Preview: return what the new dimensions and adjusted shapes would be
        lbl = db_get_label(version_id, name)
        new_shapes = []
        if lbl:
            for shape in lbl['shapes']:
                pts = shape.get('points')
                if pts and len(pts) == 2:
                    new_pts = [
                        [pts[0][0], pts[0][1] * ratio],
                        [pts[1][0], pts[1][1] * ratio]
                    ]
                    new_shapes.append({**shape, "points": new_pts})
        return jsonify({
            "success": True,
            "original_width": orig_w,
            "original_height": orig_h,
            "new_width": orig_w,
            "new_height": new_h,
            "ratio": ratio,
            "shapes_preview": new_shapes
        })

    # Actual resize: modify image file on disk
    try:
        with Image.open(abs_img_path) as im:
            resized = im.resize((orig_w, new_h), Image.LANCZOS)
            resized.save(abs_img_path)
    except Exception as e:
        return jsonify({"error": f"resize failed: {str(e)}"}), 500

    # Update image dimensions in DB
    db_insert_image(version_id, img['name'], img['filename'], abs_img_path, orig_w, new_h, project_id=project['id'])

    # Update label shapes: scale y coordinates
    lbl = db_get_label(version_id, name)
    if lbl:
        new_shapes = []
        for shape in lbl['shapes']:
            pts = shape.get('points')
            if pts and len(pts) == 2:
                new_pts = [
                    [pts[0][0], pts[0][1] * ratio],
                    [pts[1][0], pts[1][1] * ratio]
                ]
                new_shapes.append({**shape, "points": new_pts})
            else:
                new_shapes.append(shape)
        db_save_label(version_id, name, new_shapes, img['filename'], orig_w, new_h)

    return jsonify({
        "success": True,
        "new_width": orig_w,
        "new_height": new_h
    })


@app.route('/api/scale_image/<name>', methods=['POST'])
def api_scale_image(name):
    """Scale image content within fixed canvas. Shrink→black padding, enlarge→crop center. Syncs labels."""
    images_dir, _, _, project, version_id, _v = resolve_context()
    payload = request.get_json(force=True) or {}
    ratio = payload.get('ratio')
    preview = payload.get('preview', False)

    if ratio is None:
        return jsonify({"error": "ratio required"}), 400
    try:
        ratio = float(ratio)
    except (TypeError, ValueError):
        return jsonify({"error": "ratio must be a number"}), 400
    if ratio <= 0 or ratio > 5:
        return jsonify({"error": "ratio must be between 0 and 5"}), 400

    if not project or not version_id:
        return jsonify({"error": "project required"}), 400

    img = db_get_image(version_id, name)
    if not img:
        return jsonify({"error": "image not found"}), 404

    abs_img_path = _abs_path(img['image_path'])
    if not os.path.exists(abs_img_path):
        return jsonify({"error": "image file not found"}), 404

    try:
        with Image.open(abs_img_path) as im:
            W, H = im.size
    except Exception:
        return jsonify({"error": "cannot read image"}), 400

    # Calculate offset: image content centered in original canvas
    offset_x = (W - W * ratio) / 2.0
    offset_y = (H - H * ratio) / 2.0

    if preview:
        lbl = db_get_label(version_id, name)
        new_shapes = []
        if lbl:
            for shape in lbl['shapes']:
                pts = shape.get('points')
                if pts and len(pts) == 2:
                    new_pts = [
                        [pts[0][0] * ratio + offset_x, pts[0][1] * ratio + offset_y],
                        [pts[1][0] * ratio + offset_x, pts[1][1] * ratio + offset_y]
                    ]
                    new_shapes.append({**shape, "points": new_pts})
        return jsonify({
            "success": True,
            "canvas_width": W,
            "canvas_height": H,
            "ratio": ratio,
            "offset_x": offset_x,
            "offset_y": offset_y,
            "shapes_preview": new_shapes
        })

    # Actual scale: canvas stays W×H
    try:
        with Image.open(abs_img_path) as im:
            orig = im.copy()

        if ratio < 1.0:
            # Shrink: place scaled-down image centered on black canvas
            new_w, new_h = int(round(W * ratio)), int(round(H * ratio))
            scaled = orig.resize((new_w, new_h), Image.LANCZOS)
            canvas = Image.new(im.mode if im.mode != 'P' else 'RGB', (W, H), (0, 0, 0))
            paste_x = int(round(offset_x))
            paste_y = int(round(offset_y))
            canvas.paste(scaled, (paste_x, paste_y))
            canvas.save(abs_img_path)
        elif ratio > 1.0:
            # Enlarge: scale up, then crop center to original canvas
            new_w, new_h = int(round(W * ratio)), int(round(H * ratio))
            scaled = orig.resize((new_w, new_h), Image.LANCZOS)
            left = int(round((new_w - W) / 2))
            top = int(round((new_h - H) / 2))
            cropped = scaled.crop((left, top, left + W, top + H))
            cropped.save(abs_img_path)
        # ratio == 1.0: no-op
    except Exception as e:
        return jsonify({"error": f"scale failed: {str(e)}"}), 500

    # Update labels
    lbl = db_get_label(version_id, name)
    if lbl:
        new_shapes = []
        for shape in lbl['shapes']:
            pts = shape.get('points')
            if pts and len(pts) == 2:
                new_pts = [
                    [pts[0][0] * ratio + offset_x, pts[0][1] * ratio + offset_y],
                    [pts[1][0] * ratio + offset_x, pts[1][1] * ratio + offset_y]
                ]
                # Clamp to canvas bounds
                for pt in new_pts:
                    pt[0] = max(0, min(W, pt[0]))
                    pt[1] = max(0, min(H, pt[1]))
                new_shapes.append({**shape, "points": new_pts})
            else:
                new_shapes.append(shape)
        db_save_label(version_id, name, new_shapes, img['filename'], W, H)

    return jsonify({"success": True, "canvas_width": W, "canvas_height": H, "ratio": ratio})


@app.route('/api/eval/save_result', methods=['POST'])
def api_eval_save_result():
    payload = request.get_json(force=True) or {}
    version_id = payload.get('version_id', '')
    name = payload.get('name', '').strip()
    pred_dir = payload.get('pred_dir', '')
    conf_threshold = float(payload.get('conf_threshold', 0.25))
    iou_threshold = float(payload.get('iou_threshold', 0.5))
    metrics = payload.get('metrics', {})
    images = payload.get('images', [])

    if not version_id or not name:
        return jsonify({'error': 'version_id and name required'}), 400

    # Get project_id from version
    version = db_get_version(version_id)
    if not version:
        return jsonify({'error': 'version not found'}), 404

    now = datetime.now().isoformat()
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute('''
            INSERT INTO eval_results (project_id, version_id, name, pred_dir, conf_threshold, iou_threshold,
                                      metrics, images, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (version['project_id'], version_id, name, pred_dir, conf_threshold, iou_threshold,
              json.dumps(metrics, ensure_ascii=False),
              json.dumps(images, ensure_ascii=False), now))
        conn.commit()
    return jsonify({'success': True})


@app.route('/api/eval/list_results')
def api_eval_list_results():
    version_id = request.args.get('version_id', '')
    if not version_id:
        return jsonify([])
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            'SELECT id, name, pred_dir, conf_threshold, iou_threshold, created_at '
            'FROM eval_results WHERE version_id = ? ORDER BY created_at DESC',
            (version_id,)
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


# ── Classification (whole-image labels) ─────────────────────
@app.route('/api/classify/<name>', methods=['POST'])
def api_classify_label(name):
    """Save whole-image classification label (classification mode)."""
    images_dir, _, _, project, version_id, version = resolve_context()
    if not project:
        return jsonify({"error": "project required"}), 400
    if project.get('mode') != 'classification':
        return jsonify({"error": "project is not a classification project"}), 400
    if not version_id:
        return jsonify({"error": "version_id required"}), 400

    payload = request.get_json(force=True) or {}
    class_label = (payload.get('class_label') or '').strip()
    if class_label and class_label not in project['classes']:
        return jsonify({"error": f"unknown class: {class_label}"}), 400

    # Ensure image exists in DB (sync from filesystem if needed)
    img = db_get_image(version_id, name)
    if not img:
        sync_images_from_filesystem(version_id, images_dir, project['id'])
        img = db_get_image(version_id, name)
    if not img:
        return jsonify({"error": "image not found in version"}), 404

    db_save_class_label(version_id, name, class_label,
                        img['filename'], img['image_width'], img['image_height'])
    return jsonify({"success": True, "class_label": class_label or None})


@app.route('/version/<version_id>/evaluate-classify')
def evaluate_classify_page(version_id):
    """Classification evaluation page for a version."""
    version = db_get_version(version_id)
    if not version:
        abort(404)
    project = db_get_project(version['project_id'])
    if not project or project.get('mode') != 'classification':
        abort(404)
    return render_template('evaluate_classify.html',
                           classes=version['classes'],
                           base_dir=config.BASE_DIR,
                           version_id=version_id,
                           project_id=version['project_id'],
                           project_name=version.get('project_name', ''))


@app.route('/api/evaluate_classify', methods=['POST'])
def api_evaluate_classify():
    """Evaluate image classification: read CSV predictions, compare to GT
    whole-image labels, compute accuracy / per-class P/R/F1 / confusion matrix."""
    payload = request.get_json(force=True) or {}
    version_id = payload.get('version_id', '')
    pred_path = payload.get('pred_dir', '').strip()   # reuse pred_dir field for CSV path
    conf_threshold = float(payload.get('conf_threshold', 0.0))

    if not version_id:
        return jsonify({"error": "version_id required"}), 400
    version = db_get_version(version_id)
    if not version:
        return jsonify({"error": "version not found"}), 404
    project = db_get_project(version['project_id'])
    if not project or project.get('mode') != 'classification':
        return jsonify({"error": "project is not a classification project"}), 400

    classes = version['classes']
    if not classes:
        return jsonify({"error": "project has no classes"}), 400

    # Resolve CSV: pred_path may be a .csv file or a directory containing one
    csv_path = None
    if pred_path and os.path.isfile(pred_path) and pred_path.lower().endswith('.csv'):
        csv_path = pred_path
    elif pred_path and os.path.isdir(pred_path):
        for f in sorted(os.listdir(pred_path)):
            if f.lower().endswith('.csv'):
                csv_path = os.path.join(pred_path, f)
                break
    if not csv_path:
        return jsonify({"error": "prediction CSV not found (select a .csv file or a dir with one)"}), 400

    cls_to_idx = {c: i for i, c in enumerate(classes)}
    N = len(classes)

    # Read predictions: image_name, predicted_class, confidence
    preds = {}   # base image name -> (pred_class, conf)
    try:
        with open(csv_path, newline='', encoding='utf-8') as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) < 2:
                    continue
                raw_name = (row[0] or '').strip()
                pred_cls = (row[1] or '').strip()
                conf_str = (row[2] or '').strip() if len(row) >= 3 else ''
                try:
                    conf = float(conf_str) if conf_str else 1.0
                except ValueError:
                    conf = 1.0
                if conf < conf_threshold:
                    continue
                if pred_cls not in cls_to_idx:
                    continue
                # normalize to base name (strip dir + extension) to match DB image.name
                base = os.path.splitext(os.path.basename(raw_name))[0]
                preds[base] = (pred_cls, conf)
    except Exception as e:
        return jsonify({"error": f"failed to read CSV: {e}"}), 400

    # GT + statistics
    images = db_list_images(version_id)
    correct = 0
    total = 0
    confusion = [[0] * N for _ in range(N)]   # [gt_idx][pred_idx]
    per_class_tp = [0] * N
    per_class_fp = [0] * N
    per_class_fn = [0] * N
    unpredictable = 0
    images_out = []

    for img in images:
        name = img['name']
        gt = img.get('class_label')
        if not gt or gt not in cls_to_idx:
            continue   # unlabeled or GT class outside project classes: skip
        total += 1
        gi = cls_to_idx[gt]

        pred_entry = preds.get(name)
        pred_cls = pred_entry[0] if pred_entry else None
        pred_conf = pred_entry[1] if pred_entry else 0.0

        if pred_cls and pred_cls in cls_to_idx:
            pi = cls_to_idx[pred_cls]
            confusion[gi][pi] += 1
            if pred_cls == gt:
                correct += 1
                per_class_tp[gi] += 1
            else:
                per_class_fp[pi] += 1
                per_class_fn[gi] += 1
        else:
            # GT labeled but no valid prediction (missing / below threshold / unknown class)
            unpredictable += 1
            per_class_fn[gi] += 1

        images_out.append({
            'name': name,
            'filename': img.get('filename', name),
            'gt': gt,
            'pred': pred_cls,
            'conf': pred_conf,
            'correct': bool(pred_cls) and pred_cls == gt
        })

    accuracy = correct / total if total else 0.0
    per_class = {}
    for i, c in enumerate(classes):
        tp, fp, fn = per_class_tp[i], per_class_fp[i], per_class_fn[i]
        p = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        r = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * p * r / (p + r) if (p + r) > 0 else 0.0
        per_class[c] = {'precision': p, 'recall': r, 'f1': f1, 'tp': tp, 'fp': fp, 'fn': fn}

    metrics = {
        'accuracy': accuracy,
        'total': total,
        'correct': correct,
        'unpredicted': unpredictable,
        'per_class': per_class,
        'confusion_matrix': confusion
    }
    return jsonify({'images': images_out, 'metrics': metrics, 'classes': classes})


if __name__ == '__main__':
    init_db()
    os.makedirs(config.IMAGES_DIR, exist_ok=True)
    os.makedirs(config.LABELS_DIR, exist_ok=True)
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
