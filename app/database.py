"""数据库初始化和连接管理"""
import os
import json
import sqlite3
from pathlib import Path
from flask import g, current_app

# 支持通过环境变量指定数据库路径（Docker 持久化卷挂载到该路径）
DATABASE_PATH = Path(os.environ.get('DATABASE_PATH', Path(__file__).parent.parent / 'benchmark.db'))


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


def _seed_event_types(cursor):
    """从硬编码注册表和 config/alert_types.json 播种 event_types 表"""
    from app.event_types import (
        EVENT_TYPES,
        TYPE_NAMES,
        TYPE_DESCRIPTIONS,
        TYPE_TAG_COLORS,
    )

    config_path = Path(__file__).parent.parent / 'config' / 'alert_types.json'
    id_to_key = {}
    if config_path.exists():
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split(maxsplit=1)
                    if len(parts) == 2:
                        alert_id, alert_type = parts
                        id_to_key[int(alert_id)] = alert_type
        except Exception:
            pass

    key_to_id = {v: k for k, v in id_to_key.items()}

    # 硬编码列表中的类型优先播种
    for idx, key in enumerate(EVENT_TYPES):
        et_id = key_to_id.get(key)
        if et_id is None:
            et_id = 100 + idx
        name = TYPE_NAMES.get(key, key)
        description = TYPE_DESCRIPTIONS.get(key, '')
        bg_color, fg_color = TYPE_TAG_COLORS.get(key, ('#e0e0e0', '#333333'))
        cursor.execute(
            '''
            INSERT OR IGNORE INTO event_types
            (id, key, name, description, bg_color, fg_color, tags, sort_order)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (et_id, key, name, description, bg_color, fg_color, '[]', idx)
        )

    # config 中存在但硬编码中没有的类型，用占位数据播种
    existing_keys = set(EVENT_TYPES)
    for alert_id, alert_key in id_to_key.items():
        if alert_key in existing_keys:
            continue
        cursor.execute(
            '''
            INSERT OR IGNORE INTO event_types
            (id, key, name, description, bg_color, fg_color, tags, sort_order)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (alert_id, alert_key, alert_key, '', '#e0e0e0', '#333333', '[]', alert_id)
        )


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

    # 告警评测集表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS eval_alert_sets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            notes TEXT,
            dataset_ids TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 为 eval_alert_sets 追加 dataset_ids 列（兼容之前按图片保存的版本）
    try:
        cursor.execute('ALTER TABLE eval_alert_sets ADD COLUMN dataset_ids TEXT')
    except Exception:
        pass  # 列已存在

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
            alert_eval_set_id INTEGER REFERENCES eval_alert_sets(id),
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

    # 为 eval_tasks 追加 alert_eval_set_id 列（兼容已有数据）
    try:
        cursor.execute('ALTER TABLE eval_tasks ADD COLUMN alert_eval_set_id INTEGER REFERENCES eval_alert_sets(id)')
    except Exception:
        pass  # 列已存在

    # 为 eval_tasks 追加 min_event_duration_sec 列（兼容已有数据）
    try:
        cursor.execute('ALTER TABLE eval_tasks ADD COLUMN min_event_duration_sec REAL DEFAULT 0')
    except Exception:
        pass  # 列已存在

    # 为 eval_tasks 追加 error_message 列（兼容已有数据，用于记录评测执行异常原因）
    try:
        cursor.execute('ALTER TABLE eval_tasks ADD COLUMN error_message TEXT')
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
        'ai_suggestion TEXT',
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

    # 为 eval_tasks 追加 finalized/accuracy/recall/event_metrics/confirmed_at 列（兼容已有数据）
    for col_def in [
        'finalized INTEGER DEFAULT 0',
        'accuracy REAL',
        'recall REAL',
        'avg_fp_per_hour REAL',
        'event_metrics TEXT',
        'confirmed_at TIMESTAMP',
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

    # 为 watermarked_videos 追加 OCR 验证状态字段
    try:
        cursor.execute('ALTER TABLE watermarked_videos ADD COLUMN ocr_check_status TEXT')
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

    # 自动化标注任务表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS auto_annotation_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_db_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            video_id TEXT NOT NULL,
            status TEXT DEFAULT 'queued',
            frame_interval_sec INTEGER NOT NULL DEFAULT 1,
            merge_interval_sec INTEGER NOT NULL DEFAULT 5,
            event_types TEXT NOT NULL,
            total_frames INTEGER DEFAULT 0,
            analyzed_frames INTEGER DEFAULT 0,
            current_phase TEXT DEFAULT 'queued',
            phase_progress INTEGER DEFAULT 0,
            result_json_path TEXT,
            error_message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP
        )
    ''')

    # 逐帧分析结果表（中间数据）
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS auto_annotation_frames (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES auto_annotation_tasks(id) ON DELETE CASCADE,
            timestamp_sec REAL NOT NULL,
            frame_path TEXT NOT NULL,
            detected_event_types TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 测前分析记录表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pre_analysis_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            eval_video_set_id INTEGER NOT NULL REFERENCES eval_video_sets(id),
            merge_interval_sec REAL DEFAULT 5.0,
            event_interval_sec REAL DEFAULT 10.0,
            trigger_rate REAL DEFAULT 0.5,
            min_event_duration_sec REAL DEFAULT 0,
            result_json TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 数据集图片操作日志表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS dataset_image_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dataset_id INTEGER REFERENCES datasets(id),
            action TEXT NOT NULL,
            image_count INTEGER NOT NULL,
            details TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 报告 AI 对话历史表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS report_chat_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL REFERENCES eval_tasks(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            messages TEXT NOT NULL,
            summary_text TEXT,
            conclusion_text TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_report_chat_task ON report_chat_sessions(task_id)')

    # 推流任务表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS stream_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            source_type TEXT NOT NULL,
            source_id INTEGER NOT NULL,
            stream_name TEXT NOT NULL,
            loop_count INTEGER DEFAULT 1,
            status TEXT DEFAULT 'created',
            total_duration REAL,
            suggested_algorithms TEXT,
            error_message TEXT,
            pid INTEGER,
            log_path TEXT,
            resume_video_index INTEGER,
            resume_offset REAL,
            resume_loop INTEGER,
            resume_at TIMESTAMP,
            restart_count INTEGER DEFAULT 0,
            max_restarts INTEGER DEFAULT 3,
            last_error TEXT,
            current_video_index INTEGER,
            current_loop INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP,
            ended_at TIMESTAMP
        )
    ''')

    # 视频抽帧任务表（视频转图片用于测试模型，支持批量）
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS extracted_frames_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wm_ids TEXT,
            video_id TEXT NOT NULL,
            video_count INTEGER DEFAULT 1,
            target_width INTEGER,
            interval_sec REAL DEFAULT 1.0,
            include_normal INTEGER DEFAULT 0,
            status TEXT DEFAULT 'running',
            frame_count INTEGER DEFAULT 0,
            output_dir TEXT,
            error_message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # 兼容已存在的表：补 video_count / wm_ids 列
    try:
        cursor.execute('ALTER TABLE extracted_frames_tasks ADD COLUMN video_count INTEGER DEFAULT 1')
    except Exception:
        pass
    try:
        cursor.execute('ALTER TABLE extracted_frames_tasks ADD COLUMN wm_ids TEXT')
    except Exception:
        pass

    # 算法版本表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS algorithm_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            algorithm_type TEXT NOT NULL,
            name TEXT NOT NULL,
            version_date TEXT NOT NULL,
            description TEXT,
            config_file_path TEXT,
            algorithm_file_path TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 事件类型注册表（算法类型）
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS event_types (
            id INTEGER PRIMARY KEY,
            key TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            description TEXT,
            bg_color TEXT NOT NULL DEFAULT '#e0e0e0',
            fg_color TEXT NOT NULL DEFAULT '#333333',
            tags TEXT NOT NULL DEFAULT '[]',
            sort_order INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 从硬编码注册表 + config/alert_types.json 播种 event_types
    _seed_event_types(cursor)

    # 数据集算法版本关联表（带历史记录）
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS dataset_algorithm_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dataset_id INTEGER NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
            algorithm_version_id INTEGER NOT NULL REFERENCES algorithm_versions(id) ON DELETE CASCADE,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 为 eval_tasks 追加 algorithm_versions 字段（兼容已有数据）
    try:
        cursor.execute('ALTER TABLE eval_tasks ADD COLUMN algorithm_versions TEXT')
    except Exception:
        pass  # 列已存在

    # 为 stream_tasks 追加 log_path 字段（兼容已有数据）
    try:
        cursor.execute('ALTER TABLE stream_tasks ADD COLUMN log_path TEXT')
    except Exception:
        pass  # 列已存在

    # 为 stream_tasks 追加 resume 字段（兼容已有数据）
    resume_cols = [
        ('resume_video_index', 'INTEGER'),
        ('resume_offset', 'REAL'),
        ('resume_loop', 'INTEGER'),
        ('resume_at', 'TIMESTAMP'),
        ('restart_count', 'INTEGER'),
        ('max_restarts', 'INTEGER'),
        ('last_error', 'TEXT'),
        ('current_video_index', 'INTEGER'),
        ('current_loop', 'INTEGER'),
    ]
    for col, col_type in resume_cols:
        try:
            cursor.execute(f'ALTER TABLE stream_tasks ADD COLUMN {col} {col_type}')
        except Exception:
            pass  # 列已存在

    # 为 eval_tasks 追加 duration_hours 列（实时模式采集时长）
    try:
        cursor.execute('ALTER TABLE eval_tasks ADD COLUMN duration_hours REAL')
    except Exception:
        pass  # 列已存在

    # 为 datasets 追加 mode 列（数据集模式）
    try:
        cursor.execute("ALTER TABLE datasets ADD COLUMN mode TEXT DEFAULT 'normal'")
    except Exception:
        pass  # 列已存在

    # AI 助手设置表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS assistant_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            openai_api_key TEXT,
            openai_base_url TEXT DEFAULT 'https://api.openai.com/v1',
            openai_model TEXT DEFAULT 'gpt-4o-mini',
            max_messages_per_session INTEGER DEFAULT 50,
            max_write_actions_per_session INTEGER DEFAULT 30,
            confirmation_ttl_seconds INTEGER DEFAULT 300,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # 统一 API Token 配置表（非敏感项；密钥仅存 .env，此处只存“是否已配置”标记）
    # 字段按“能力角色”分组：openai_* = 文本逻辑组，vision_* = 多模态审查组；
    # claude_* 字段保留但已停用（①②报告生成已迁移到 OpenAI 协议）。
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS api_config (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            openai_base_url TEXT,
            openai_model TEXT DEFAULT 'gpt-4o-mini',
            openai_request_interval_sec INTEGER DEFAULT 1,
            claude_base_url TEXT,
            claude_model TEXT DEFAULT 'claude-sonnet-5',
            openai_key_configured INTEGER DEFAULT 0,
            claude_key_configured INTEGER DEFAULT 0,
            vision_base_url TEXT,
            vision_model TEXT DEFAULT 'Qwen3-VL-8B-Instruct',
            vision_request_interval_sec INTEGER DEFAULT 1,
            vision_key_configured INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # 为 api_config 追加 vision_* 列（兼容已有库）
    for col_def in [
        'vision_base_url TEXT',
        "vision_model TEXT DEFAULT 'Qwen3-VL-8B-Instruct'",
        'vision_request_interval_sec INTEGER DEFAULT 1',
        'vision_key_configured INTEGER DEFAULT 0',
    ]:
        try:
            cursor.execute(f'ALTER TABLE api_config ADD COLUMN {col_def}')
        except Exception:
            pass  # 列已存在

    # AI 助手待确认操作表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pending_confirmations (
            id TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            params TEXT NOT NULL,
            summary TEXT NOT NULL,
            session_id TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_pending_confirmations_session ON pending_confirmations(session_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_pending_confirmations_expires ON pending_confirmations(expires_at)')

    # AI 助手审计日志表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS assistant_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            action TEXT NOT NULL,
            params TEXT,
            result TEXT,
            status TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_assistant_audit_session ON assistant_audit_log(session_id)')

    # AI 助手统一任务表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS assistant_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_type TEXT NOT NULL,
            ref_type TEXT,
            ref_id INTEGER,
            status TEXT DEFAULT 'pending',
            params TEXT,
            result_summary TEXT,
            error_message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_assistant_tasks_status ON assistant_tasks(status)')

    # AI 助手对话历史表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS assistant_conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            tool_calls TEXT,
            tool_call_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_assistant_conversations_session ON assistant_conversations(session_id)')
    # 为旧库追加 tool_call_id 列（tool 消息的配对 ID，缺失会导致 API 400）
    try:
        cursor.execute('ALTER TABLE assistant_conversations ADD COLUMN tool_call_id TEXT')
    except Exception:
        pass  # 列已存在

    # 常用查询索引
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_alert_images_dataset ON alert_images(dataset_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_ocr_alert_image ON ocr_results(alert_image_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_events_video_db ON events(video_db_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_watermarked_original ON watermarked_videos(original_video_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_gt_frames_event ON gt_frames(event_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_eval_merged_task ON eval_merged_events(task_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_eval_gt_task ON eval_gt_events(task_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_auto_anno_task_video ON auto_annotation_tasks(video_db_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_auto_anno_frames_task ON auto_annotation_frames(task_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_pre_analysis_set ON pre_analysis_records(eval_video_set_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_dataset_image_logs_dataset ON dataset_image_logs(dataset_id)')

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
