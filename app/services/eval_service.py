"""评测业务逻辑服务层

包含核心分析、指标计算、报告生成等纯业务逻辑，
不依赖 Flask 请求上下文，可独立测试和复用。
"""

import io
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def calc_expected_count(start_sec, end_sec, interval_sec, trigger_rate, min_event_duration_sec=0):
    """计算预期触发次数"""
    if end_sec - start_sec < min_event_duration_sec:
        return 0
    raw = (end_sec - start_sec - 1) / interval_sec * trigger_rate
    return max(1, round(raw))


def get_effective_status(row):
    """根据 manual_status 解析有效状态"""
    manual = row['manual_status']
    if manual == 'correct':
        return 'correct'
    if manual == 'false_positive':
        return 'false_positive'
    if manual == 'ignored':
        return 'ignored'
    # auto 或未设置时，以 is_false_positive 字段为准
    is_fp = row['is_false_positive']
    return 'false_positive' if is_fp else 'correct'


def _emit_group(merged_alerts, vid, etype, group):
    """将一组告警图片合并/保留为一条记录（单张也保留）"""
    image_ids = [img['id'] for img in group]
    rep_idx = len(image_ids) // 2
    representative_image_id = image_ids[rep_idx]
    ts_start = group[0]['timestamp_seconds']
    ts_end = group[-1]['timestamp_seconds']
    # 保存所有图片的详细信息供前端显示
    all_images = []
    for img in group:
        all_images.append({
            'id': img['id'],
            'filename': img.get('filename'),
            'file_path': img.get('file_path'),
            'timestamp_seconds': img.get('timestamp_seconds')
        })
    merged_alerts.append({
        'video_id': vid,
        'event_type': etype,
        'image_ids': image_ids,
        'all_images': all_images,
        'representative_image_id': representative_image_id,
        'ts_start': ts_start,
        'ts_end': ts_end,
        'is_single': len(image_ids) == 1,
    })


