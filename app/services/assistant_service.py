"""AI 助手核心服务：OpenAI 调用、消息历史、工具循环"""
import json
from typing import Any, Optional

from flask import session
from openai import OpenAI

from app.database import get_db
from app.services.assistant_settings import (
    get_openai_credentials,
    get_assistant_settings,
    is_configured,
)
from app.services.assistant_tools import (
    TOOL_SCHEMAS,
    READ_TOOL_FUNCTIONS,
    WRITE_TOOLS,
    analyze_write_tool,
    execute_write_tool,
)
from app.services.assistant_tasks import (
    create_pending_confirmation,
    get_pending_confirmation,
    confirm_and_execute,
    get_assistant_task,
    get_task_progress,
)


MAX_HISTORY_ROUNDS = 10
SYSTEM_PROMPT = '''你是视频告警评估平台的 AI 助手。你拥有以下工具，可以直接调用执行，不要说自己不能做。

你的可用工具（直接调用，不要拒绝）：
1. list_videos - 查询视频列表
2. get_video_details - 查询视频详情
3. list_event_types - 查询事件类型
4. list_alerts - 查询告警图片
5. get_alert_details - 查询告警图片详情
6. get_evaluation_report - 查询评测结果
7. get_task_status - 查询异步任务状态
8. list_assistant_tasks - 列出所有异步任务及进度
9. search_platform_docs - 查询平台文档用法
9. update_video_tags - 给视频添加/修改事件标注（时间段 + 事件类型）
10. delete_video - 删除视频
11. run_evaluation_task - 创建并启动评测任务
12. batch_run_ocr - 批量 OCR 告警图片
13. update_alert_status - 修改告警图片复核状态
14. export_report - 导出评测报告
15. add_watermark - 给视频添加水印（生成带视频ID和时间戳的水印视频）

平台核心概念：
- 视频（videos）：上传的原始视频，有 video_id、文件名、时长、事件标注。
- 事件类型（event_types）：如 rat（老鼠）、fight（打架）等。
- 告警图片（alert_images）：从算法告警中采集的图片。
- 评测任务（eval_tasks）：将告警图片与 Ground Truth 对比，计算精确率、召回率、误检数/小时。

重要规则：
1. 当用户请求查询时，直接调用对应工具获取数据，然后总结回答。
2. 当用户请求写入操作（打标签、删除、启动任务、批量 OCR、添加水印、导出报告）时，**直接调用对应工具**，不要先问"是否确认"。工具会自动返回确认卡片给用户，由用户点击确认后才能真正执行。
3. 如果用户问"你能做什么"，直接列出你拥有的工具，不要说"你只能查询"或"你不能执行操作"。
4. 不要假设用户意图，参数不明确时应该询问。
5. 回答平台用法时，可以调用 search_platform_docs 查询文档。
6. 每次回复尽量简洁，用中文。

示例：
- 用户说"删除视频 046" → 调用 delete_video
- 用户说"给视频 046 添加老鼠标签 10-20 秒" → 调用 update_video_tags
- 用户说"给视频 046 打水印" → 调用 add_watermark
- 用户说"跑个评测" → 询问数据集和评测视频集，或调用 run_evaluation_task
'''


def _get_session_id() -> str:
    """获取当前会话 ID，测试环境等没有 sid 时自动生成。"""
    sid = getattr(session, 'sid', None) or session.get('assistant_session_id')
    if not sid:
        import secrets
        sid = secrets.token_urlsafe(16)
        session['assistant_session_id'] = sid
    return sid


def _get_history() -> list:
    """获取当前会话的消息历史（优先从数据库读取）。"""
    sid = _get_session_id()
    db = get_db()
    cursor = db.cursor()
    cursor.execute('''
        SELECT role, content, tool_calls, tool_call_id FROM assistant_conversations
        WHERE session_id = ? ORDER BY id ASC LIMIT 100
    ''', (sid,))
    messages = []
    for row in cursor.fetchall():
        msg = {'role': row['role'], 'content': row['content'] or ''}
        if row['tool_calls']:
            try:
                msg['tool_calls'] = json.loads(row['tool_calls'])
            except Exception:
                pass
        # tool 消息必须带 tool_call_id，否则 API 报 tool id not found
        if row['tool_call_id']:
            msg['tool_call_id'] = row['tool_call_id']
        messages.append(msg)

    # 如果没有数据库记录，回退到 session（兼容旧会话）
    if not messages:
        messages = session.get('assistant_messages', [])

    # 防御性清理：丢弃无法配对的孤立 tool 消息（兼容旧库缺 tool_call_id 的脏数据）
    messages = _drop_orphan_tool_messages(messages)
    return messages


def clear_history():
    """清除当前会话的对话历史。"""
    sid = _get_session_id()
    db = get_db()
    cursor = db.cursor()
    cursor.execute('DELETE FROM assistant_conversations WHERE session_id = ?', (sid,))
    db.commit()
    session['assistant_messages'] = []
    session['assistant_write_count'] = 0
    session.modified = True


def _drop_orphan_tool_messages(messages: list) -> list:
    """丢弃无法配对的孤立 tool 消息。

    OpenAI 要求每条 role=tool 消息的 tool_call_id 必须能在前文某条
    assistant 消息的 tool_calls 中找到对应项。这里剔除：
    1. 没有 tool_call_id 的 tool 消息
    2. tool_call_id 在前文 assistant tool_calls 中找不到的 tool 消息
    这样可兼容旧库（迁移前未持久化 tool_call_id）的脏数据，避免 API 400。
    """
    seen_tool_call_ids = set()
    cleaned = []
    for msg in messages:
        role = msg.get('role')
        if role == 'assistant' and msg.get('tool_calls'):
            for tc in msg['tool_calls']:
                tc_id = tc.get('id') if isinstance(tc, dict) else getattr(tc, 'id', None)
                if tc_id:
                    seen_tool_call_ids.add(tc_id)
        elif role == 'tool':
            tc_id = msg.get('tool_call_id')
            if not tc_id or tc_id not in seen_tool_call_ids:
                continue  # 孤立 tool 消息，丢弃
        cleaned.append(msg)
    return cleaned


def _trim_messages(messages: list, max_recent: int) -> list:
    """截断消息历史，保留首条 system + 最近 max_recent 条，且保证
    tool_call / tool_result 配对完整，避免出现孤立的 tool 消息导致 API 400。

    OpenAI 要求每条 role=tool 消息的 tool_call_id 必须能在前文某条
    assistant 消息的 tool_calls 中找到对应项，否则报
    'tool result's tool id not found'。简单的尾部切片可能把
    assistant(tool_calls) 切掉而留下其后的 tool 结果，必须丢弃这些孤儿。
    """
    if len(messages) <= max_recent + 1:
        return list(messages)

    head = messages[:1]  # system
    recent = messages[-max_recent:]

    # 收集 recent 段中所有 assistant tool_call 的 id
    tool_call_ids = set()
    for msg in recent:
        if msg.get('role') == 'assistant' and msg.get('tool_calls'):
            for tc in msg['tool_calls']:
                tc_id = tc.get('id') if isinstance(tc, dict) else getattr(tc, 'id', None)
                if tc_id:
                    tool_call_ids.add(tc_id)

    # 从 recent 段开头丢弃找不到配对 assistant 的孤立 tool 消息
    while recent:
        first = recent[0]
        if first.get('role') == 'tool' and first.get('tool_call_id') not in tool_call_ids:
            recent = recent[1:]
        else:
            break

    return head + recent


def _save_history(messages: list):
    """保存消息历史到数据库。"""
    sid = _get_session_id()
    db = get_db()
    cursor = db.cursor()

    # 为了简单，每次全量删除后重新写入
    cursor.execute('DELETE FROM assistant_conversations WHERE session_id = ?', (sid,))

    # 保留 system + 最近若干条消息，但必须保证 tool_call / tool_result 配对完整
    trimmed = _trim_messages(messages, MAX_HISTORY_ROUNDS * 2)

    for msg in trimmed:
        tool_calls = None
        if msg.get('tool_calls'):
            tool_calls = json.dumps(msg['tool_calls'], ensure_ascii=False)
        tool_call_id = msg.get('tool_call_id')
        cursor.execute('''
            INSERT INTO assistant_conversations (session_id, role, content, tool_calls, tool_call_id)
            VALUES (?, ?, ?, ?, ?)
        ''', (
            sid,
            msg['role'],
            msg.get('content', ''),
            tool_calls,
            tool_call_id,
        ))
    db.commit()

    # 同时更新 session 做兼容
    session['assistant_messages'] = trimmed
    session.modified = True


