"""AI 助手路由"""
import json
from flask import Blueprint, request, jsonify, render_template, session

from app.database import get_db
from app.services.assistant_service import chat, confirm, cancel, clear_history, _get_session_id, _get_history
from app.services.assistant_settings import (
    get_settings_for_display,
    update_assistant_settings,
    is_configured,
)
from app.services.assistant_tasks import get_assistant_task, get_task_progress

bp = Blueprint('assistant', __name__, url_prefix='/assistant')


@bp.route('/widget')
def assistant_widget():
    """返回聊天组件 HTML 片段（供 base.html include）。"""
    return render_template('assistant_widget.html')


@bp.route('/settings')
def settings_page():
    """AI 助手设置页。"""
    settings = get_settings_for_display()
    return render_template('assistant_settings.html', settings=settings)


@bp.route('/api/settings', methods=['GET'])
def api_get_settings():
    """获取当前设置（API Key 脱敏）。"""
    return jsonify({'success': True, 'settings': get_settings_for_display()})


@bp.route('/api/settings', methods=['POST'])
def api_update_settings():
    """更新设置。"""
    data = request.get_json() or {}
    settings = update_assistant_settings(data)
    return jsonify({'success': True, 'settings': get_settings_for_display()})


@bp.route('/api/chat', methods=['POST'])
def api_chat():
    """聊天接口。"""
    data = request.get_json() or {}
    user_message = (data.get('message') or '').strip()
    if not user_message:
        return jsonify({'error': '消息不能为空'}), 400

    if not is_configured():
        return jsonify({
            'type': 'error',
            'message': {
                'role': 'assistant',
                'content': 'AI 助手尚未配置。请先在 /assistant/settings 页面设置 OpenAI API Key。',
            },
            'error_code': 'NOT_CONFIGURED',
        })

    result = chat(user_message, _get_session_id())
    return jsonify(result)


@bp.route('/api/confirm', methods=['POST'])
def api_confirm():
    """确认执行待确认操作。"""
    data = request.get_json() or {}
    confirmation_id = data.get('confirmation_id')
    if not confirmation_id:
        return jsonify({'error': '缺少 confirmation_id'}), 400
    result = confirm(confirmation_id, _get_session_id())
    return jsonify(result)


@bp.route('/api/cancel', methods=['POST'])
def api_cancel():
    """取消待确认操作。"""
    data = request.get_json() or {}
    confirmation_id = data.get('confirmation_id')
    if not confirmation_id:
        return jsonify({'error': '缺少 confirmation_id'}), 400
    result = cancel(confirmation_id, _get_session_id())
    return jsonify(result)


@bp.route('/api/clear', methods=['POST'])
def api_clear():
    """清除当前会话的对话历史。"""
    clear_history()
    return jsonify({'success': True, 'message': '对话历史已清除'})


@bp.route('/api/history', methods=['GET'])
def api_history():
    """获取当前会话的对话历史（仅返回 user/assistant 可见文本消息）。

    不向前端返回 tool_calls：tool_calls 含工具函数名（如 add_watermark、start_stream），
    暴露给用户违反规则7；且前端 appendToolResult 会把中间工具步骤渲染成「查看 xx 原始结果」
    折叠块，既泄露内部名又是半成品 UI。故只返回 user/assistant 的文本内容，空内容（仅有
    tool_calls 无文本的 assistant 中间步骤）直接跳过。内部工具步骤仍留在库里供下一轮 LLM
    上下文（_get_history 返回完整带 tool_calls 的消息），仅显示层剥离。
    """
    messages = []
    for msg in _get_history():
        role = msg.get('role')
        if role not in ('user', 'assistant'):
            continue
        content = (msg.get('content') or '').strip()
        if not content:
            continue
        messages.append({'role': role, 'content': content})
    return jsonify({'messages': messages})


@bp.route('/api/tasks', methods=['GET'])
def api_list_tasks():
    """查询 assistant 任务列表。"""
    limit = request.args.get('limit', 20, type=int)
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
        tasks.append(t)
    return jsonify({'success': True, 'tasks': tasks})


@bp.route('/api/tasks/<int:task_id>', methods=['GET'])
def api_task_status(task_id):
    """查询 assistant 任务状态。"""
    task = get_assistant_task(task_id)
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    progress = get_task_progress(task_id)
    return jsonify({'success': True, 'task': task, 'progress': progress})