def analyze_merged_events(task_id, db):
    """
    分析并返回合并告警组（以告警图片为中心）和 GT 事件列表。

    返回格式:
    {
        "merged_alerts": [...],  # 合并后的告警组
        "gt_events": [...],      # GT 事件列表（带中间帧）
        "missing_video_ids": [...]  # 告警集中有但评测视频集中缺失的 video_id
    }
    """
    cursor = db.cursor()

    cursor.execute('SELECT * FROM eval_tasks WHERE id = ?', (task_id,))
    task = cursor.fetchone()
    if not task:
        return None

    dataset_id = task['dataset_id']
    alert_eval_set_id = task['alert_eval_set_id'] if 'alert_eval_set_id' in task.keys() else None
    eval_set_id = task['eval_set_id']
    merge_interval = task['merge_interval_sec']
    ev_interval = task['event_interval_sec']
    trigger_rate = task['trigger_rate']
    min_event_duration_sec = task['min_event_duration_sec'] if task['min_event_duration_sec'] is not None else 0

    # ── 获取评测视频集的 video db_id 列表 ─────────────────────────────────────
    cursor.execute('SELECT video_ids FROM eval_video_sets WHERE id = ?', (eval_set_id,))
    eval_set = cursor.fetchone()
    if not eval_set:
        return {'merged_alerts': [], 'gt_events': []}

    eval_video_db_ids = []
    if eval_set['video_ids']:
        try:
            eval_video_db_ids = json.loads(eval_set['video_ids'])
        except Exception:
            eval_video_db_ids = []

    if not eval_video_db_ids:
        return {'merged_alerts': [], 'gt_events': []}

    # ── 获取告警来源中的所有已 OCR 图片 ───────────────────────────────────────
    alert_sql = '''
        SELECT a.id, a.filename, a.file_path, a.event_label, a.alert_type,
               o.video_id, o.timestamp_seconds
        FROM alert_images a
        LEFT JOIN (
            SELECT alert_image_id, video_id, timestamp_seconds
            FROM ocr_results
            WHERE id IN (
                SELECT MAX(id) FROM ocr_results GROUP BY alert_image_id
            )
        ) o ON o.alert_image_id = a.id
        WHERE o.video_id IS NOT NULL
          AND o.timestamp_seconds IS NOT NULL
    '''
    alert_params = []
    if alert_eval_set_id:
        cursor.execute('SELECT dataset_ids FROM eval_alert_sets WHERE id = ?', (alert_eval_set_id,))
        alert_set = cursor.fetchone()
        if not alert_set:
            return {'merged_alerts': [], 'gt_events': []}
        try:
            alert_dataset_ids = json.loads(alert_set['dataset_ids'] or '[]')
        except Exception:
            alert_dataset_ids = []
        if not alert_dataset_ids:
            return {'merged_alerts': [], 'gt_events': []}
        placeholders = ','.join('?' for _ in alert_dataset_ids)
        alert_sql += f' AND a.dataset_id IN ({placeholders})'
        alert_params.extend(alert_dataset_ids)
    else:
        alert_sql += ' AND a.dataset_id = ?'
        alert_params.append(dataset_id)

    cursor.execute(alert_sql, alert_params)
    alert_images = [dict(r) for r in cursor.fetchall()]

    # ── 校验：告警集中的 video_id 是否全部包含在评测视频集中 ───────────────────
    eval_video_ids = set()
    if eval_video_db_ids:
        placeholders = ','.join('?' for _ in eval_video_db_ids)
        cursor.execute(f'SELECT video_id FROM videos WHERE id IN ({placeholders})', eval_video_db_ids)
        for row in cursor.fetchall():
            if row['video_id']:
                eval_video_ids.add(row['video_id'])

    alert_video_ids = set()
    for img in alert_images:
        vid = img.get('video_id')
        if vid:
            alert_video_ids.add(vid)

    missing_video_ids = sorted(alert_video_ids - eval_video_ids)

    # ── 按 (video_id, event_type) 分组 ────────────────────────────────────────
    groups = {}  # key: (video_id, event_type) → list of images
    for img in alert_images:
        vid = img.get('video_id')
        etype = img.get('event_label') or img.get('alert_type')
        if not vid or not etype:
            continue
        key = (vid, etype)
        groups.setdefault(key, []).append(img)

    # ── 在每组内按时间戳合并 ───────────────────────────────────────────────────
    merged_alerts = []
    for (vid, etype), imgs in groups.items():
        imgs_sorted = sorted(imgs, key=lambda x: x['timestamp_seconds'])

        current_group = [imgs_sorted[0]]
        for img in imgs_sorted[1:]:
            prev_ts = current_group[-1]['timestamp_seconds']
            cur_ts = img['timestamp_seconds']
            if cur_ts - prev_ts <= merge_interval:
                current_group.append(img)
            else:
                # emit current group
                _emit_group(merged_alerts, vid, etype, current_group)
                current_group = [img]
        _emit_group(merged_alerts, vid, etype, current_group)

    # ── 获取评测视频集中所有 GT 事件 ───────────────────────────────────────────
    placeholders = ','.join('?' for _ in eval_video_db_ids)
    cursor.execute(f'''
        SELECT e.*, v.video_id
        FROM events e
        JOIN videos v ON v.id = e.video_db_id
        WHERE e.video_db_id IN ({placeholders})
        ORDER BY v.video_id, e.event_type, e.start_seconds
    ''', eval_video_db_ids)
    gt_events_raw = [dict(r) for r in cursor.fetchall()]

    # 同一 video_id 可能对应多条 videos 记录，去重避免 GT 事件重复展示
    seen = set()
    gt_events_dedup = []
    for ev in gt_events_raw:
        key = (ev.get('video_id'), ev.get('event_type'), ev.get('start_seconds'), ev.get('end_seconds'))
        if key not in seen:
            seen.add(key)
            gt_events_dedup.append(ev)
    gt_events_raw = gt_events_dedup

    # ── 为每个 GT 事件找中间帧 ─────────────────────────────────────────────────
    gt_events = []
    for ev in gt_events_raw:
        vid = ev.get('video_id')
        etype = ev.get('event_type')
        start_sec = ev.get('start_seconds', 0)
        end_sec = ev.get('end_seconds', 0)
        mid_ts = (start_sec + end_sec) / 2

        expected = calc_expected_count(start_sec, end_sec, ev_interval, trigger_rate, min_event_duration_sec)

        # 找中间帧
        mid_frame_id = None
        mid_frame_path = None
        cursor.execute('''
            SELECT id, file_path FROM gt_frames
            WHERE event_id = ?
            ORDER BY ABS(timestamp_sec - ?) LIMIT 1
        ''', (ev.get('id'), mid_ts))
        frame_row = cursor.fetchone()
        if frame_row:
            mid_frame_id = frame_row['id']
            mid_frame_path = frame_row['file_path']

        gt_events.append({
            'gt_event_id': ev.get('id'),
            'video_id': vid,
            'event_type': etype,
            'start_sec': start_sec,
            'end_sec': end_sec,
            'expected_count': expected,
            'confirmed_count': expected,
            'mid_frame_id': mid_frame_id,
            'mid_frame_path': mid_frame_path,
        })

    # 按时间戳对合并告警组排序
    merged_alerts.sort(key=lambda x: x['ts_start'])
    return {'merged_alerts': merged_alerts, 'gt_events': gt_events, 'missing_video_ids': missing_video_ids}


