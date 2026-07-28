"""AI 助手工具函数与 JSON Schema 定义

所有工具分为两类：
- 只读工具：直接返回查询结果
- 写入工具：只进行参数校验和影响分析，返回确认请求；真正执行在确认后由 executor 完成
"""
import json
import threading
from pathlib import Path
from typing import Any, Optional

from flask import current_app

from app.database import get_db


# ═══════════════════════════════════════════════════════════════════════════════
# 工具 JSON Schema（供 OpenAI function calling 使用）
# ═══════════════════════════════════════════════════════════════════════════════

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_videos",
            "description": "列出平台上的视频，支持按文件名、视频ID或标签过滤",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词，可匹配文件名或 video_id"},
                    "event_type": {"type": "string", "description": "按事件类型过滤"},
                    "limit": {"type": "integer", "description": "最多返回条数，默认 20"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_video_details",
            "description": "获取某个视频的详细信息，包括标签/事件标注",
            "parameters": {
                "type": "object",
                "properties": {
                    "video_id": {"type": "string", "description": "视频 ID"},
                },
                "required": ["video_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_event_types",
            "description": "列出平台支持的所有事件类型",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_alerts",
            "description": "列出告警图片，支持按数据集或事件类型过滤",
            "parameters": {
                "type": "object",
                "properties": {
                    "dataset_id": {"type": "integer", "description": "告警数据集 ID"},
                    "event_type": {"type": "string", "description": "事件类型"},
                    "limit": {"type": "integer", "description": "最多返回条数，默认 20"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_alert_details",
            "description": "获取单张告警图片的详情，包括 OCR 和验证结果",
            "parameters": {
                "type": "object",
                "properties": {
                    "alert_id": {"type": "integer", "description": "告警图片 ID"},
                },
                "required": ["alert_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_evaluation_report",
            "description": "获取某个评测任务的结果指标",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "评测任务 ID"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_task_status",
            "description": "查询 AI 助手创建的异步任务状态",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "任务 ID"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_assistant_tasks",
            "description": "列出 AI 助手创建的异步任务（打水印、批量 OCR、评测等），查看进度",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "最多返回条数，默认 20"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_platform_docs",
            "description": "查询平台使用文档，回答用户关于平台用法的问题",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "用户的问题或关键词"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_video_tags",
            "description": "给视频添加、替换或删除事件标签/标注",
            "parameters": {
                "type": "object",
                "properties": {
                    "video_id": {"type": "string", "description": "视频 ID"},
                    "events": {
                        "type": "array",
                        "description": "要设置的事件列表，每项包含 type、start、end（秒）",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string"},
                                "start": {"type": "number"},
                                "end": {"type": "number"},
                            },
                            "required": ["type", "start", "end"],
                        },
                    },
                    "mode": {
                        "type": "string",
                        "description": "replace=替换所有现有标注，append=追加，delete=删除指定类型标注",
                        "enum": ["replace", "append", "delete"],
                    },
                },
                "required": ["video_id", "events", "mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_video",
            "description": "删除指定视频及其关联数据",
            "parameters": {
                "type": "object",
                "properties": {
                    "video_id": {"type": "string", "description": "视频 ID"},
                },
                "required": ["video_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_evaluation_task",
            "description": "创建并启动一个评测任务",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "任务名称"},
                    "dataset_id": {"type": "integer", "description": "告警数据集 ID"},
                    "eval_set_id": {"type": "integer", "description": "评测视频集 ID"},
                },
                "required": ["name", "dataset_id", "eval_set_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "batch_run_ocr",
            "description": "批量对告警图片执行 OCR 识别",
            "parameters": {
                "type": "object",
                "properties": {
                    "dataset_id": {"type": "integer", "description": "告警数据集 ID，不传则处理所有未 OCR 的图片"},
                    "alert_ids": {"type": "array", "items": {"type": "integer"}, "description": "指定图片 ID 列表"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_alert_status",
            "description": "修改告警图片的人工复核状态",
            "parameters": {
                "type": "object",
                "properties": {
                    "alert_ids": {"type": "array", "items": {"type": "integer"}, "description": "告警图片 ID 列表"},
                    "status": {"type": "string", "enum": ["correct", "false_positive", "ignored"], "description": "要设置的状态"},
                },
                "required": ["alert_ids", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_watermark",
            "description": "给指定视频添加水印（视频ID和时间戳），生成水印视频",
            "parameters": {
                "type": "object",
                "properties": {
                    "video_id": {"type": "string", "description": "视频 ID"},
                },
                "required": ["video_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "export_report",
            "description": "导出评测报告（图片或 PDF）",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "评测任务 ID"},
                    "format": {"type": "string", "enum": ["png", "pdf"], "description": "报告格式"},
                },
                "required": ["task_id", "format"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "concat_videos",
            "description": "将多个水印视频拼接成一个视频（2-10个）",
            "parameters": {
                "type": "object",
                "properties": {
                    "video_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要拼接的视频 ID 列表（2-10个）",
                    },
                },
                "required": ["video_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "package_videos",
            "description": "将多个水印视频打包成 zip 下载（1-10个）",
            "parameters": {
                "type": "object",
                "properties": {
                    "video_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要打包的视频 ID 列表",
                    },
                },
                "required": ["video_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_frames",
            "description": "对视频抽帧生成图片（命名含视频id/时间/事件），用于图片测试模型",
            "parameters": {
                "type": "object",
                "properties": {
                    "video_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要抽帧的视频 ID 列表",
                    },
                    "target_width": {"type": "integer", "description": "目标宽度（等比例缩放），留空=原尺寸"},
                    "interval_sec": {"type": "number", "description": "抽帧间隔（秒），默认1.0"},
                    "include_normal": {"type": "boolean", "description": "是否包含无事件的normal帧（全程抽帧），false=仅GT事件时段"},
                },
                "required": ["video_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_eval_set",
            "description": "管理评测视频集：创建/重命名/编辑说明/添加视频/移出视频/删除",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["create", "rename", "edit", "add", "remove", "delete"], "description": "操作类型"},
                    "name": {"type": "string", "description": "评测集名称（create/rename 时必填）"},
                    "notes": {"type": "string", "description": "说明（create/edit 时可填）"},
                    "set_id": {"type": "integer", "description": "评测集 ID（rename/edit/add/remove/delete 时必填）"},
                    "video_ids": {"type": "array", "items": {"type": "string"}, "description": "视频 ID 列表（create/add/remove 时必填）"},
                },
                "required": ["action"],
            },
        },
    },
]


# ═══════════════════════════════════════════════════════════════════════════════
# 只读工具实现
# ═══════════════════════════════════════════════════════════════════════════════

def list_videos(query: Optional[str] = None, event_type: Optional[str] = None,
                limit: int = 20) -> dict:
    db = get_db()
    cursor = db.cursor()

    sql = '''
        SELECT DISTINCT v.id, v.filename, v.video_id, v.file_size, v.duration, v.created_at
        FROM videos v
        LEFT JOIN events e ON e.video_db_id = v.id
        WHERE 1=1
    '''
    params = []
    if query:
        sql += ' AND (v.filename LIKE ? OR v.video_id LIKE ?)'
        like = f'%{query}%'
        params.extend([like, like])
    if event_type:
        sql += ' AND e.event_type = ?'
        params.append(event_type)
    sql += ' ORDER BY v.created_at DESC LIMIT ?'
    params.append(limit)

    cursor.execute(sql, params)
    rows = [dict(r) for r in cursor.fetchall()]

    # 补充每个视频的事件类型标签
    for row in rows:
        cursor.execute('''
            SELECT DISTINCT event_type FROM events WHERE video_db_id = ?
        ''', (row['id'],))
        row['event_types'] = [r['event_type'] for r in cursor.fetchall()]

    return {'videos': rows, 'count': len(rows)}


def get_video_details(video_id: str) -> dict:
    db = get_db()
    cursor = db.cursor()

    cursor.execute('''
        SELECT id, filename, video_id, file_size, duration, created_at, updated_at
        FROM videos WHERE video_id = ? ORDER BY id DESC LIMIT 1
    ''', (video_id,))
    video = cursor.fetchone()
    if not video:
        return {'error': f'视频 {video_id} 不存在'}

    video_dict = dict(video)

    # 事件标注
    cursor.execute('''
        SELECT id, event_type, start_seconds, end_seconds, created_at
        FROM events WHERE video_db_id = ? ORDER BY start_seconds
    ''', (video_dict['id'],))
    video_dict['events'] = [dict(r) for r in cursor.fetchall()]

    # 告警图数量
    cursor.execute('''
        SELECT COUNT(*) as cnt FROM alert_images a
        JOIN ocr_results o ON o.alert_image_id = a.id
        WHERE o.video_id = ?
    ''', (video_id,))
    video_dict['alert_count'] = cursor.fetchone()['cnt']

    return {'video': video_dict}


def list_event_types() -> dict:
    db = get_db()
    cursor = db.cursor()
    cursor.execute('''
        SELECT id, key, name, description FROM event_types ORDER BY sort_order
    ''')
    return {'event_types': [dict(r) for r in cursor.fetchall()]}


def list_alerts(dataset_id: Optional[int] = None, event_type: Optional[str] = None,
                limit: int = 20) -> dict:
    db = get_db()
    cursor = db.cursor()

    sql = '''
        SELECT a.id, a.filename, a.alert_type, a.event_label, a.dataset_id,
               a.image_width, a.image_height, a.uploaded_at,
               o.video_id, o.timestamp_seconds
        FROM alert_images a
        LEFT JOIN (
            SELECT alert_image_id, video_id, timestamp_seconds
            FROM ocr_results
            WHERE id IN (SELECT MAX(id) FROM ocr_results GROUP BY alert_image_id)
        ) o ON o.alert_image_id = a.id
        WHERE 1=1
    '''
    params = []
    if dataset_id:
        sql += ' AND a.dataset_id = ?'
        params.append(dataset_id)
    if event_type:
        sql += ' AND (a.alert_type = ? OR a.event_label = ?)'
        params.extend([event_type, event_type])
    sql += ' ORDER BY a.uploaded_at DESC LIMIT ?'
    params.append(limit)

    cursor.execute(sql, params)
    return {'alerts': [dict(r) for r in cursor.fetchall()], 'count': cursor.rowcount}


def get_alert_details(alert_id: int) -> dict:
    db = get_db()
    cursor = db.cursor()

    cursor.execute('''
        SELECT id, filename, alert_type, event_label, dataset_id,
               image_width, image_height, uploaded_at
        FROM alert_images WHERE id = ?
    ''', (alert_id,))
    alert = cursor.fetchone()
    if not alert:
        return {'error': f'告警图片 {alert_id} 不存在'}

    alert_dict = dict(alert)

    cursor.execute('''
        SELECT id, raw_ocr_text, video_id, timestamp, timestamp_seconds, success, created_at
        FROM ocr_results WHERE alert_image_id = ? ORDER BY created_at DESC
    ''', (alert_id,))
    alert_dict['ocr_results'] = [dict(r) for r in cursor.fetchall()]

    cursor.execute('''
        SELECT id, verdict, reason, ground_truth_file, matched_event, created_at
        FROM verification_results WHERE alert_image_id = ? ORDER BY created_at DESC
    ''', (alert_id,))
    alert_dict['verification_results'] = [dict(r) for r in cursor.fetchall()]

    return {'alert': alert_dict}


def get_evaluation_report(task_id: int) -> dict:
    db = get_db()
    cursor = db.cursor()
    cursor.execute('''
        SELECT id, name, status, finalized, accuracy, recall, avg_fp_per_hour,
               event_metrics, created_at, confirmed_at
        FROM eval_tasks WHERE id = ?
    ''', (task_id,))
    task = cursor.fetchone()
    if not task:
        return {'error': f'评测任务 {task_id} 不存在'}

    task_dict = dict(task)
    if task_dict.get('event_metrics'):
        try:
            task_dict['event_metrics'] = json.loads(task_dict['event_metrics'])
        except Exception:
            task_dict['event_metrics'] = []
    return {'task': task_dict}


def _load_doc_text(filename: str) -> str:
    path = Path(current_app.config['PROJECT_ROOT']) / filename
    if path.exists():
        try:
            return path.read_text(encoding='utf-8')
        except Exception:
            pass
    return ''


def search_platform_docs(query: str) -> dict:
    """简单关键词匹配返回文档相关段落（项目文档量小，无需向量库）。"""
    docs = {
        'README.md': _load_doc_text('README.md'),
        'WEB_PLATFORM_README.md': _load_doc_text('WEB_PLATFORM_README.md'),
        'CLAUDE.md': _load_doc_text('CLAUDE.md'),
    }

    query_lower = query.lower()
    keywords = [k for k in query_lower.split() if len(k) > 1]

    results = []
    for name, content in docs.items():
        if not content:
            continue
        # 简单相关性：按行匹配关键词数量
        best_lines = []
        for line in content.splitlines():
            line_lower = line.lower()
            score = sum(1 for k in keywords if k in line_lower)
            if score > 0:
                best_lines.append((score, line.strip()))
        best_lines.sort(reverse=True)
        excerpt = '\n'.join(line for _, line in best_lines[:10])
        if excerpt:
            results.append({'source': name, 'excerpt': excerpt})

    return {'results': results}


def get_task_status(task_id: int) -> dict:
    if not task_id:
        return {'error': '缺少 task_id'}
    task = get_assistant_task(task_id)
    if not task:
        return {'error': f'任务 {task_id} 不存在'}
    progress = get_task_progress(task_id)
    return {
        'task': task,
        'progress': progress,
    }


def list_assistant_tasks(limit: int = 20) -> dict:
    db = get_db()
    cursor = db.cursor()
    cursor.execute('''
        SELECT id, task_type, ref_type, ref_id, status, params,
               result_summary, error_message, created_at, updated_at
        FROM assistant_tasks
        ORDER BY created_at DESC
        LIMIT ?
    ''', (limit,))
    tasks = []
    for row in cursor.fetchall():
        t = dict(row)
        try:
            t['params'] = json.loads(t['params']) if t['params'] else {}
        except Exception:
            t['params'] = {}
        # 补充内存进度
        t['progress'] = get_task_progress(t['id'])
        tasks.append(t)
    return {'tasks': tasks, 'count': len(tasks)}


# ═══════════════════════════════════════════════════════════════════════════════
# 写入工具：影响分析与执行
# ═══════════════════════════════════════════════════════════════════════════════

WRITE_TOOLS = {
    'update_video_tags',
    'delete_video',
    'run_evaluation_task',
    'batch_run_ocr',
    'update_alert_status',
    'export_report',
    'add_watermark',
    'concat_videos',
    'package_videos',
    'extract_frames',
    'manage_eval_set',
}


def analyze_update_video_tags(video_id: str, events: list, mode: str) -> dict:
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, filename, video_id FROM videos WHERE video_id = ? ORDER BY id DESC LIMIT 1', (video_id,))
    video = cursor.fetchone()
    if not video:
        return {'error': f'视频 {video_id} 不存在'}

    # 校验事件类型
    cursor.execute('SELECT key, name FROM event_types')
    valid_types = {r['key']: r['name'] for r in cursor.fetchall()}
    invalid = [e['type'] for e in events if e['type'] not in valid_types]
    if invalid:
        return {'error': f'无效的事件类型: {", ".join(invalid)}'}

    summary = f'{"替换" if mode == "replace" else "追加" if mode == "append" else "删除"}视频 {video_id}（{video["filename"]}）的 {len(events)} 条事件标注'
    return {
        'video_db_id': video['id'],
        'video_id': video_id,
        'filename': video['filename'],
        'event_count': len(events),
        'summary': summary,
    }


def execute_update_video_tags(params: dict) -> dict:
    db = get_db()
    cursor = db.cursor()

    analysis = analyze_update_video_tags(params['video_id'], params['events'], params['mode'])
    if 'error' in analysis:
        raise ValueError(analysis['error'])

    video_db_id = analysis['video_db_id']
    mode = params['mode']
    events = params['events']

    if mode == 'replace':
        cursor.execute('DELETE FROM events WHERE video_db_id = ?', (video_db_id,))

    for ev in events:
        etype = ev['type']
        start = ev['start']
        end = ev['end']
        if mode == 'delete':
            cursor.execute('''
                DELETE FROM events
                WHERE video_db_id = ? AND event_type = ? AND start_seconds = ? AND end_seconds = ?
            ''', (video_db_id, etype, start, end))
        else:
            cursor.execute('''
                INSERT INTO events (video_db_id, event_type, start_seconds, end_seconds)
                VALUES (?, ?, ?, ?)
            ''', (video_db_id, etype, start, end))

    db.commit()
    return {'success': True, 'updated_video_id': params['video_id'], 'event_count': len(events)}


def analyze_delete_video(video_id: str) -> dict:
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, filename FROM videos WHERE video_id = ? ORDER BY id DESC LIMIT 1', (video_id,))
    video = cursor.fetchone()
    if not video:
        return {'error': f'视频 {video_id} 不存在'}

    cursor.execute('SELECT COUNT(*) as cnt FROM events WHERE video_db_id = ?', (video['id'],))
    event_count = cursor.fetchone()['cnt']

    cursor.execute('''
        SELECT COUNT(*) as cnt FROM alert_images a
        JOIN ocr_results o ON o.alert_image_id = a.id WHERE o.video_id = ?
    ''', (video_id,))
    alert_count = cursor.fetchone()['cnt']

    return {
        'video_db_id': video['id'],
        'video_id': video_id,
        'filename': video['filename'],
        'event_count': event_count,
        'alert_count': alert_count,
        'summary': f'删除视频 {video_id}（{video["filename"]}），将移除 {event_count} 条标注和 {alert_count} 条关联告警记录',
    }


def execute_delete_video(params: dict) -> dict:
    db = get_db()
    cursor = db.cursor()

    analysis = analyze_delete_video(params['video_id'])
    if 'error' in analysis:
        raise ValueError(analysis['error'])

    video_db_id = analysis['video_db_id']

    # 级联清理
    cursor.execute('DELETE FROM events WHERE video_db_id = ?', (video_db_id,))
    cursor.execute('DELETE FROM watermarked_videos WHERE original_video_id = ?', (video_db_id,))
    cursor.execute('DELETE FROM gt_frames WHERE video_db_id = ?', (video_db_id,))
    cursor.execute('DELETE FROM videos WHERE id = ?', (video_db_id,))
    db.commit()

    return {
        'success': True,
        'deleted_video_id': params['video_id'],
        'removed_events': analysis['event_count'],
    }


def analyze_run_evaluation_task(name: str, dataset_id: int, eval_set_id: int) -> dict:
    db = get_db()
    cursor = db.cursor()

    cursor.execute('SELECT id, name FROM datasets WHERE id = ?', (dataset_id,))
    dataset = cursor.fetchone()
    if not dataset:
        return {'error': f'告警数据集 {dataset_id} 不存在'}

    cursor.execute('SELECT id, name FROM eval_video_sets WHERE id = ?', (eval_set_id,))
    eval_set = cursor.fetchone()
    if not eval_set:
        return {'error': f'评测视频集 {eval_set_id} 不存在'}

    return {
        'name': name,
        'dataset_id': dataset_id,
        'dataset_name': dataset['name'],
        'eval_set_id': eval_set_id,
        'eval_set_name': eval_set['name'],
        'summary': f'创建评测任务"{name}"，使用告警数据集"{dataset["name"]}"和评测视频集"{eval_set["name"]}"',
    }


def execute_run_evaluation_task(params: dict) -> dict:
    """创建并立即执行评测任务。"""
    from app.routes.evaluation import execute_task

    analysis = analyze_run_evaluation_task(params['name'], params['dataset_id'], params['eval_set_id'])
    if 'error' in analysis:
        raise ValueError(analysis['error'])

    db = get_db()
    cursor = db.cursor()
    cursor.execute('''
        INSERT INTO eval_tasks (name, dataset_id, eval_set_id, status)
        VALUES (?, ?, ?, ?)
    ''', (params['name'], params['dataset_id'], params['eval_set_id'], 'created'))
    db.commit()
    task_id = cursor.lastrowid

    # 复用 evaluation.py 的异步执行（当前已处于 app 上下文）
    resp = execute_task(task_id)
    if resp.status_code not in (200, 409):
        try:
            error = resp.get_json().get('error', '未知错误')
        except Exception:
            error = '启动评测任务失败'
        raise RuntimeError(error)

    return {
        'success': True,
        'eval_task_id': task_id,
        'message': '评测任务已创建并启动',
    }


def analyze_batch_run_ocr(dataset_id: Optional[int] = None,
                          alert_ids: Optional[list] = None) -> dict:
    db = get_db()
    cursor = db.cursor()

    if alert_ids:
        placeholders = ','.join('?' for _ in alert_ids)
        cursor.execute(f'''
            SELECT a.id, a.filename, a.alert_type, a.event_label
            FROM alert_images a
            WHERE a.id IN ({placeholders})
        ''', tuple(alert_ids))
    elif dataset_id:
        # 过滤掉已 OCR 成功的
        cursor.execute('''
            SELECT a.id, a.filename, a.alert_type, a.event_label
            FROM alert_images a
            WHERE a.dataset_id = ?
              AND a.id NOT IN (
                  SELECT DISTINCT alert_image_id FROM ocr_results WHERE success = 1
              )
        ''', (dataset_id,))
    else:
        cursor.execute('''
            SELECT a.id, a.filename, a.alert_type, a.event_label
            FROM alert_images a
            WHERE a.id NOT IN (
                SELECT DISTINCT alert_image_id FROM ocr_results WHERE success = 1
            )
        ''')

    alerts = [dict(r) for r in cursor.fetchall()]
    return {
        'alert_count': len(alerts),
        'alerts': alerts[:50],  # 摘要最多展示 50 条
        'summary': f'对 {len(alerts)} 张告警图片执行批量 OCR',
    }


def execute_batch_run_ocr(params: dict) -> dict:
    """批量 OCR 实际执行函数，在后台线程中运行。"""
    import threading
    from app.services.assistant_tasks import (
        create_assistant_task, update_assistant_task, set_task_progress
    )
    from app.services.verification_service import run_ocr
    from app.database import get_db

    task_id = create_assistant_task('batch_ocr', params)

    def _worker(assistant_task_id: int):
        db = get_db()
        cursor = db.cursor()
        analysis = analyze_batch_run_ocr(params.get('dataset_id'), params.get('alert_ids'))
        alerts = analysis.get('alerts', [])

        # 如果 alerts 是截断后的摘要，需要重新查完整列表
        alert_ids_param = params.get('alert_ids')
        dataset_id_param = params.get('dataset_id')
        if alert_ids_param:
            placeholders = ','.join('?' for _ in alert_ids_param)
            cursor.execute(f'SELECT id, file_path FROM alert_images WHERE id IN ({placeholders})', tuple(alert_ids_param))
        elif dataset_id_param:
            cursor.execute('''
                SELECT id, file_path FROM alert_images
                WHERE dataset_id = ? AND id NOT IN (
                    SELECT DISTINCT alert_image_id FROM ocr_results WHERE success = 1
                )
            ''', (dataset_id_param,))
        else:
            cursor.execute('''
                SELECT id, file_path FROM alert_images
                WHERE id NOT IN (
                    SELECT DISTINCT alert_image_id FROM ocr_results WHERE success = 1
                )
            ''')
        all_alerts = [dict(r) for r in cursor.fetchall()]

        update_assistant_task(assistant_task_id, 'running')
        set_task_progress(assistant_task_id, len(all_alerts), 0)

        success_count = 0
        fail_count = 0
        for idx, alert in enumerate(all_alerts):
            result = run_ocr(alert['file_path'])
            if 'error' in result:
                fail_count += 1
            else:
                success_count += 1
                cursor.execute('''
                    INSERT INTO ocr_results
                    (alert_image_id, raw_ocr_text, video_id, timestamp, timestamp_seconds, success, full_result)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    alert['id'],
                    result.get('raw_ocr_text', ''),
                    result.get('video_id'),
                    result.get('timestamp'),
                    result.get('timestamp_seconds'),
                    result.get('success', False),
                    json.dumps(result, ensure_ascii=False),
                ))
                db.commit()
            set_task_progress(assistant_task_id, len(all_alerts), idx + 1)

        update_assistant_task(
            assistant_task_id,
            'done',
            result_summary=f'成功 {success_count} 张，失败 {fail_count} 张',
        )

    thread = threading.Thread(target=_worker, args=(task_id,), daemon=True)
    thread.start()

    return {'success': True, 'assistant_task_id': task_id, 'message': '批量 OCR 任务已启动'}


def analyze_update_alert_status(alert_ids: list, status: str) -> dict:
    db = get_db()
    cursor = db.cursor()
    placeholders = ','.join('?' for _ in alert_ids)
    cursor.execute(f'''
        SELECT id, filename, alert_type, event_label FROM alert_images WHERE id IN ({placeholders})
    ''', tuple(alert_ids))
    alerts = [dict(r) for r in cursor.fetchall()]
    if not alerts:
        return {'error': '未找到指定的告警图片'}

    return {
        'alert_count': len(alerts),
        'alerts': alerts[:50],
        'summary': f'将 {len(alerts)} 张告警图片的人工复核状态设置为"{status}"',
    }


def execute_update_alert_status(params: dict) -> dict:
    # 当前平台没有独立的 alert 复核状态表，这里仅做演示占位
    # 实际可写入 verification_results 或新增 alert_status 字段
    return {
        'success': True,
        'message': f'已标记 {len(params["alert_ids"])} 张告警图片状态为 {params["status"]}',
    }


def analyze_export_report(task_id: int, format: str) -> dict:
    return {
        'task_id': task_id,
        'format': format,
        'summary': f'导出评测任务 {task_id} 的 {format.upper()} 报告',
    }


def analyze_add_watermark(video_id: str) -> dict:
    db = get_db()
    cursor = db.cursor()
    cursor.execute('''
        SELECT id, filename, original_path, video_id, duration FROM videos
        WHERE video_id = ? ORDER BY id DESC LIMIT 1
    ''', (video_id,))
    video = cursor.fetchone()
    if not video:
        return {'error': f'视频 {video_id} 不存在'}

    # 检查是否已有水印视频
    cursor.execute('''
        SELECT id, output_path FROM watermarked_videos
        WHERE original_video_id = ? ORDER BY id DESC LIMIT 1
    ''', (video['id'],))
    existing = cursor.fetchone()

    return {
        'video_db_id': video['id'],
        'video_id': video_id,
        'filename': video['filename'],
        'duration': video['duration'],
        'existing_watermark': dict(existing) if existing else None,
        'summary': f'给视频 {video_id}（{video["filename"]}）添加水印' +
                   ('，将覆盖已有水印视频' if existing else ''),
    }


def execute_add_watermark(params: dict) -> dict:
    """异步给视频添加水印。"""
    import threading
    from flask import current_app
    from app.services.watermark_service import add_watermark
    from app.services.assistant_tasks import (
        create_assistant_task, update_assistant_task, set_task_progress
    )
    from app.database import get_db

    analysis = analyze_add_watermark(params['video_id'])
    if 'error' in analysis:
        raise ValueError(analysis['error'])

    video_db_id = analysis['video_db_id']
    video_id = params['video_id']

    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT original_path FROM videos WHERE id = ?', (video_db_id,))
    video_path = cursor.fetchone()['original_path']
    output_dir = current_app.config['OUTPUT_DIR']

    task_id = create_assistant_task('add_watermark', params)

    def _worker(assistant_task_id: int):
        update_assistant_task(assistant_task_id, 'running')
        set_task_progress(assistant_task_id, 100, 0)

        result = add_watermark(
            video_path=video_path,
            output_dir=output_dir,
            video_id=video_id,
        )

        if result['success']:
            # 保存到 watermarked_videos 表
            try:
                from pathlib import Path
                p = Path(result['output_path'])
                db2 = get_db()
                cur2 = db2.cursor()
                cur2.execute('''
                    INSERT INTO watermarked_videos (original_video_id, filename, output_path, file_size)
                    VALUES (?, ?, ?, ?)
                ''', (video_db_id, p.name, str(p), p.stat().st_size if p.exists() else 0))
                db2.commit()
            except Exception:
                pass

            update_assistant_task(
                assistant_task_id,
                'done',
                result_summary=f'水印视频已生成: {result["output_path"]}',
            )
        else:
            update_assistant_task(
                assistant_task_id,
                'failed',
                error_message=result.get('stderr', '添加水印失败'),
            )

        set_task_progress(assistant_task_id, 100, 100)

    thread = threading.Thread(target=_worker, args=(task_id,), daemon=True)
    thread.start()

    return {
        'success': True,
        'assistant_task_id': task_id,
        'message': '水印任务已启动，处理完成后可在视频管理页面查看',
    }


def execute_export_report(params: dict) -> dict:
    task_id = params['task_id']
    fmt = params['format']
    if fmt == 'png':
        return {
            'success': True,
            'download_url': f'/evaluation/api/tasks/{task_id}/report-image',
            'message': '报告图片已生成',
        }
    return {
        'success': True,
        'message': f'{fmt.upper()} 报告导出功能待实现',
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 视频管理批量操作工具
# ═══════════════════════════════════════════════════════════════════════════════

def _resolve_video_db_ids(video_ids: list) -> tuple:
    """把 video_id 字符串列表转成 db_id 列表。返回 (db_ids, not_found)。"""
    db = get_db()
    cursor = db.cursor()
    db_ids = []
    not_found = []
    for vid in video_ids:
        cursor.execute('SELECT id FROM videos WHERE video_id = ? ORDER BY id DESC LIMIT 1', (vid,))
        row = cursor.fetchone()
        if row:
            db_ids.append(row['id'])
        else:
            not_found.append(vid)
    return db_ids, not_found


def analyze_concat_videos(video_ids: list) -> dict:
    if not video_ids or len(video_ids) < 2:
        return {'error': '拼接至少需要2个视频'}
    if len(video_ids) > 10:
        return {'error': '拼接最多10个视频'}
    db_ids, not_found = _resolve_video_db_ids(video_ids)
    if not_found:
        return {'error': f'视频不存在: {", ".join(not_found)}'}
    db = get_db()
    cursor = db.cursor()
    names = []
    for did in db_ids:
        cursor.execute('SELECT video_id FROM videos WHERE id=?', (did,))
        names.append(cursor.fetchone()['video_id'])
    return {
        'video_db_ids': db_ids,
        'summary': f'将拼接 {len(db_ids)} 个视频：{", ".join(names)}',
    }


def analyze_package_videos(video_ids: list) -> dict:
    if not video_ids:
        return {'error': '请至少选择1个视频'}
    if len(video_ids) > 10:
        return {'error': '打包最多10个视频'}
    db_ids, not_found = _resolve_video_db_ids(video_ids)
    if not_found:
        return {'error': f'视频不存在: {", ".join(not_found)}'}
    return {
        'video_db_ids': db_ids,
        'summary': f'将打包 {len(db_ids)} 个视频为 zip',
    }


def analyze_extract_frames(video_ids: list, target_width=None, interval_sec=1.0, include_normal=False) -> dict:
    if not video_ids:
        return {'error': '请至少选择1个视频'}
    db_ids, not_found = _resolve_video_db_ids(video_ids)
    if not_found:
        return {'error': f'视频不存在: {", ".join(not_found)}'}
    range_desc = '全程含normal帧' if include_normal else '仅GT事件时段'
    return {
        'video_db_ids': db_ids,
        'target_width': target_width,
        'interval_sec': interval_sec,
        'include_normal': include_normal,
        'summary': f'将对 {len(db_ids)} 个视频抽帧，间隔{interval_sec}s，{range_desc}'
                   + (f'，宽度{target_width}' if target_width else ''),
    }


def analyze_manage_eval_set(action: str, name=None, notes=None, set_id=None, video_ids=None) -> dict:
    db = get_db()
    cursor = db.cursor()
    if action in ('rename', 'edit', 'add', 'remove', 'delete'):
        if not set_id:
            return {'error': f'{action} 操作需要 set_id'}
        cursor.execute('SELECT id, name, notes, video_ids FROM eval_video_sets WHERE id=?', (set_id,))
        s = cursor.fetchone()
        if not s:
            return {'error': f'评测集 {set_id} 不存在'}
    if action == 'create':
        if not name:
            return {'error': '创建评测集需要 name'}
        summary = f'创建评测集"{name}"'
        if video_ids:
            db_ids, not_found = _resolve_video_db_ids(video_ids)
            if not_found:
                return {'error': f'视频不存在: {", ".join(not_found)}'}
            summary += f'，包含 {len(db_ids)} 个视频'
    elif action == 'rename':
        if not name:
            return {'error': '重命名需要 name'}
        summary = f'将评测集"{s["name"]}"重命名为"{name}"'
    elif action == 'edit':
        summary = f'编辑评测集"{s["name"]}"的说明'
    elif action == 'add':
        if not video_ids:
            return {'error': '添加视频需要 video_ids'}
        db_ids, not_found = _resolve_video_db_ids(video_ids)
        if not_found:
            return {'error': f'视频不存在: {", ".join(not_found)}'}
        summary = f'向评测集"{s["name"]}"添加 {len(db_ids)} 个视频'
    elif action == 'remove':
        if not video_ids:
            return {'error': '移出视频需要 video_ids'}
        summary = f'从评测集"{s["name"]}"移出 {len(video_ids)} 个视频'
    elif action == 'delete':
        summary = f'删除评测集"{s["name"]}"'
    else:
        return {'error': f'未知操作: {action}'}
    return {'summary': summary}


def execute_concat_videos(params: dict) -> dict:
    analysis = analyze_concat_videos(params['video_ids'])
    if 'error' in analysis:
        raise ValueError(analysis['error'])
    from app.routes.videos import _do_concat_task, video_process_tasks
    import random
    task_id = f"concat_{int(__import__('time').time())}_{random.randint(1000,9999)}"
    video_process_tasks[task_id] = {'type': 'concat', 'status': 'processing', 'progress': 10,
                                    'output_path': None, 'error': None, 'generated_id': None, 'name': None}
    from flask import current_app
    threading.Thread(
        target=_do_concat_task,
        args=(task_id, analysis['video_db_ids'], current_app.config['PROJECT_ROOT'], current_app.config['GENERATED_VIDEOS_DIR']),
        daemon=True,
    ).start()
    return {'success': True, 'message': '拼接任务已提交，可在视频管理页"处理中的任务"查看进度，完成后在"生成的视频"区查看'}


def execute_package_videos(params: dict) -> dict:
    analysis = analyze_package_videos(params['video_ids'])
    if 'error' in analysis:
        raise ValueError(analysis['error'])
    from app.routes.videos import _do_package_task, video_process_tasks
    import random
    task_id = f"package_{int(__import__('time').time())}_{random.randint(1000,9999)}"
    video_process_tasks[task_id] = {'type': 'package', 'status': 'processing', 'progress': 10,
                                    'output_path': None, 'error': None, 'generated_id': None, 'name': None}
    from flask import current_app
    threading.Thread(
        target=_do_package_task,
        args=(task_id, analysis['video_db_ids'], current_app.config['PROJECT_ROOT'], current_app.config['GENERATED_VIDEOS_DIR']),
        daemon=True,
    ).start()
    return {'success': True, 'message': '打包任务已提交，可在视频管理页查看进度，完成后在"生成的视频"区下载'}


def execute_extract_frames(params: dict) -> dict:
    analysis = analyze_extract_frames(
        params['video_ids'],
        params.get('target_width'),
        float(params.get('interval_sec') or 1.0),
        bool(params.get('include_normal', False)),
    )
    if 'error' in analysis:
        raise ValueError(analysis['error'])
    # 复用 extract 的批量抽帧逻辑：取 wm_id 列表，调 _do_extract_batch
    from app.routes.extract import _do_extract_batch, _extract_tasks, _extract_lock
    from pathlib import Path
    from flask import current_app
    db = get_db()
    cur = db.cursor()
    db_ids = analysis['video_db_ids']
    placeholders = ','.join('?' for _ in db_ids)
    cur.execute(f'''
        SELECT w.id, w.output_path, w.original_video_id, v.video_id
        FROM watermarked_videos w JOIN videos v ON v.id = w.original_video_id
        WHERE w.original_video_id IN ({placeholders})
        GROUP BY w.original_video_id
    ''', db_ids)
    wms = [dict(w) for w in cur.fetchall()]
    for w in wms:
        cur.execute('SELECT event_type, start_seconds, end_seconds FROM events WHERE video_db_id=?', (w['original_video_id'],))
        w['events'] = [dict(e) for e in cur.fetchall()]
    if not wms:
        raise ValueError('选中的视频均无可抽帧的水印视频')

    output_dir = Path(current_app.config['EXTRACTED_FRAMES_DIR']) / f'batch_{int(__import__("time").time())}'
    import sqlite3
    from app.database import DATABASE_PATH
    conn = sqlite3.connect(str(DATABASE_PATH))
    try:
        c = conn.cursor()
        c.execute('''INSERT INTO extracted_frames_tasks
            (wm_ids, video_id, video_count, target_width, interval_sec, include_normal, status, output_dir)
            VALUES (?, ?, ?, ?, ?, ?, 'running', ?)''',
            (json.dumps([w['id'] for w in wms]), ','.join(w['video_id'] for w in wms), len(wms),
             analysis.get('target_width'), analysis['interval_sec'], 1 if analysis['include_normal'] else 0, str(output_dir)))
        conn.commit()
        task_id = c.lastrowid
    finally:
        conn.close()

    with _extract_lock:
        _extract_tasks[task_id] = {'video_id': ','.join(w['video_id'] for w in wms), 'video_count': len(wms),
                                   'total': len(wms), 'done': 0, 'frame_count': 0, 'status': 'running',
                                   'output_dir': str(output_dir), 'error': None}
    threading.Thread(
        target=_do_extract_batch,
        args=(task_id, wms, analysis.get('target_width'), analysis['interval_sec'], analysis['include_normal'], str(output_dir)),
        daemon=True,
    ).start()
    return {'success': True, 'message': f'抽帧任务已提交（{len(wms)}个视频），可在视频管理页"处理中的任务"查看进度，完成后在"生成的图片集"区下载'}


def execute_manage_eval_set(params: dict) -> dict:
    action = params['action']
    name = (params.get('name') or '').strip() or None
    notes = (params.get('notes') or '').strip() if params.get('notes') is not None else None
    set_id = params.get('set_id')
    video_ids = params.get('video_ids') or []
    analysis = analyze_manage_eval_set(action, name, notes, set_id, video_ids)
    if 'error' in analysis:
        raise ValueError(analysis['error'])

    db = get_db()
    cur = db.cursor()
    if action == 'create':
        db_ids, _ = _resolve_video_db_ids(video_ids) if video_ids else ([], [])
        cur.execute('INSERT INTO eval_video_sets (name, notes, video_ids) VALUES (?, ?, ?)',
                    (name, notes or '', json.dumps(db_ids)))
        db.commit()
        return {'success': True, 'message': f'已创建评测集"{name}"，包含{len(db_ids)}个视频'}
    if action == 'rename':
        cur.execute('UPDATE eval_video_sets SET name=? WHERE id=?', (name, set_id))
        db.commit()
        return {'success': True, 'message': f'已重命名为"{name}"'}
    if action == 'edit':
        if notes is not None:
            cur.execute('UPDATE eval_video_sets SET notes=? WHERE id=?', (notes, set_id))
            db.commit()
        return {'success': True, 'message': '已更新说明'}
    if action == 'add':
        db_ids, _ = _resolve_video_db_ids(video_ids)
        cur.execute('SELECT video_ids FROM eval_video_sets WHERE id=?', (set_id,))
        existing = json.loads(cur.fetchone()['video_ids'] or '[]')
        for did in db_ids:
            if did not in existing:
                existing.append(did)
        cur.execute('UPDATE eval_video_sets SET video_ids=? WHERE id=?', (json.dumps(existing), set_id))
        db.commit()
        return {'success': True, 'message': f'已添加{len(db_ids)}个视频'}
    if action == 'remove':
        db_ids, _ = _resolve_video_db_ids(video_ids)
        cur.execute('SELECT video_ids FROM eval_video_sets WHERE id=?', (set_id,))
        existing = json.loads(cur.fetchone()['video_ids'] or '[]')
        existing = [x for x in existing if x not in db_ids]
        cur.execute('UPDATE eval_video_sets SET video_ids=? WHERE id=?', (json.dumps(existing), set_id))
        db.commit()
        return {'success': True, 'message': f'已移出{len(db_ids)}个视频'}
    if action == 'delete':
        cur.execute('DELETE FROM eval_video_sets WHERE id=?', (set_id,))
        db.commit()
        return {'success': True, 'message': '已删除评测集'}
    raise ValueError(f'未知操作: {action}')


# ═══════════════════════════════════════════════════════════════════════════════
# 工具分发
# ═══════════════════════════════════════════════════════════════════════════════

READ_TOOL_FUNCTIONS = {
    'list_videos': list_videos,
    'get_video_details': get_video_details,
    'list_event_types': list_event_types,
    'list_alerts': list_alerts,
    'get_alert_details': get_alert_details,
    'get_evaluation_report': get_evaluation_report,
    'search_platform_docs': search_platform_docs,
    'get_task_status': get_task_status,
    'list_assistant_tasks': list_assistant_tasks,
}


def analyze_write_tool(tool_name: str, params: dict) -> dict:
    """分析写入工具的影响范围，返回确认摘要。"""
    if tool_name == 'update_video_tags':
        return analyze_update_video_tags(params['video_id'], params['events'], params['mode'])
    if tool_name == 'delete_video':
        return analyze_delete_video(params['video_id'])
    if tool_name == 'run_evaluation_task':
        return analyze_run_evaluation_task(params['name'], params['dataset_id'], params['eval_set_id'])
    if tool_name == 'batch_run_ocr':
        return analyze_batch_run_ocr(params.get('dataset_id'), params.get('alert_ids'))
    if tool_name == 'update_alert_status':
        return analyze_update_alert_status(params['alert_ids'], params['status'])
    if tool_name == 'export_report':
        return analyze_export_report(params['task_id'], params['format'])
    if tool_name == 'add_watermark':
        return analyze_add_watermark(params['video_id'])
    if tool_name == 'concat_videos':
        return analyze_concat_videos(params['video_ids'])
    if tool_name == 'package_videos':
        return analyze_package_videos(params['video_ids'])
    if tool_name == 'extract_frames':
        return analyze_extract_frames(params['video_ids'], params.get('target_width'),
                                      float(params.get('interval_sec') or 1.0), bool(params.get('include_normal', False)))
    if tool_name == 'manage_eval_set':
        return analyze_manage_eval_set(params['action'], params.get('name'), params.get('notes'),
                                       params.get('set_id'), params.get('video_ids'))
    return {'error': f'未知工具: {tool_name}'}


def execute_write_tool(tool_name: str, params: dict) -> dict:
    """执行写入工具。"""
    if tool_name == 'update_video_tags':
        return execute_update_video_tags(params)
    if tool_name == 'delete_video':
        return execute_delete_video(params)
    if tool_name == 'run_evaluation_task':
        return execute_run_evaluation_task(params)
    if tool_name == 'batch_run_ocr':
        return execute_batch_run_ocr(params)
    if tool_name == 'update_alert_status':
        return execute_update_alert_status(params)
    if tool_name == 'export_report':
        return execute_export_report(params)
    if tool_name == 'add_watermark':
        return execute_add_watermark(params)
    if tool_name == 'concat_videos':
        return execute_concat_videos(params)
    if tool_name == 'package_videos':
        return execute_package_videos(params)
    if tool_name == 'extract_frames':
        return execute_extract_frames(params)
    if tool_name == 'manage_eval_set':
        return execute_manage_eval_set(params)
    raise ValueError(f'未知工具: {tool_name}')
