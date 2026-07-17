"""AI 助手任务与确认管理"""
import json
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from app.database import get_db, DATABASE_PATH


# 内存中的任务进度（和 evaluation.py 的 _eval_progress 类似）
_task_progress = {}
_task_lock = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(value) -> datetime:
    """兼容 SQLite 返回的字符串或 datetime 对象。"""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    return _now()


def create_pending_confirmation(action: str, params: dict, summary: str, session_id: str,
                                ttl_seconds: int = 300) -> str:
    """创建一条待确认操作，返回确认令牌 ID。"""
    confirmation_id = secrets.token_urlsafe(24)
    expires_at = _now() + timedelta(seconds=ttl_seconds)
    db = get_db()
    cursor = db.cursor()
    cursor.execute('''
        INSERT INTO pending_confirmations
        (id, action, params, summary, session_id, status, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (
        confirmation_id,
        action,
        json.dumps(params, ensure_ascii=False),
        summary,
        session_id,
        'pending',
        expires_at,
    ))
    db.commit()
    return confirmation_id


def get_pending_confirmation(confirmation_id: str, session_id: str) -> Optional[dict]:
    """获取并校验待确认记录，不存在/过期/已处理则返回 None。"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('''
        SELECT id, action, params, summary, status, created_at, expires_at
        FROM pending_confirmations
        WHERE id = ? AND session_id = ? AND status = 'pending'
    ''', (confirmation_id, session_id))
    row = cursor.fetchone()
    if not row:
        return None
    if _now() > _parse_datetime(row['expires_at']):
        return None
    result = dict(row)
    try:
        result['params'] = json.loads(result['params'])
    except Exception:
        result['params'] = {}
    return result


def cancel_confirmation(confirmation_id: str, session_id: str) -> bool:
    """取消待确认操作。"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('''
        UPDATE pending_confirmations SET status = 'cancelled'
        WHERE id = ? AND session_id = ? AND status = 'pending'
    ''', (confirmation_id, session_id))
    db.commit()
    return cursor.rowcount > 0


def confirm_and_execute(confirmation_id: str, session_id: str,
                        executor: Callable[[str, dict], dict]) -> dict:
    """确认并执行待确认操作。executor 接收 (action, params) 返回执行结果。"""
    pending = get_pending_confirmation(confirmation_id, session_id)
    if not pending:
        return {'success': False, 'error': '操作已过期或不存在'}

    db = get_db()
    cursor = db.cursor()
    cursor.execute('''
        UPDATE pending_confirmations SET status = 'confirmed'
        WHERE id = ? AND session_id = ? AND status = 'pending'
    ''', (confirmation_id, session_id))
    db.commit()

    if cursor.rowcount == 0:
        return {'success': False, 'error': '操作已被处理'}

    try:
        result = executor(pending['action'], pending['params'])
        _log_action(session_id, pending['action'], pending['params'], result, 'success')
        return {'success': True, 'result': result}
    except Exception as e:
        _log_action(session_id, pending['action'], pending['params'], {'error': str(e)}, 'failed')
        return {'success': False, 'error': str(e)}


def _log_action(session_id: str, action: str, params: dict, result: dict, status: str):
    """记录审计日志。"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('''
        INSERT INTO assistant_audit_log (session_id, action, params, result, status)
        VALUES (?, ?, ?, ?, ?)
    ''', (
        session_id,
        action,
        json.dumps(params, ensure_ascii=False),
        json.dumps(result, ensure_ascii=False),
        status,
    ))
    db.commit()


def create_assistant_task(task_type: str, params: dict,
                          ref_type: Optional[str] = None,
                          ref_id: Optional[int] = None) -> int:
    """创建一条统一的 assistant 异步任务记录。"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('''
        INSERT INTO assistant_tasks (task_type, ref_type, ref_id, status, params)
        VALUES (?, ?, ?, ?, ?)
    ''', (
        task_type,
        ref_type,
        ref_id,
        'pending',
        json.dumps(params, ensure_ascii=False),
    ))
    db.commit()
    return cursor.lastrowid


def update_assistant_task(task_id: int, status: str,
                          result_summary: Optional[str] = None,
                          error_message: Optional[str] = None):
    """更新 assistant 任务状态。"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('''
        UPDATE assistant_tasks
        SET status = ?, result_summary = ?, error_message = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
    ''', (status, result_summary, error_message, task_id))
    db.commit()


def get_assistant_task(task_id: int) -> Optional[dict]:
    """获取 assistant 任务详情。"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('''
        SELECT id, task_type, ref_type, ref_id, status, params,
               result_summary, error_message, created_at, updated_at
        FROM assistant_tasks WHERE id = ?
    ''', (task_id,))
    row = cursor.fetchone()
    if not row:
        return None
    result = dict(row)
    try:
        result['params'] = json.loads(result['params'])
    except Exception:
        result['params'] = {}
    return result


def set_task_progress(task_id: int, total: int, done: int, running: bool = True):
    """设置内存中的任务进度。"""
    with _task_lock:
        _task_progress[task_id] = {
            'total': total,
            'done': done,
            'running': running,
        }


def get_task_progress(task_id: int) -> Optional[dict]:
    """获取内存中的任务进度。"""
    with _task_lock:
        return _task_progress.get(task_id)


def run_task_in_thread(task_id: int, target: Callable[[int], None]):
    """启动后台线程执行任务。"""
    import threading
    thread = threading.Thread(target=target, args=(task_id,), daemon=True)
    thread.start()