def get_font(size):
    """尝试加载系统中文字体"""
    font_paths = [
        '/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/wqy-microhei/wqy-microhei.ttc',
        '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
    ]
    from PIL import ImageFont
    for fp in font_paths:
        try:
            return ImageFont.truetype(fp, size)
        except Exception:
            pass
    return ImageFont.load_default()


def generate_report_image(task, event_metrics, accuracy, recall, avg_fp_per_hour, total_duration_hours=0,
                          total_duration=0, gt_event_count=0, gt_coverage_seconds=0,
                          gt_coverage_rate=0.0, expected_alert_total=0):
    """用 Pillow 生成评测报告图片"""
    from PIL import Image, ImageDraw

    width = 1200
    margin = 40
    bg_color = (255, 255, 255)
    header_color = (52, 152, 219)  # #3498db
    text_dark = (44, 62, 80)
    text_gray = (100, 100, 100)
    line_color = (220, 220, 220)
    card_bg = (248, 249, 250)
    good_color = (39, 174, 96)
    mid_color = (243, 156, 18)
    bad_color = (231, 76, 60)

    font_title = get_font(36)
    font_subtitle = get_font(24)
    font_normal = get_font(20)
    font_small = get_font(18)
    font_table = get_font(18)

    def _fmt_duration(sec):
        if sec <= 0:
            return "0s"
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = int(sec % 60)
        parts = []
        if h > 0:
            parts.append(f"{h}h")
        if m > 0:
            parts.append(f"{m}m")
        if s > 0 or not parts:
            parts.append(f"{s}s")
        return " ".join(parts)

    # 预估高度
    row_height = 46
    header_height = 160
    overall_height = 140
    stats_height = 120
    desc_height = 70
    section_gap = 30
    table_header = 50
    table_rows = (len(event_metrics) + 1) * row_height  # +1 合计行
    height = (header_height + overall_height + stats_height + desc_height
              + section_gap * 3 + 60 + table_header + table_rows + margin * 2)

    img = Image.new('RGB', (width, height), bg_color)
    draw = ImageDraw.Draw(img)

    y = 0

    # 顶部蓝条
    draw.rectangle([0, 0, width, header_height - 40], fill=header_color)
    draw.text((width // 2, 40), "评测报告", font=font_title, fill=(255, 255, 255), anchor="mt")

    # 任务名 + 评估时间
    y = header_height - 30
    task_name = task.get('name', '-')
    created_at = task.get('created_at', '-')
    if created_at:
        if isinstance(created_at, str):
            try:
                dt = datetime.strptime(created_at, '%Y-%m-%d %H:%M:%S')
                dt = dt.replace(tzinfo=timezone.utc).astimezone()
                eval_time = dt.strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                eval_time = created_at
        elif isinstance(created_at, datetime):
            if created_at.tzinfo is None:
                dt = created_at.replace(tzinfo=timezone.utc).astimezone()
            else:
                dt = created_at.astimezone()
            eval_time = dt.strftime('%Y-%m-%d %H:%M:%S')
        else:
            eval_time = str(created_at)
    else:
        eval_time = '-'
    draw.text((margin, y), f"任务：{task_name}", font=font_subtitle, fill=text_dark)
    y += 40
    draw.text((margin, y), f"评估时间：{eval_time}", font=font_normal, fill=text_gray)
    y += 50

    # 整体指标区域
    def draw_card(x, w, label, value, color):
        draw.rounded_rectangle([x, y, x + w, y + 100], radius=8, fill=card_bg)
        draw.text((x + w // 2, y + 20), label, font=font_small, fill=text_gray, anchor="mt")
        draw.text((x + w // 2, y + 60), value, font=font_subtitle, fill=color, anchor="mt")

    card_w = (width - margin * 2 - 40) // 3
    acc_str = f"{(accuracy * 100):.1f}%" if accuracy is not None else "N/A"
    rec_str = f"{(recall * 100):.1f}%" if recall is not None else "N/A"
    fp_str = f"{avg_fp_per_hour:.2f}" if avg_fp_per_hour is not None else "N/A"

    def fp_color(val):
        if val is None:
            return text_gray
        if val <= 4.0:
            return good_color
        if val <= 10.0:
            return mid_color
        return bad_color

    draw_card(margin, card_w, "整体精确率", acc_str,
              good_color if accuracy and accuracy >= 0.8 else mid_color if accuracy and accuracy >= 0.5 else bad_color)
    draw_card(margin + card_w + 20, card_w, "整体召回率", rec_str,
              good_color if recall and recall >= 0.8 else mid_color if recall and recall >= 0.5 else bad_color)
    draw_card(margin + card_w * 2 + 40, card_w, "平均误检数/小时", fp_str,
              fp_color(avg_fp_per_hour))
    y += 120

    # 分隔线
    y += section_gap
    draw.line([margin, y, width - margin, y], fill=line_color, width=2)
    y += section_gap

    # 评测概况统计
    draw.text((margin, y), "评测概况", font=font_subtitle, fill=text_dark)
    y += 40

    stat_card_w = (width - margin * 2 - 80) // 5
    stat_items = [
        ("视频总时长", _fmt_duration(total_duration)),
        ("GT覆盖时长", _fmt_duration(gt_coverage_seconds)),
        ("整体覆盖率", f"{(gt_coverage_rate * 100):.1f}%"),
        ("GT事件总数", str(gt_event_count)),
        ("理论告警数", str(expected_alert_total)),
    ]
    for i, (label, value) in enumerate(stat_items):
        sx = margin + i * (stat_card_w + 20)
        draw.rounded_rectangle([sx, y, sx + stat_card_w, y + 70], radius=6, fill=card_bg)
        draw.text((sx + stat_card_w // 2, y + 12), label, font=font_small, fill=text_gray, anchor="mt")
        draw.text((sx + stat_card_w // 2, y + 40), value, font=font_normal, fill=text_dark, anchor="mt")
    y += 80

    # 评测说明
    desc_lines = [
        "评测方法：通过 OCR 提取告警图片水印中的 video_id 和时间戳，",
        "与 Ground Truth 事件区间比对。告警时间落在 GT 事件 ±5 秒容差范围内视为命中。",
        "召回率 = 命中 GT 事件数 / 总 GT 事件数；精确率 = 正确告警数 / 总告警数。"
    ]
    for line in desc_lines:
        draw.text((margin, y), line, font=font_small, fill=text_gray)
        y += 24
    y += 10

    # 分隔线
    y += section_gap // 2
    draw.line([margin, y, width - margin, y], fill=line_color, width=2)
    y += section_gap

    # 详细指标标题
    draw.text((margin, y), "事件详细指标", font=font_subtitle, fill=text_dark)
    y += 50

    # 表格
    cols = ["事件类型", "告警数", "准确数", "精确率", "GT数", "命中数", "漏检数", "召回率", "误检数", "平均误检/h"]
    col_widths = [150, 90, 90, 100, 80, 80, 80, 100, 90, 110]
    total_table_w = sum(col_widths)
    start_x = margin + (width - margin * 2 - total_table_w) // 2

    def draw_table_row(row_y, cells, is_header=False, is_total=False, cell_colors=None):
        bg = (240, 240, 240) if is_header else (248, 249, 250) if is_total else bg_color
        if is_header or is_total:
            draw.rectangle([start_x, row_y, start_x + total_table_w, row_y + row_height], fill=bg)
        x = start_x
        for i, cell in enumerate(cells):
            text = str(cell)
            fw = col_widths[i]
            anchor = "lm" if i == 0 else "mm"
            tx = x + 10 if i == 0 else x + fw // 2
            font = font_table
            fill = cell_colors[i] if cell_colors and i < len(cell_colors) and cell_colors[i] else text_dark
            draw.text((tx, row_y + row_height // 2), text, font=font, fill=fill, anchor=anchor)
            x += fw
        # 横线
        draw.line([start_x, row_y + row_height, start_x + total_table_w, row_y + row_height], fill=line_color, width=1)

    # 表头
    draw_table_row(y, cols, is_header=True)
    y += row_height

    # 数据行
    for em in event_metrics:
        prec_val = em.get('precision')
        rec_val = em.get('recall')
        fp_val = em.get('avg_fp_per_hour', 0)
        prec = f"{(prec_val * 100):.1f}%" if prec_val is not None else "N/A"
        rec = f"{(rec_val * 100):.1f}%" if rec_val is not None else "N/A"
        fp_txt = f"{fp_val:.2f}"
        cells = [
            em.get('event_type', '-'),
            em.get('alert_count', 0),
            em.get('correct_pred_count', 0),
            prec,
            em.get('gt_count', 0),
            em.get('hit_count', 0),
            em.get('missed_gt_count', 0),
            rec,
            em.get('false_positive_count', 0),
            fp_txt,
        ]
        prec_color = (good_color if prec_val is not None and prec_val >= 0.8
                      else mid_color if prec_val is not None and prec_val >= 0.5
                      else bad_color if prec_val is not None else None)
        rec_color = (good_color if rec_val is not None and rec_val >= 0.8
                     else mid_color if rec_val is not None and rec_val >= 0.5
                     else bad_color if rec_val is not None else None)
        fp_color_val = fp_color(fp_val)
        cell_colors = [None, None, None, prec_color, None, None, None, rec_color, None, fp_color_val]
        draw_table_row(y, cells, cell_colors=cell_colors)
        y += row_height

    # 合计行
    total_alert = sum(em.get('alert_count', 0) for em in event_metrics)
    total_correct = sum(em.get('correct_pred_count', 0) for em in event_metrics)
    total_gt = sum(em.get('gt_count', 0) for em in event_metrics)
    total_hit = sum(em.get('hit_count', 0) for em in event_metrics)
    total_miss = sum(em.get('missed_gt_count', 0) for em in event_metrics)
    total_fp = sum(em.get('false_positive_count', 0) for em in event_metrics)
    overall_avg_fp = round(total_fp / total_duration_hours, 2) if total_duration_hours else 0
    oprec = f"{(accuracy * 100):.1f}%" if accuracy is not None else "N/A"
    orec = f"{(recall * 100):.1f}%" if recall is not None else "N/A"
    total_cells = [
        "合计/整体", total_alert, total_correct, oprec,
        total_gt, total_hit, total_miss, orec, total_fp, f"{overall_avg_fp:.2f}"
    ]
    prec_color = (good_color if accuracy and accuracy >= 0.8
                  else mid_color if accuracy and accuracy >= 0.5 else bad_color)
    rec_color = (good_color if recall and recall >= 0.8
                 else mid_color if recall and recall >= 0.5 else bad_color)
    fp_color_val = fp_color(avg_fp_per_hour)
    cell_colors = [None, None, None, prec_color, None, None, None, rec_color, None, fp_color_val]
    draw_table_row(y, total_cells, is_total=True, cell_colors=cell_colors)

    # 外边框
    draw.rectangle([start_x, y - (len(event_metrics) + 1) * row_height,
                    start_x + total_table_w, y + row_height], outline=line_color, width=1)

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf
