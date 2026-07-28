"""误检复核工作台 + 智能审查路由"""
import base64
import json
import os
import threading
import time

from flask import Blueprint, request, jsonify, render_template

from app.database import get_db, DATABASE_PATH
from app.services import api_config_service
from app.services.eval_service import get_effective_status

bp = Blueprint('review', __name__, url_prefix='/review')

# 批量智能审查任务状态：{batch_id: {task_id, total, done, status, current_id, results, error}}
_ai_batches = {}
_ai_batches_lock = threading.Lock()


@bp.route('/<int:task_id>/')
def workbench_page(task_id):
    """复核工作台页面。"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, name, status, finalized FROM eval_tasks WHERE id = ?', (task_id,))
    task = cursor.fetchone()
    if not task:
        return '<h2>评测任务不存在</h2><p><a href="/evaluation/">返回评测列表</a></p>', 404
    return render_template('review_workbench.html', task=dict(task))


@bp.route('/api/<int:task_id>/alerts', methods=['GET'])
def get_alerts(task_id):
    """返回该任务的全量告警结果（前端做筛选/分组）。

    每条记录包含：id, video_id, event_type, image_ids[], representative_image_id,
    ts_start, ts_end, is_false_positive, manual_status, effective_status,
    ai_suggestion, matched_gt_event_id, ocr_timestamp_seconds
    """
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id FROM eval_tasks WHERE id = ?', (task_id,))
    if not cursor.fetchone():
        return jsonify({'error': '任务不存在'}), 404

    cursor.execute('''
        SELECT m.id, m.video_id, m.event_type, m.image_ids,
               m.representative_image_id, m.ts_start, m.ts_end,
               m.is_false_positive, m.matched_gt_event_id, m.manual_status,
               m.ai_suggestion,
               o.timestamp_seconds
        FROM eval_merged_events m
        LEFT JOIN (
            SELECT alert_image_id, timestamp_seconds
            FROM ocr_results
            WHERE id IN (SELECT MAX(id) FROM ocr_results GROUP BY alert_image_id)
        ) o ON o.alert_image_id = m.representative_image_id
        WHERE m.task_id = ?
        ORDER BY m.video_id, m.event_type, m.ts_start
    ''', (task_id,))
    rows = [dict(r) for r in cursor.fetchall()]

    for r in rows:
        r['image_ids'] = json.loads(r.get('image_ids') or '[]')
        r['effective_status'] = get_effective_status(r)
        # ai_suggestion 解析为 dict
        raw = r.get('ai_suggestion')
        if raw:
            try:
                r['ai_suggestion'] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                r['ai_suggestion'] = None
        else:
            r['ai_suggestion'] = None

    return jsonify({'success': True, 'alerts': rows, 'count': len(rows)})


@bp.route('/api/<int:task_id>/gt-context', methods=['GET'])
def gt_context(task_id):
    """返回该任务某视频的 GT 事件区间 + 同视频所有告警时间点，供时间轴渲染。"""
    video_id = request.args.get('video_id')
    if not video_id:
        return jsonify({'error': '缺少 video_id'}), 400

    db = get_db()
    cursor = db.cursor()

    # GT 事件区间
    cursor.execute('''
        SELECT id, event_type, start_sec, end_sec
        FROM eval_gt_events
        WHERE task_id = ? AND video_id = ?
        ORDER BY start_sec
    ''', (task_id, video_id))
    gt_events = [dict(r) for r in cursor.fetchall()]

    # 同视频所有告警的时间点
    cursor.execute('''
        SELECT id, event_type, ts_start, ts_end, is_false_positive, manual_status
        FROM eval_merged_events
        WHERE task_id = ? AND video_id = ?
        ORDER BY ts_start
    ''', (task_id, video_id))
    alerts = []
    for r in cursor.fetchall():
        r = dict(r)
        r['effective_status'] = get_effective_status(r)
        alerts.append(r)

    return jsonify({
        'success': True,
        'gt_events': gt_events,
        'alerts': alerts,
    })


@bp.route('/api/<int:task_id>/ai-check', methods=['POST'])
def ai_check(task_id):
    """提交批量智能审查任务（异步，立即返回 batch_id）。

    body: {merged_ids: [int, ...]}
    后台线程串行调用多模态模型，逐条写入 ai_suggestion，不阻塞请求线程。
    """
    data = request.get_json() or {}
    merged_ids = data.get('merged_ids', [])
    if not merged_ids or not isinstance(merged_ids, list):
        return jsonify({'error': '请提供 merged_ids 列表'}), 400

    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id FROM eval_tasks WHERE id = ?', (task_id,))
    if not cursor.fetchone():
        return jsonify({'error': '任务不存在'}), 404

    creds = api_config_service.get_openai_creds()
    if not creds.get('api_key'):
        return jsonify({'error': '未配置 OpenAI 兼容 API，请在 /api-config/ 页面配置'}), 400

    # 取这些 merged 记录 + 代表图路径 + 匹配的 GT
    placeholders = ','.join('?' for _ in merged_ids)
    cursor.execute(f'''
        SELECT m.id, m.video_id, m.event_type, m.ts_start, m.ts_end,
               m.is_false_positive, m.matched_gt_event_id,
               a.file_path, a.filename
        FROM eval_merged_events m
        LEFT JOIN alert_images a ON a.id = m.representative_image_id
        WHERE m.id IN ({placeholders}) AND m.task_id = ?
    ''', list(merged_ids) + [task_id])
    merged_rows = [dict(r) for r in cursor.fetchall()]

    if not merged_rows:
        return jsonify({'error': '未找到匹配的告警记录'}), 404

    # 生成 batch_id（time + 数量，避免随机）
    batch_id = f'{int(time.time())}_{len(merged_rows)}'
    with _ai_batches_lock:
        _ai_batches[batch_id] = {
            'task_id': task_id,
            'total': len(merged_rows),
            'done': 0,
            'status': 'running',
            'current_id': None,
            'results': [],  # 已完成的结果 [{merged_id, suggestion}]
            'error': None,
        }

    # 后台线程跑，用独立 DB 连接（不依赖请求上下文）
    thread = threading.Thread(
        target=_ai_check_worker,
        args=(batch_id, task_id, merged_rows, creds),
        daemon=True,
    )
    thread.start()

    return jsonify({'success': True, 'batch_id': batch_id, 'total': len(merged_rows)})


@bp.route('/api/<int:task_id>/ai-check/status', methods=['GET'])
def ai_check_status(task_id):
    """轮询批量智能审查进度。返回 {status, total, done, current_id, results}。"""
    batch_id = request.args.get('batch_id')
    if not batch_id:
        return jsonify({'error': '缺少 batch_id'}), 400
    with _ai_batches_lock:
        batch = _ai_batches.get(batch_id)
        if not batch:
            return jsonify({'error': '批次不存在'}), 404
        # 只返回该 task_id 的批次
        if batch['task_id'] != task_id:
            return jsonify({'error': '批次不属于该任务'}), 404
        return jsonify({
            'success': True,
            'status': batch['status'],
            'total': batch['total'],
            'done': batch['done'],
            'current_id': batch['current_id'],
            'results': batch['results'],
            'error': batch['error'],
        })


def _ai_check_worker(batch_id, task_id, merged_rows, creds):
    """后台串行执行批量智能审查，逐条写库并更新进度。"""
    import sqlite3
    from openai import OpenAI

    client = OpenAI(api_key=creds['api_key'], base_url=creds['base_url'])
    interval = api_config_service.get_openai_request_interval()

    # 独立 DB 连接（后台线程不能用 Flask 请求上下文的 get_db）
    conn = sqlite3.connect(str(DATABASE_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    try:
        for mr in merged_rows:
            # 检查是否被取消
            with _ai_batches_lock:
                if _ai_batches[batch_id]['status'] == 'cancelled':
                    break
                _ai_batches[batch_id]['current_id'] = mr['id']

            img_path = mr.get('file_path')
            if not img_path or not os.path.exists(img_path):
                suggestion = {'verdict': 'error', 'reason': '告警图片文件不存在，无法审查'}
            else:
                try:
                    suggestion = _review_one(client, creds['model'], mr, task_id, cur)
                except Exception as e:
                    suggestion = {'verdict': 'error', 'reason': f'模型调用失败：{e}'}

            # 写入 ai_suggestion（不改 manual_status）
            suggestion_json = json.dumps(suggestion, ensure_ascii=False)
            cur.execute(
                'UPDATE eval_merged_events SET ai_suggestion = ? WHERE id = ? AND task_id = ?',
                (suggestion_json, mr['id'], task_id)
            )
            conn.commit()

            # 更新进度
            with _ai_batches_lock:
                b = _ai_batches[batch_id]
                b['done'] += 1
                b['results'].append({'merged_id': mr['id'], 'suggestion': suggestion})
                b['current_id'] = None

            # 限流
            if interval > 0:
                time.sleep(interval)

        with _ai_batches_lock:
            _ai_batches[batch_id]['status'] = 'done'
    except Exception as e:
        with _ai_batches_lock:
            _ai_batches[batch_id]['status'] = 'error'
            _ai_batches[batch_id]['error'] = str(e)
    finally:
        conn.close()


def _review_one(client, model_name, merged_row, task_id, cursor):
    """对单条告警做多模态审查，返回 {verdict, reason}。"""
    mime, b64 = _encode_image(merged_row['file_path'])
    image_url = f"data:{mime};base64,{b64}"

    # 取匹配的 GT 事件信息（若有）
    gt_info = ''
    gt_id = merged_row.get('matched_gt_event_id')
    if gt_id:
        cursor.execute(
            'SELECT event_type, start_sec, end_sec FROM eval_gt_events WHERE id = ? AND task_id = ?',
            (gt_id, task_id)
        )
        g = cursor.fetchone()
        if g:
            gt_info = f"\n对应的 Ground Truth 事件：类型={g['event_type']}，区间={g['start_sec']}s~{g['end_sec']}s。"

    ts_start = merged_row.get('ts_start')
    ts_end = merged_row.get('ts_end')
    ts_desc = f"{ts_start}s"
    if ts_end is not None and ts_end != ts_start:
        ts_desc = f"{ts_start}s~{ts_end}s"

    # 取事件类型的中文名与描述，让模型理解判定语义
    from app.event_types import get_type_names, get_type_descriptions
    etype = merged_row['event_type']
    type_name = get_type_names().get(etype, etype)
    type_desc = get_type_descriptions().get(etype, '')

    # 判定事件属于"违规检测类"还是"现象检测类"
    violation_kw = ['未戴', '未穿', '不戴', '未关', '离岗', '占用', '睡觉', '睡岗', '未佩戴']
    is_violation = any(k in type_desc for k in violation_kw)

    if is_violation:
        rule = (
            "【判定规则 - 违规检测类，注意方向是反的】\n"
            f"该告警声称【检测到有人违规】（如有人未穿戴规定服装/未在岗/通道被堵等）。\n"
            "- correct（正确告警）：图中确实能找到违规的人或行为（例如有人未戴厨师帽/未戴口罩/未穿反光衣，或垃圾桶未关，或人员离岗）。\n"
            "- false_positive（误检）：图中所有人都合规、未发现任何违规。注意：如果图中多人都正确穿戴了规定服装，这恰恰说明没有违规，应判为误检，不能因为'看到厨师服/口罩'就判 correct。\n"
            "- 你必须在理由中明确指出：是否看到了违规的人，以及具体哪里违规。若找不到违规者，判 false_positive。"
        )
    else:
        rule = (
            "【判定规则 - 现象检测类】\n"
            f"该告警声称【图中出现了该现象】（如老鼠/火焰/抽烟/打电话/跌倒等）。\n"
            "- correct（正确告警）：图中确实清晰存在该现象。\n"
            "- false_positive（误检）：图中没有该现象，或识别错误。"
        )

    prompt = (
        "你是一名告警复核专家。请分析这张告警截图，判断该告警是真实事件（correct）还是误检（false_positive）。\n\n"
        f"告警信息：视频ID={merged_row['video_id']}，事件类型={type_name}（{etype}），"
        f"触发时间={ts_desc}。{gt_info}\n"
        f"事件类型说明：{type_desc}\n\n"
        f"{rule}\n\n"
        "严格按以下 JSON 格式返回（只返回 JSON，不要其它文字）：\n"
        '{"verdict": "correct 或 false_positive", "reason": "简短中文理由（30字内），必须说明是否看到违规者/现象"}'
    )

    messages = [
        {"role": "system", "content": "你是告警复核专家，只返回 JSON。"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        },
    ]

    completion = client.chat.completions.create(model=model_name, messages=messages, timeout=120)
    text = (completion.choices[0].message.content or '').strip()

    return _parse_suggestion(text)


def _parse_suggestion(text: str) -> dict:
    """从容错性强的模型返回中解析 {verdict, reason}。

    处理以下情况：
    - Qwen3 等 thinking 模型：<think>...</think> 后才给 JSON
    - JSON 包在 ```json ``` 代码块里
    - JSON 前后有多余文字
    """
    import re

    raw = text
    # 1. 去掉 <think>...</think> 块（thinking 模型）
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

    # 2. 去掉 ```json ... ``` 代码块包裹
    if '```' in text:
        m = re.search(r'```(?:json)?\s*(.*?)```', text, re.DOTALL)
        if m:
            text = m.group(1).strip()

    # 3. 直接解析
    try:
        result = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        # 4. 正则提取第一个 {...} 对象
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if not m:
            return {'verdict': 'error', 'reason': f'模型返回无法解析：{raw[:60]}'}
        try:
            result = json.loads(m.group(0))
        except (json.JSONDecodeError, TypeError):
            return {'verdict': 'error', 'reason': f'模型返回无法解析：{raw[:60]}'}

    verdict = result.get('verdict', 'error')
    if verdict not in ('correct', 'false_positive'):
        verdict = 'error'
    return {'verdict': verdict, 'reason': result.get('reason', '')}


def _encode_image(image_path: str) -> tuple:
    """将图片转为 base64，返回 (mime_type, base64_string)。复用 behavior_analysis_service 的实现。"""
    from app.services.behavior_analysis_service import _encode_image as _ba_encode
    return _ba_encode(image_path)
