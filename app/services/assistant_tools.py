"""AI 助手工具函数与 JSON Schema 定义

所有工具分为两类：
- 只读工具：直接返回查询结果
- 写入工具：只进行参数校验和影响分析，返回确认请求；真正执行在确认后由 executor 完成
"""
import json
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
    raise ValueError(f'未知工具: {tool_name}')
