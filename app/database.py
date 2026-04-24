"""数据库初始化和连接管理"""
import sqlite3
from pathlib import Path
from flask import g, current_app

DATABASE_PATH = Path(__file__).parent.parent / 'benchmark.db'


def get_db():
    """获取数据库连接"""
    if 'db' not in g:
        g.db = sqlite3.connect(
            str(DATABASE_PATH),
            detect_types=sqlite3.PARSE_DECLTYPES
        )
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(e=None):
    """关闭数据库连接"""
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    """初始化数据库表"""
    db = sqlite3.connect(str(DATABASE_PATH))
    cursor = db.cursor()

    # 原始视频表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            original_path TEXT NOT NULL,
            video_id TEXT,
            file_size INTEGER,
            duration REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 水印视频表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS watermarked_videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_video_id INTEGER REFERENCES videos(id),
            filename TEXT NOT NULL,
            output_path TEXT NOT NULL,
            file_size INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 告警图片表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS alert_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            file_path TEXT NOT NULL,
            alert_type_id TEXT,
            alert_type TEXT,
            file_size INTEGER,
            uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # OCR结果表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ocr_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_image_id INTEGER REFERENCES alert_images(id),
            raw_ocr_text TEXT,
            video_id TEXT,
            timestamp TEXT,
            timestamp_seconds REAL,
            success BOOLEAN,
            full_result TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 验证结果表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS verification_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_image_id INTEGER REFERENCES alert_images(id),
            ocr_result_id INTEGER REFERENCES ocr_results(id),
            verdict TEXT,
            reason TEXT,
            ground_truth_file TEXT,
            matched_event TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Ground Truth表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ground_truth (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            start_seconds REAL NOT NULL,
            end_seconds REAL NOT NULL,
            source_file TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 告警数据集表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS datasets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 视频事件标注表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_db_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            event_type TEXT NOT NULL,
            start_seconds REAL NOT NULL,
            end_seconds REAL NOT NULL,
            gt_frames_status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 为 events 追加 gt_frames_status 列（兼容已有数据）
    try:
        cursor.execute('ALTER TABLE events ADD COLUMN gt_frames_status TEXT DEFAULT "pending"')
    except Exception:
        pass  # 列已存在

    # 为常用表追加 updated_at 列（兼容已有数据）
    for table in ['events', 'gt_frames', 'eval_tasks', 'videos']:
        try:
            cursor.execute(f'ALTER TABLE {table} ADD COLUMN updated_at TIMESTAMP')
        except Exception:
            pass  # 列已存在

    # 对 videos.filename 加唯一索引（防止重复上传）
    cursor.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS idx_videos_filename ON videos(filename)
    ''')

    # 为 alert_images 追加新列（兼容已有数据）
    for col_def in [
        'dataset_id INTEGER REFERENCES datasets(id)',
        'image_width INTEGER',
        'image_height INTEGER',
        'event_label TEXT',
    ]:
        col_name = col_def.split()[0]
        try:
            cursor.execute(f'ALTER TABLE alert_images ADD COLUMN {col_def}')
        except Exception:
            pass  # 列已存在

    # 评测任务表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS eval_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            notes TEXT,
            dataset_id INTEGER REFERENCES datasets(id),
            eval_set_id INTEGER REFERENCES eval_video_sets(id),
            merge_interval_sec REAL DEFAULT 5.0,
            event_start_sec REAL DEFAULT 5.0,
            event_end_sec REAL DEFAULT 60.0,
            event_interval_sec REAL DEFAULT 10.0,
            trigger_rate REAL DEFAULT 0.5,
            min_event_duration_sec REAL DEFAULT 0,
            status TEXT DEFAULT 'created',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 为 eval_tasks 追加 eval_set_id 列（兼容已有数据）
    try:
        cursor.execute('ALTER TABLE eval_tasks ADD COLUMN eval_set_id INTEGER REFERENCES eval_video_sets(id)')
    except Exception:
        pass  # 列已存在

    # 为 eval_tasks 追加 min_event_duration_sec 列（兼容已有数据）
    try:
        cursor.execute('ALTER TABLE eval_tasks ADD COLUMN min_event_duration_sec REAL DEFAULT 0')
    except Exception:
        pass  # 列已存在

    # 合并事件表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS eval_merged_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER REFERENCES eval_tasks(id),
            video_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            start_sec REAL,
            end_sec REAL,
            expected_count INTEGER,
            confirmed_count INTEGER,
            image_ids TEXT,
            confirmed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 评测结果表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS eval_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER REFERENCES eval_tasks(id),
            merged_event_id INTEGER REFERENCES eval_merged_events(id),
            alert_image_id INTEGER REFERENCES alert_images(id),
            is_false_positive BOOLEAN DEFAULT 0,
            is_missed BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # GT 帧表（标注事件后每秒截一帧）
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS gt_frames (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_db_id INTEGER REFERENCES videos(id),
            event_id INTEGER REFERENCES events(id),
            event_type TEXT NOT NULL,
            timestamp_sec REAL NOT NULL,
            file_path TEXT NOT NULL,
            filename TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 为 gt_frames 追加 event_id 列（兼容已有数据）
    try:
        cursor.execute('ALTER TABLE gt_frames ADD COLUMN event_id INTEGER REFERENCES events(id)')
    except Exception:
        pass  # 列已存在

    # 评测视频集表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS eval_video_sets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            notes TEXT,
            video_ids TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 为 eval_merged_events 追加新列（兼容已有数据）
    for col_def in [
        'ts_start REAL',
        'ts_end REAL',
        'representative_image_id INTEGER',
        'is_false_positive INTEGER DEFAULT 0',
        'matched_gt_event_id INTEGER',
        'manual_status TEXT DEFAULT "auto"',
    ]:
        col_name = col_def.split()[0]
        try:
            cursor.execute(f'ALTER TABLE eval_merged_events ADD COLUMN {col_def}')
        except Exception:
            pass  # 列已存在

    # GT 事件得分表（评测任务的 GT 事件视角）
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS eval_gt_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER REFERENCES eval_tasks(id),
            gt_event_id INTEGER REFERENCES events(id),
            video_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            start_sec REAL,
            end_sec REAL,
            expected_count INTEGER DEFAULT 1,
            confirmed_count INTEGER DEFAULT 1,
            actual_count INTEGER DEFAULT 0,
            mid_frame_id INTEGER REFERENCES gt_frames(id),
            mid_frame_path TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 为 eval_tasks 追加 finalized/accuracy/recall/event_metrics 列（兼容已有数据）
    for col_def in [
        'finalized INTEGER DEFAULT 0',
        'accuracy REAL',
        'recall REAL',
        'event_metrics TEXT',
    ]:
        try:
            cursor.execute(f'ALTER TABLE eval_tasks ADD COLUMN {col_def}')
        except Exception:
            pass  # 列已存在

    # 为 watermarked_videos 追加封面、分辨率、时长字段（兼容已有数据）
    for col_def in [
        'thumbnail_path TEXT',
        'resolution TEXT',
        'duration REAL',
    ]:
        try:
            cursor.execute(f'ALTER TABLE watermarked_videos ADD COLUMN {col_def}')
        except Exception:
            pass  # 列已存在

    # 为 videos 表追加 video_id_confirmed 字段（兼容已有数据）
    try:
        cursor.execute('ALTER TABLE videos ADD COLUMN video_id_confirmed INTEGER DEFAULT 0')
    except Exception:
        pass  # 列已存在

    # 生成视频表（存储拼接/打包结果）
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS generated_videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_size INTEGER,
            source_video_ids TEXT,
            status TEXT DEFAULT 'processing',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 常用查询索引
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_alert_images_dataset ON alert_images(dataset_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_ocr_alert_image ON ocr_results(alert_image_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_events_video_db ON events(video_db_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_watermarked_original ON watermarked_videos(original_video_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_gt_frames_event ON gt_frames(event_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_eval_merged_task ON eval_merged_events(task_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_eval_gt_task ON eval_gt_events(task_id)')

    db.commit()
    db.close()


def import_ground_truth():
    """导入现有的ground truth JSON文件到数据库"""
    import json
    from pathlib import Path

    db = get_db()
    cursor = db.cursor()

    gt_dir = Path(current_app.config['GROUND_TRUTH_DIR'])
    if not gt_dir.exists():
        return

    for gt_file in gt_dir.glob('*.json'):
        try:
            with open(gt_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            video_id = data.get('id', '').replace('001', '')[:3]  # 从 "046001" 提取 "046"
            events = data.get('events', [])

            for event in events:
                # 检查是否已存在
                cursor.execute('''
                    SELECT id FROM ground_truth
                    WHERE video_id = ? AND event_type = ?
                    AND start_seconds = ? AND end_seconds = ?
                ''', (video_id, event.get('type'), event.get('start'), event.get('end')))

                if not cursor.fetchone():
                    cursor.execute('''
                        INSERT INTO ground_truth
                        (video_id, event_type, start_seconds, end_seconds, source_file)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (video_id, event.get('type'), event.get('start'),
                          event.get('end'), str(gt_file)))

        except Exception as e:
            print(f"Error importing {gt_file}: {e}")
            continue

    db.commit()


def init_app(app):
    """初始化应用的数据库"""
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db()
        import_ground_truth()