def _get_write_count() -> int:
    return session.get('assistant_write_count', 0)


def _increment_write_count():
    session['assistant_write_count'] = _get_write_count() + 1
    session.modified = True


def _check_limits(settings: dict) -> Optional[str]:
    """检查会话限制，超限返回错误信息。"""
    messages = _get_history()
    # 不算 system 消息
    user_assistant_count = max(0, len(messages) - 1)
    max_msgs = settings.get('max_messages_per_session', 50)
    if user_assistant_count >= max_msgs:
        return f'本会话消息数已达上限（{max_msgs} 条），请刷新页面重新开始。'

    max_writes = settings.get('max_write_actions_per_session', 30)
    if _get_write_count() >= max_writes:
        return f'本会话写入操作数已达上限（{max_writes} 次），请刷新页面重新开始。'

    return None


def _build_tool_result_message(tool_call_id: str, content: Any) -> dict:
    return {
        'role': 'tool',
        'tool_call_id': tool_call_id,
        'content': json.dumps(content, ensure_ascii=False, default=str),
    }


def _build_assistant_message(content: str, tool_calls: Optional[list] = None) -> dict:
    msg = {'role': 'assistant', 'content': content}
    if tool_calls:
        msg['tool_calls'] = tool_calls
    return msg


def _call_openai(messages: list, settings: dict) -> Any:
    """调用 OpenAI API，返回 completion 对象。"""
    creds = get_openai_credentials()
    if not creds['api_key']:
        raise RuntimeError('AI 助手尚未配置 OpenAI API Key')

    client = OpenAI(api_key=creds['api_key'], base_url=creds['base_url'])
    return client.chat.completions.create(
        model=creds['model'],
        messages=messages,
        tools=TOOL_SCHEMAS,
        tool_choice='auto',
        temperature=0.3,
        max_tokens=2048,
    )


def _prepare_confirmation_response(tool_name: str, params: dict, analysis: dict,
                                   settings: dict, session_id: str) -> dict:
    """为写入工具创建待确认记录并返回确认卡片。"""
    ttl = settings.get('confirmation_ttl_seconds', 300)
    confirmation_id = create_pending_confirmation(
        action=tool_name,
        params=params,
        summary=analysis['summary'],
        session_id=session_id,
        ttl_seconds=ttl,
    )

    affected = []
    if 'alerts' in analysis:
        affected = [{'id': a.get('id'), 'name': a.get('filename')} for a in analysis['alerts'][:10]]
    elif 'filename' in analysis:
        affected = [{'id': analysis.get('video_id') or analysis.get('task_id'), 'name': analysis['filename']}]

    return {
        'type': 'confirmation_required',
        'message': {
            'role': 'assistant',
            'content': f'{analysis["summary"]}，是否确认执行？',
        },
        'confirmation': {
            'id': confirmation_id,
            'action': tool_name,
            'summary': analysis['summary'],
            'affected_count': analysis.get('alert_count') or analysis.get('event_count') or 1,
            'affected': affected,
            'expires_at': None,  # 前端不严格要求
        },
    }


def _execute_tool(tool_name: str, params: dict, settings: dict) -> dict:
    """执行单个工具，返回工具结果或确认请求。"""
    if tool_name in READ_TOOL_FUNCTIONS:
        return READ_TOOL_FUNCTIONS[tool_name](**params)

    if tool_name in WRITE_TOOLS:
        analysis = analyze_write_tool(tool_name, params)
        if 'error' in analysis:
            return {'error': analysis['error']}
        # 写入操作需要确认
        return _prepare_confirmation_response(tool_name, params, analysis, settings, _get_session_id())

    return {'error': f'未实现的工具: {tool_name}'}


