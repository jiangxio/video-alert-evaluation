#!/usr/bin/env python3
"""
迁移脚本：从文件系统迁移数据到数据库
使用方法: python migrate_to_db.py
"""

import os
import sys
import json
from pathlib import Path
from datetime import datetime
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config


def init_db():
    """初始化数据库 schema"""
    import sqlite3
    DB_FILE = os.path.join(config.DB_DIR, 'annotations.db')

    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()

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

        conn.commit()
    return DB_FILE


def migrate():
    DB_FILE = init_db()
    import sqlite3

    # 检查 projects.json 是否存在
    projects_file = os.path.join(config.BASE_DIR, 'projects.json')
    if not os.path.exists(projects_file):
        print(f"❌ projects.json 不存在: {projects_file}")
        return

    # 读取 projects.json
    with open(projects_file, 'r', encoding='utf-8') as f:
        projects_data = json.load(f)

    projects = projects_data.get('projects', [])
    if not projects:
        print("⚠️  projects.json 中没有项目")
        return

    total_projects = 0
    total_images = 0
    total_labels = 0
    total_backups = 0

    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        for p in projects:
            project_id = p['id']
            print(f"\n📦 处理项目: {p['name']} ({project_id})")

            # 检查项目是否已存在
            cursor.execute('SELECT id FROM projects WHERE id = ?', (project_id,))
            if cursor.fetchone():
                print(f"   ⏭️  项目已存在，跳过")
                continue

            # 插入项目
            now = datetime.now().isoformat()
            cursor.execute('''
                INSERT INTO projects (id, name, classes, images_dir, labels_dir, labels_format, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                project_id,
                p['name'],
                json.dumps(p.get('classes', config.CLASSES), ensure_ascii=False),
                p['images_dir'],
                p['labels_dir'],
                p.get('labels_format', 'json'),
                p.get('created_at', now),
                now
            ))
            total_projects += 1
            print(f"   ✅ 项目已创建")

            # 扫描并插入图片
            images_dir = p['images_dir']
            if not os.path.exists(images_dir):
                print(f"   ⚠️  图片目录不存在: {images_dir}")
                continue

            image_files = []
            try:
                image_files = sorted([f for f in os.listdir(images_dir)
                                       if f.lower().endswith(tuple(config.IMAGE_EXTS))])
            except Exception as e:
                print(f"   ❌ 读取图片目录失败: {e}")
                continue

            for filename in image_files:
                base, _ = os.path.splitext(filename)
                img_path = os.path.join(images_dir, filename)
                width, height = None, None
                try:
                    with Image.open(img_path) as im:
                        width, height = im.size
                except Exception:
                    pass

                cursor.execute('''
                    INSERT OR IGNORE INTO images (project_id, name, filename, image_path, image_width, image_height, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (project_id, base, filename, img_path, width, height, now))
                total_images += 1

            print(f"   ✅ 图片已导入: {len(image_files)} 张")

            # 扫描并插入标注
            labels_dir = p['labels_dir']
            if os.path.exists(labels_dir):
                label_files = []
                try:
                    label_files = sorted([f for f in os.listdir(labels_dir) if f.endswith('.json')])
                except Exception:
                    pass

                for label_file in label_files:
                    base, _ = os.path.splitext(label_file)

                    # 获取对应的图片记录
                    cursor.execute('SELECT id, filename, image_width, image_height FROM images WHERE project_id = ? AND name = ?', (project_id, base))
                    img_row = cursor.fetchone()
                    if not img_row:
                        continue

                    img_id, img_filename, img_w, img_h = img_row

                    # 读取标注文件
                    try:
                        with open(os.path.join(labels_dir, label_file), 'r', encoding='utf-8') as f:
                            label_data = json.load(f)
                    except Exception:
                        continue

                    shapes = label_data.get('shapes', [])
                    shapes_json = json.dumps(shapes, ensure_ascii=False)

                    try:
                        cursor.execute('''
                            INSERT INTO labels (project_id, image_id, image_path, image_width, image_height, shapes, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        ''', (project_id, img_id, img_filename, img_w, img_h, shapes_json, now))
                        total_labels += 1
                    except sqlite3.IntegrityError:
                        pass

                print(f"   ✅ 标注已导入: {total_labels} 个")

                # 扫描并插入备份
                backup_dir = os.path.join(labels_dir, 'backup')
                if os.path.exists(backup_dir):
                    backup_files = []
                    try:
                        backup_files = sorted([f for f in os.listdir(backup_dir) if f.endswith('.json')])
                    except Exception:
                        pass

                    for backup_file in backup_files:
                        # 从备份文件名中提取图片名: 0001_20260327162524.json -> 0001
                        parts = backup_file.rsplit('_', 1)
                        if len(parts) != 2:
                            base = os.path.splitext(backup_file)[0]
                        else:
                            base = parts[0]

                        cursor.execute('SELECT id FROM images WHERE project_id = ? AND name = ?', (project_id, base))
                        img_row = cursor.fetchone()
                        if not img_row:
                            continue

                        img_id = img_row[0]

                        try:
                            with open(os.path.join(backup_dir, backup_file), 'r', encoding='utf-8') as f:
                                backup_data = json.load(f)
                        except Exception:
                            continue

                        shapes = backup_data.get('shapes', [])
                        shapes_json = json.dumps(shapes, ensure_ascii=False)

                        # 从文件名解析时间戳，如果失败用当前时间
                        created_at = now
                        try:
                            if len(parts) == 2:
                                ts_part = os.path.splitext(parts[1])[0]
                                # 尝试解析为 ISO 格式或直接使用
                                created_at = datetime.strptime(ts_part, '%Y%m%d%H%M%S').isoformat()
                        except Exception:
                            pass

                        cursor.execute('''
                            INSERT INTO label_backups (project_id, image_id, shapes, created_at)
                            VALUES (?, ?, ?, ?)
                        ''', (project_id, img_id, shapes_json, created_at))
                        total_backups += 1

                    print(f"   ✅ 备份已导入: {len(backup_files)} 个")

        conn.commit()

    print(f"\n{'='*60}")
    print(f"✅ 迁移完成！")
    print(f"   - 项目: {total_projects} 个")
    print(f"   - 图片: {total_images} 张")
    print(f"   - 标注: {total_labels} 个")
    print(f"   - 备份: {total_backups} 个")
    print(f"   - 数据库: {DB_FILE}")
    print(f"{'='*60}\n")


if __name__ == '__main__':
    print("="*60)
    print("目标检测标注平台 - 数据迁移脚本")
    print("从文件系统迁移到 SQLite 数据库")
    print("="*60)
    migrate()