def chat(user_message: str, session_id: str) -> dict:
    """处理用户消息，返回结构化响应。"""
    if not is_configured():
        return {
            'type': 'error',
            'message': {
                'role': 'assistant',
                'content': 'AI 助手尚未配置。请管理员在右上角设置中填写 OpenAI API Key。',
            },
            'error_code': 'NOT_CONFIGURED',
        }

    settings = get_assistant_settings()

    limit_error = _check_limits(settings)
    if limit_error:
        return {
            'type': 'error',
            'message': {'role': 'assistant', 'content': limit_error},
            'error_code': 'SESSION_LIMIT',
        }

    messages = _get_history()
    if not messages:
        messages = [{'role': 'system', 'content': SYSTEM_PROMPT}]

    messages.append({'role': 'user', 'content': user_message})

    try:
        completion = _call_openai(messages, settings)
    except Exception as e:
        return {
            'type': 'error',
            'message': {'role': 'assistant', 'content': f'调用 AI 服务失败: {str(e)}'},
            'error_code': 'OPENAI_ERROR',
        }

    assistant_msg = completion.choices[0].message

    # 处理工具调用循环（最多 5 轮，防止无限循环）
    max_tool_rounds = 5
    for _ in range(max_tool_rounds):
        if not assistant_msg.tool_calls:
            break

        messages.append({
            'role': 'assistant',
            'content': assistant_msg.content or '',
            'tool_calls': [tc.model_dump() for tc in assistant_msg.tool_calls],
        })

        # 收集需要用户确认的写入操作：只要有一个就立即返回确认卡片
        pending_confirmations = []
        tool_results = []

        for tc in assistant_msg.tool_calls:
            tool_name = tc.function.name
            try:
                params = json.loads(tc.function.arguments)
            except Exception:
                params = {}
            result = _execute_tool(tool_name, params, settings)

            if isinstance(result, dict) and result.get('type') == 'confirmation_required':
                pending_confirmations.append(result)
            else:
                tool_results.append(_build_tool_result_message(tc.id, result))

        # 如果有确认请求，优先返回第一个，不再继续调用
        if pending_confirmations:
            # 保存历史到用户提问为止（工具调用和结果先不存，等确认后再说）
            _save_history(messages[:-1])  # 去掉刚加入的 assistant tool_calls 消息
            return pending_confirmations[0]

        messages.extend(tool_results)

        try:
            completion = _call_openai(messages, settings)
        except Exception as e:
            return {
                'type': 'error',
                'message': {'role': 'assistant', 'content': f'调用 AI 服务失败: {str(e)}'},
                'error_code': 'OPENAI_ERROR',
            }
        assistant_msg = completion.choices[0].message

    # 最终文本回复
    final_content = assistant_msg.content or ''
    messages.append({'role': 'assistant', 'content': final_content})
    _save_history(messages)

    return {
        'type': 'message',
        'message': {
            'role': 'assistant',
            'content': final_content,
        },
        'requires_confirmation': False,
    }


def confirm(confirmation_id: str, session_id: str) -> dict:
    """处理用户确认操作。"""
    pending = get_pending_confirmation(confirmation_id, session_id)
    if not pending:
        return {
            'type': 'error',
            'message': {'role': 'assistant', 'content': '操作已过期或不存在，请重新发起。'},
            'error_code': 'CONFIRMATION_EXPIRED',
        }

    def executor(action: str, params: dict) -> dict:
        return execute_write_tool(action, params)

    result = confirm_and_execute(confirmation_id, session_id, executor)
    if not result['success']:
        return {
            'type': 'error',
            'message': {'role': 'assistant', 'content': f'执行失败: {result.get("error", "未知错误")}'},
            'error_code': 'EXECUTION_FAILED',
        }

    _increment_write_count()

    # 把执行结果告知 LLM，让它生成自然语言回复
    settings = get_assistant_settings()
    messages = _get_history()
    messages.append({
        'role': 'user',
        'content': f'用户已确认执行操作：{pending["action"]}。执行结果：{json.dumps(result["result"], ensure_ascii=False, default=str)}。请用中文简要告知用户结果。',
    })

    try:
        completion = _call_openai(messages, settings)
        reply = completion.choices[0].message.content or '操作已执行。'
    except Exception:
        reply = '操作已执行。'

    messages.append({'role': 'assistant', 'content': reply})
    _save_history(messages)

    return {
        'type': 'message',
        'message': {
            'role': 'assistant',
            'content': reply,
        },
        'requires_confirmation': False,
        'executed_result': result['result'],
    }


def cancel(confirmation_id: str, session_id: str) -> dict:
    """处理用户取消操作。"""
    from app.services.assistant_tasks import cancel_confirmation
    if cancel_confirmation(confirmation_id, session_id):
        return {
            'type': 'message',
            'message': {'role': 'assistant', 'content': '已取消操作。'},
            'requires_confirmation': False,
        }
    return {
        'type': 'error',
        'message': {'role': 'assistant', 'content': '操作已过期或不存在。'},
        'error_code': 'CONFIRMATION_EXPIRED',
    }
