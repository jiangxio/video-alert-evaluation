"""评测业务逻辑服务层

包含核心分析、指标计算、报告生成等纯业务逻辑，
不依赖 Flask 请求上下文，可独立测试和复用。
"""

import base64
import io
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from app.utils import merge_intervals


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


def _analyze_realtime_events(task_id, cursor, task):
    """实时采集模式：每张图片独立一个告警组，无 GT 事件。"""
    dataset_id = task['dataset_id']

    cursor.execute('''
        SELECT a.id, a.filename, a.file_path, a.event_label, a.alert_type
        FROM alert_images a
        WHERE a.dataset_id = ?
        ORDER BY a.uploaded_at ASC
    ''', (dataset_id,))
    images = [dict(r) for r in cursor.fetchall()]

    merged_alerts = []
    for img in images:
        etype = img.get('event_label') or img.get('alert_type') or '未分类'
        merged_alerts.append({
            'video_id': '',
            'event_type': etype,
            'image_ids': [img['id']],
            'all_images': [{
                'id': img['id'],
                'filename': img.get('filename'),
                'file_path': img.get('file_path'),
                'timestamp_seconds': None,
            }],
            'representative_image_id': img['id'],
            'ts_start': None,
            'ts_end': None,
            'is_single': True,
        })

    return {
        'merged_alerts': merged_alerts,
        'gt_events': [],
        'missing_video_ids': [],
    }


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

    # ── 检查是否为实时采集模式 ──────────────────────────────────────────────────
    if task['dataset_id']:
        cursor.execute('SELECT mode FROM datasets WHERE id = ?', (task['dataset_id'],))
        ds_row = cursor.fetchone()
        if ds_row and ds_row['mode'] == 'realtime':
            return _analyze_realtime_events(task_id, cursor, task)

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
        if val <= 5.0:
            return good_color
        if val <= 10.0:
            return mid_color
        return bad_color

    draw_card(margin, card_w, "整体精确率", acc_str,
              good_color if accuracy and accuracy >= 0.85 else mid_color if accuracy and accuracy >= 0.75 else bad_color)
    draw_card(margin + card_w + 20, card_w, "整体召回率", rec_str,
              good_color if recall and recall >= 0.8 else mid_color if recall and recall >= 0.7 else bad_color)
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
        prec_color = (good_color if prec_val is not None and prec_val >= 0.85
                      else mid_color if prec_val is not None and prec_val >= 0.75
                      else bad_color if prec_val is not None else None)
        rec_color = (good_color if rec_val is not None and rec_val >= 0.8
                     else mid_color if rec_val is not None and rec_val >= 0.7
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
    overall_avg_fp = avg_fp_per_hour if avg_fp_per_hour is not None else 0
    oprec = f"{(accuracy * 100):.1f}%" if accuracy is not None else "N/A"
    orec = f"{(recall * 100):.1f}%" if recall is not None else "N/A"
    total_cells = [
        "合计/整体", total_alert, total_correct, oprec,
        total_gt, total_hit, total_miss, orec, total_fp, f"{overall_avg_fp:.2f}"
    ]
    prec_color = (good_color if accuracy and accuracy >= 0.85
                  else mid_color if accuracy and accuracy >= 0.75 else bad_color)
    rec_color = (good_color if recall and recall >= 0.8
                 else mid_color if recall and recall >= 0.7 else bad_color)
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


# ═══════════════════════════════════════════════════════════════════════════════
# 详细算法验证报告生成
# ═══════════════════════════════════════════════════════════════════════════════

def _select_sample_items(items, group_key, time_key, max_items=10):
    """通用样本选择算法：覆盖所有 group，同 group 时间隔开。"""
    from collections import defaultdict

    groups = defaultdict(list)
    for item in items:
        groups[item[group_key]].append(item)

    for gid in groups:
        groups[gid].sort(key=lambda x: x[time_key])

    result = []
    group_ids = sorted(groups.keys())

    # 第一轮：每个 group 取 1 张（取最早的，保证覆盖）
    for gid in group_ids:
        imgs = groups[gid]
        if imgs:
            result.append(imgs.pop(0))

    if len(result) >= max_items:
        return result[:max_items]

    # 第二轮：剩余名额按 group 大小分配，取与已选时间间隔最远的
    remaining = max_items - len(result)
    while remaining > 0:
        best_gid = None
        best_idx = None
        best_dist = -1
        for gid in group_ids:
            imgs = groups[gid]
            if not imgs:
                continue
            selected_times = [r[time_key] for r in result if r[group_key] == gid]
            for i, img in enumerate(imgs):
                t = img[time_key]
                if selected_times:
                    dist = min(abs(t - st) for st in selected_times)
                else:
                    dist = float('inf')
                if dist > best_dist:
                    best_dist = dist
                    best_gid = gid
                    best_idx = i
        if best_gid is None:
            break
        result.append(groups[best_gid].pop(best_idx))
        remaining -= 1

    return result


def _img_to_base64(path, max_width=400):
    """将图片文件转为 Base64 data URL，并压缩尺寸。"""
    try:
        from PIL import Image
        p = Path(path)
        if not p.exists():
            return None
        img = Image.open(p)
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')
        if img.width > max_width:
            ratio = max_width / img.width
            new_h = int(img.height * ratio)
            img = img.resize((max_width, new_h), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=75)
        b64 = base64.b64encode(buf.getvalue()).decode('ascii')
        return f'data:image/jpeg;base64,{b64}'
    except Exception:
        return None


def _call_claude(prompt_text, api_key=None, base_url=None):
    """调用 Claude API，失败时返回 None。

    api_key/base_url/model 优先用传入参数，缺失时回退到统一 API 配置（api_config_service）。
    """
    from app.services import api_config_service
    creds = api_config_service.get_claude_creds()
    if not api_key:
        api_key = creds.get('auth_token')
    if not api_key:
        return None
    if not base_url:
        base_url = creds.get('base_url')
    model = creds.get('model', 'claude-sonnet-5')
    try:
        import anthropic
        kwargs = {'api_key': api_key}
        if base_url:
            kwargs['base_url'] = base_url
        client = anthropic.Anthropic(**kwargs)
        resp = client.messages.create(
            model=model,
            max_tokens=2048,
            system='你是一位计算机视觉算法验证专家，请用中文回答，语言简洁专业。',
            messages=[{'role': 'user', 'content': prompt_text}],
        )
        return resp.content[0].text if resp.content else None
    except Exception:
        return None


def _build_report_html(task, event_metrics, summary_text, conclusion_text,
                       event_detail_list, video_stats,
                       total_duration, gt_event_count, gt_coverage_seconds,
                       gt_coverage_rate, expected_alert_total,
                       report_png_b64, config=None):
    """用字符串拼接生成自包含 HTML 报告。"""
    from datetime import datetime, timezone

    config = config or {}
    task_name = task.get('name', '-')
    report_title = config.get('report_title', '算法验证报告')
    project_name = config.get('project_name', task_name)
    created_at = task.get('created_at', '-')
    eval_time = '-'
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

    accuracy = task.get('accuracy')
    recall = task.get('recall')
    avg_fp = task.get('avg_fp_per_hour')

    def _fmt_pct(v):
        return f"{v*100:.1f}%" if v is not None else 'N/A'

    def _fp_color_class(v):
        if v is None:
            return 'gray'
        if v <= 5.0:
            return 'good'
        if v <= 10.0:
            return 'mid'
        return 'bad'

    def _metric_chip_class(metric_key, metric_value):
        if metric_key in ('alert_count', 'hit_count', 'false_positive_count', 'missed_gt_count'):
            return 'gray'
        if metric_key == 'precision':
            if metric_value is None:
                return 'neutral'
            if metric_value >= 0.85:
                return 'good'
            if metric_value >= 0.75:
                return 'mid'
            return 'bad'
        if metric_key == 'recall':
            if metric_value is None:
                return 'neutral'
            if metric_value >= 0.8:
                return 'good'
            if metric_value >= 0.7:
                return 'mid'
            return 'bad'
        if metric_key == 'avg_fp_per_hour':
            return _fp_color_class(metric_value)
        return 'neutral'

    modules = config.get('modules', ['cover', 'summary', 'method', 'overview', 'events', 'video', 'conclusion'])
    project_bg = config.get('project_background', '')

    def _has(m):
        return m in modules

    # 算法版本信息
    algo_versions = task.get('algorithm_versions')
    algo_version_html = ''
    if algo_versions:
        from app.event_types import get_type_names
        TYPE_NAMES = get_type_names()
        algo_tags = ' '.join([
            f'<span style="display:inline-block;background:#e8f4fd;color:#2980b9;padding:0.15rem 0.5rem;border-radius:10px;font-size:0.85rem;margin:0.15rem 0.3rem 0.15rem 0;">{TYPE_NAMES.get(v["algorithm_type"], v["algorithm_type"])}: {v["name"]} ({v["version_date"]})</span>'
            for v in algo_versions
        ])
        algo_version_html = f'<p><strong>算法版本：</strong>{algo_tags}</p>'

    # 封面
    cover_html = ''
    if _has('cover'):
        bg_block = f'<p><strong>项目背景：</strong>{project_bg}</p>' if project_bg else ''
        cover_html = f'''
    <div class="page">
      <div class="cover">
        <h1>{report_title}</h1>
        <p class="cover-sub">{project_name}</p>
        <div class="cover-meta">
          <p><strong>任务名称：</strong>{task_name}</p>
          {bg_block}
          <p><strong>报告时间：</strong>{eval_time}</p>
          {algo_version_html}
          <p><strong>评测参数：</strong>合并间隔 {task.get('merge_interval_sec', '-')}s / 事件间隔 {task.get('event_interval_sec', '-')}s / 容差 ±5s / 触发率 {task.get('trigger_rate', '-')}</p>
        </div>
      </div>
    </div>
    '''

    # 测试摘要
    summary_html = ''
    if _has('summary'):
        summary_html = f'''
    <div class="page">
      <h2>测试摘要</h2>
      <div class="metrics-row">
        <div class="metric-card {'good' if accuracy and accuracy >= 0.85 else 'mid' if accuracy and accuracy >= 0.75 else 'bad'}">
          <div class="metric-label">整体精确率</div>
          <div class="metric-value">{_fmt_pct(accuracy)}</div>
        </div>
        <div class="metric-card {'good' if recall and recall >= 0.8 else 'mid' if recall and recall >= 0.7 else 'bad'}">
          <div class="metric-label">整体召回率</div>
          <div class="metric-value">{_fmt_pct(recall)}</div>
        </div>
        <div class="metric-card {_fp_color_class(avg_fp)}">
          <div class="metric-label">平均误检数/小时</div>
          <div class="metric-value">{f'{avg_fp:.2f}' if avg_fp is not None else 'N/A'}</div>
        </div>
      </div>
      <div class="ai-section">
        <div class="ai-content">{summary_text or '<em>AI 分析暂不可用</em>'}</div>
      </div>
    </div>
    '''

    # 评测环境（已移除，信息合并到封面）
    env_html = ''

    # 整体指标 PNG
    png_html = ''
    if _has('overview') and report_png_b64:
        png_html = f'''
    <div class="page">
      <h2>整体指标概览</h2>
      <img src="{report_png_b64}" style="max-width:100%;border:1px solid #ddd;border-radius:6px;">
    </div>
    '''

    # 评测方法
    method_html = ''
    if _has('method'):
        method_html = f'''
    <div class="page">
      <h2>评测方法</h2>

      <h3>一、评测参数说明</h3>
      <table class="info-table">
        <tr><td>合并间隔</td><td>{task.get('merge_interval_sec', '-')}s — 同一视频、同一事件类型下，相邻告警时间差 ≤{task.get('merge_interval_sec', '-')}s 时合并，避免单次事件重复触发产生多条告警</td></tr>
        <tr><td>事件间隔</td><td>{task.get('event_interval_sec', '-')}s — 告警触发的时间间隔配置，用于计算理论告警数</td></tr>
        <tr><td>容差</td><td>±5s — 告警时间戳与 GT 事件区间比对时的容错范围</td></tr>
        <tr><td>触发率</td><td>{task.get('trigger_rate', '-')} — 测试评审的严苛程度，理论告警数 = (GT 时长 / 事件间隔) × 触发率</td></tr>
      </table>

      <h3>二、评测概况</h3>
      <table class="info-table">
        <tr><td>评测视频总时长</td><td>{_fmt_duration(total_duration)} — 参与评测的所有视频累计时长</td></tr>
        <tr><td>GT 覆盖时长</td><td>{_fmt_duration(gt_coverage_seconds)} — Ground Truth（人工标注的真实事件）覆盖的时间区间（去重后）</td></tr>
        <tr><td>整体覆盖率</td><td>{(gt_coverage_rate*100):.1f}% — GT 覆盖时长 / 视频总时长，反映测试样本中事件发生的密度</td></tr>
        <tr><td>GT 事件总数</td><td>{gt_event_count} — 人工标注的真实事件总条数</td></tr>
        <tr><td>理论告警数</td><td>{expected_alert_total} — 根据 GT 事件时长、事件间隔和触发率计算得出的预期告警数量</td></tr>
      </table>

      <h3>三、评测指标说明</h3>
      <table class="info-table">
        <tr><td>精确率</td><td>正确告警数 / 总告警数，反映告警的准确性，≥85% 为优</td></tr>
        <tr><td>召回率</td><td>命中 GT 事件数 / 总 GT 事件数，反映漏检情况，≥80% 为优</td></tr>
        <tr><td>平均误检数/小时</td><td>误检告警数 / 评测总时长（小时），反映误报频率，≤5 为优</td></tr>
        <tr><td>正确告警数</td><td>告警时间落在标注时间范围 ±容差 内的总告警数</td></tr>
        <tr><td>误检告警数</td><td>实际产生了告警，但不在任何标注时间段内的总告警数</td></tr>
        <tr><td>命中 GT 事件数</td><td>预期告警中与 GT 事件匹配的数量</td></tr>
      </table>

      <h3>四、评测步骤</h3>
      <div class="step-list">
        <div class="step-item">
          <div class="step-num">1</div>
          <div class="step-body">
            <strong>测试样本准备</strong><br>
            <strong>① 视频数据选取：</strong>选取涵盖多种告警事件类型的视频，事件覆盖率不宜过高（建议低于 30%），以贴近实际场景中事件触发率较低的特点。<br>
            <strong>② 视频打水印：</strong>为每个视频分配唯一 video_id，使用 FFmpeg 在视频左上角添加包含 video_id 和时间戳的水印，便于后续 OCR 识别。<br>
            <strong>③ GT 标注：</strong>对视频进行人工标注，标注每个事件发生的时间段（起始和结束时间），无需标注目标框，形成 Ground Truth（GT）评测基准。
          </div>
        </div>
        <div class="step-item">
          <div class="step-num">2</div>
          <div class="step-body">
            <strong>模型验证测试</strong><br>
            <strong>① 推流部署：</strong>将测试视频通过推流方式推送至被测设备，确保视频流参数（分辨率、帧率等）与实际应用场景一致。<br>
            <strong>② 算法配置：</strong>在被测设备上启动目标检测算法，按照评测参数配置事件检测间隔，并开启告警图片保存功能。<br>
            <strong>③ 告警采集：</strong>完整运行检测流程，采集告警图片（含告警类型），测试完成后将告警数据上传至评测平台。
          </div>
        </div>
        <div class="step-item">
          <div class="step-num">3</div>
          <div class="step-body">
            <strong>指标统计分析</strong><br>
            <strong>① OCR 解析：</strong>对告警图片水印区域进行 OCR 识别，提取 video_id 和时间戳，剔除识别失败的样本。<br>
            <strong>② 告警匹配：</strong>将提取的时间戳与 GT 事件区间进行比对，告警时间落在 GT 事件 ±5 秒容差范围内视为命中（正确告警），否则视为误检。GT 事件区间内无匹配告警则视为漏检。<br>
            <strong>③ 指标计算：</strong>基于匹配结果计算精确率、召回率、平均误检数/小时等核心指标，按事件类型分别统计，生成评测报告。<br>
            <strong>④ 验收判定：</strong>对照预设验收标准判定各项指标是否达标，输出详细报告并对未达标项提出改进建议。
          </div>
        </div>
      </div>
    </div>
    '''

    # 详细案例分析
    event_blocks = []
    for ed in event_detail_list:
        etype = ed['event_type']
        em = ed['metrics']

        # 误检图片网格
        fp_grid = ''
        if ed['fp_samples']:
            fp_cells = ''
            for s in ed['fp_samples']:
                img_tag = f'<img src="{s["b64"]}">' if s.get('b64') else '<div class="img-placeholder">图片加载失败</div>'
                fp_cells += f'''
                <div class="sample-cell">
                  {img_tag}
                  <div class="sample-caption">{s['video_id']} | {s['time_str']}</div>
                </div>'''
            fp_grid = f'''
            <div class="event-sample-section">
              <h4>误检案例（{len(ed["fp_samples"])} 张）</h4>
              <div class="sample-grid">{fp_cells}</div>
            </div>
            '''

        # 漏检图片网格
        miss_grid = ''
        if ed['miss_samples']:
            miss_cells = ''
            for s in ed['miss_samples']:
                img_tag = f'<img src="{s["b64"]}">' if s.get('b64') else '<div class="img-placeholder">图片加载失败</div>'
                miss_cells += f'''
                <div class="sample-cell">
                  {img_tag}
                  <div class="sample-caption">{s['video_id']} | GT: {s['time_str']}</div>
                </div>'''
            miss_grid = f'''
            <div class="event-sample-section">
              <h4>漏检案例（{len(ed["miss_samples"])} 张）</h4>
              <div class="sample-grid">{miss_cells}</div>
            </div>
            '''

        prec = f"{em.get('precision', 0)*100:.1f}%" if em.get('precision') is not None else 'N/A'
        rec = f"{em.get('recall', 0)*100:.1f}%" if em.get('recall') is not None else 'N/A'
        fp_val = em.get('avg_fp_per_hour', 0)
        empty_state = ''
        if not fp_grid and not miss_grid:
            empty_state = '<div class="event-empty">暂无可展示的典型误检或漏检样本</div>'

        metric_chips = ''.join([
            f'<span class="metric-chip metric-chip-{_metric_chip_class("alert_count", em.get("alert_count", 0))}">告警 {em.get("alert_count", 0)}</span>',
            f'<span class="metric-chip metric-chip-{_metric_chip_class("hit_count", em.get("hit_count", 0))}">命中 {em.get("hit_count", 0)}</span>',
            f'<span class="metric-chip metric-chip-{_metric_chip_class("false_positive_count", em.get("false_positive_count", 0))}">误检 {em.get("false_positive_count", 0)}</span>',
            f'<span class="metric-chip metric-chip-{_metric_chip_class("missed_gt_count", em.get("missed_gt_count", 0))}">漏检 {em.get("missed_gt_count", 0)}</span>',
            f'<span class="metric-chip metric-chip-{_metric_chip_class("precision", em.get("precision"))}">精确率 {prec}</span>',
            f'<span class="metric-chip metric-chip-{_metric_chip_class("recall", em.get("recall"))}">召回率 {rec}</span>',
            f'<span class="metric-chip metric-chip-{_metric_chip_class("avg_fp_per_hour", fp_val)}">误检/h {fp_val:.2f}</span>',
        ])

        block = f'''
        <section class="event-case-block">
          <div class="event-case-head">
            <div class="event-case-title">
              <span class="event-case-kicker">事件类型</span>
              <h3>{etype}</h3>
            </div>
            <div class="mini-metrics metric-chip-list">
              {metric_chips}
            </div>
          </div>
          <div class="event-case-body">
            {fp_grid}
            {miss_grid}
            {empty_state}
          </div>
        </section>
        '''
        event_blocks.append(block)

    event_html = ''
    if _has('events') and event_blocks:
        event_html = f'''
    <div class="page event-analysis-page">
      <div class="section-intro">
        <h2>详细案例分析</h2>
        <p class="section-intro-text">按事件类型集中展示关键误检与漏检样本，方便把指标和画面放在同一视线里分析。</p>
      </div>
      <div class="event-case-list">
        {''.join(event_blocks)}
      </div>
    </div>
    '''

    # 视频维度分析
    video_html = ''
    if _has('video'):
        video_rows = ''
        for vs in video_stats:
            video_rows += f'''
            <tr>
              <td>{vs['video_id']}</td>
              <td>{vs['alert_count']}</td>
              <td>{vs['hit_count']}</td>
              <td>{vs['fp_count']}</td>
              <td>{vs['miss_count']}</td>
              <td>{vs.get('precision', 'N/A')}</td>
              <td>{vs.get('recall', 'N/A')}</td>
            </tr>'''
        video_html = f'''
        <div class="page">
          <h2>视频维度分析</h2>
          <table class="data-table">
            <thead>
              <tr><th>视频ID</th><th>告警数</th><th>命中</th><th>误检</th><th>漏检</th><th>精确率</th><th>召回率</th></tr>
            </thead>
            <tbody>{video_rows}</tbody>
          </table>
        </div>
        '''

    # 结论
    conclusion_html = ''
    if _has('conclusion'):
        conclusion_html = f'''
    <div class="page">
      <h2>结论与改进建议</h2>
      <div class="ai-section">
        <div class="ai-badge">AI 分析</div>
        <div class="ai-content">{conclusion_text or '<em>AI 分析暂不可用</em>'}</div>
      </div>
    </div>
    '''

    css = '''
    <style>
      * { box-sizing:border-box; }
      body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; margin:0; padding:0; color:#333; line-height:1.6; background:#f5f5f5; }
      .page { max-width:1100px; margin:20px auto; background:#fff; padding:40px; border-radius:8px; box-shadow:0 1px 3px rgba(0,0,0,0.1); page-break-after:always; }
      .page:last-child { page-break-after:auto; }
      h1 { font-size:2.2rem; margin:0 0 10px; color:#2c3e50; }
      h2 { font-size:1.5rem; margin:0 0 20px; padding-bottom:10px; border-bottom:2px solid #3498db; color:#2c3e50; }
      h3 { font-size:1.2rem; margin:25px 0 15px; color:#34495e; }
      h4 { font-size:1rem; margin:20px 0 10px; color:#555; }
      .cover { text-align:center; padding:80px 40px; }
      .cover-sub { font-size:1.2rem; color:#7f8c8d; margin-bottom:40px; }
      .cover-meta { text-align:left; display:inline-block; margin-top:30px; font-size:0.95rem; color:#555; }
      .cover-meta p { margin:8px 0; }
      .metrics-row { display:flex; gap:20px; margin:20px 0; }
      .metric-card { flex:1; text-align:center; padding:25px 15px; border-radius:8px; background:#f8f9fa; }
      .metric-card.good { background:#d4edda; }
      .metric-card.mid { background:#fff3cd; }
      .metric-card.bad { background:#f8d7da; }
      .metric-label { font-size:0.85rem; color:#666; margin-bottom:8px; }
      .metric-value { font-size:1.8rem; font-weight:700; color:#2c3e50; }
      .ai-section { margin:20px 0; padding:20px; background:#f0f7ff; border-radius:8px; border-left:4px solid #3498db; }
      .ai-badge { display:inline-block; font-size:0.75rem; padding:3px 10px; background:#3498db; color:#fff; border-radius:12px; margin-bottom:10px; }
      .ai-content { font-size:0.95rem; color:#444; white-space:pre-wrap; }
      .info-table { width:100%; border-collapse:collapse; margin:15px 0; }
      .info-table td { padding:10px 15px; border-bottom:1px solid #eee; }
      .info-table td:first-child { width:200px; color:#666; font-weight:500; }
      .data-table { width:100%; border-collapse:collapse; margin:15px 0; font-size:0.9rem; }
      .data-table th, .data-table td { padding:10px 12px; text-align:left; border-bottom:1px solid #eee; }
      .data-table th { background:#f8f9fa; font-weight:600; color:#555; }
      .data-table tr:hover { background:#fafafa; }
      .sample-grid { display:grid; grid-template-columns:repeat(2, 1fr); gap:15px; margin:15px 0; }
      .sample-cell { border-radius:6px; overflow:hidden; background:#f5f5f5; border:1px solid #e0e0e0; }
      .sample-cell img { width:100%; aspect-ratio:16/9; object-fit:contain; display:block; background:#f0f0f0; }
      .sample-cell .img-placeholder { width:100%; height:150px; display:flex; align-items:center; justify-content:center; color:#999; font-size:0.85rem; }
      .sample-caption { padding:8px 10px; font-size:0.8rem; color:#555; background:#fff; }
      .mini-metrics { display:flex; flex-wrap:wrap; gap:10px; margin:10px 0 20px; }
      .mini-metrics span { padding:5px 12px; border-radius:15px; font-size:0.82rem; }
      .section-intro { margin-bottom:18px; }
      .section-intro h2 { margin-bottom:8px; }
      .section-intro-text { margin:0; color:#667085; font-size:0.92rem; }
      .event-analysis-page { padding-top:36px; }
      .event-case-list { display:flex; flex-direction:column; gap:18px; }
      .event-case-block { padding:24px; border:1px solid #e5eaf1; border-radius:14px; background:linear-gradient(180deg, #fbfdff 0%, #f7f9fc 100%); break-inside:avoid; page-break-inside:avoid; }
      .event-case-head { display:flex; flex-direction:column; gap:14px; }
      .event-case-title { display:flex; align-items:flex-start; gap:14px; padding:0 0 14px; border-bottom:1px solid #e8eef5; }
      .event-case-kicker { flex-shrink:0; display:inline-flex; align-items:center; height:28px; padding:0 12px; border-radius:999px; background:#e9f2ff; color:#2667b4; font-size:0.8rem; font-weight:700; letter-spacing:0.02em; }
      .event-case-head h3 { margin:0; font-size:1.28rem; line-height:1.3; color:#1f2d3d; }
      .event-case-head .mini-metrics { margin:0; }
      .metric-chip-list { gap:12px; }
      .metric-chip { display:inline-flex; align-items:center; min-height:32px; padding:6px 12px; border-radius:999px; font-size:0.82rem; font-weight:600; border:1px solid transparent; }
      .metric-chip-neutral { background:#eef2f6; color:#516071; border-color:#e0e6ed; }
      .metric-chip-good { background:#e9f8ef; color:#166534; border-color:#b7e4c7; }
      .metric-chip-mid { background:#fff7e8; color:#b45309; border-color:#f3d19c; }
      .metric-chip-bad { background:#fdecec; color:#b42318; border-color:#efb4ae; }
      .metric-chip-gray { background:#f3f4f6; color:#667085; border-color:#e5e7eb; }
      .event-case-body { display:flex; flex-direction:column; gap:18px; margin-top:18px; }
      .event-sample-section { padding:18px; border:1px solid #e6ebf2; border-radius:12px; background:#fff; }
      .event-sample-section h4 { margin:0 0 10px; color:#344054; }
      .event-empty { padding:18px 20px; border:1px dashed #d6dde8; border-radius:10px; background:#fff; color:#667085; font-size:0.9rem; }
      .step-list { display:flex; flex-direction:column; gap:1rem; margin:15px 0; }
      .step-item { display:flex; gap:0.8rem; align-items:flex-start; }
      .step-num { flex-shrink:0; width:28px; height:28px; background:#3498db; color:#fff; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:0.85rem; font-weight:700; margin-top:2px; }
      .step-body { flex:1; font-size:0.9rem; color:#444; line-height:1.7; }
      .step-body strong { color:#2c3e50; font-size:0.95rem; }
      @media print { body { background:#fff; } .page { margin:0; box-shadow:none; border-radius:0; } }
    </style>
    '''

    body = cover_html + summary_html + env_html + method_html + png_html + event_html + video_html + conclusion_html
    return f'<!DOCTYPE html><html><head><meta charset="utf-8"><title>算法验证报告 - {task_name}</title>{css}</head><body>{body}</body></html>'


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


def generate_detailed_report(task_id, db, config=None):
    """生成详细的算法验证报告（自包含 HTML 字符串）。"""
    import os
    import json
    import base64
    from collections import defaultdict
    from datetime import datetime, timezone

    config = config or {}
    cursor = db.cursor()

    # ── 1. 加载任务 ──────────────────────────────────────────────────────────
    cursor.execute(
        'SELECT id, name, notes, dataset_id, alert_eval_set_id, eval_set_id, '
        'merge_interval_sec, event_start_sec, event_end_sec, event_interval_sec, '
        'trigger_rate, min_event_duration_sec, status, created_at, finalized, '
        'accuracy, recall, avg_fp_per_hour, event_metrics, confirmed_at '
        'FROM eval_tasks WHERE id = ?', (task_id,))
    task = dict(cursor.fetchone())

    # 从数据库读取已保存的整体指标
    accuracy = task.get('accuracy')
    recall = task.get('recall')
    avg_fp_per_hour = task.get('avg_fp_per_hour')

    # ── 2. 加载事件指标 ──────────────────────────────────────────────────────
    event_metrics = []
    if task.get('event_metrics'):
        try:
            event_metrics = json.loads(task['event_metrics'])
        except Exception:
            pass

    # ── 3. 加载合并告警和 GT 事件（带图片路径）─────────────────────────────────
    cursor.execute('''
        SELECT m.id, m.video_id, m.event_type, m.ts_start, m.ts_end,
               m.representative_image_id, m.image_ids,
               m.is_false_positive, m.manual_status,
               a.filename, a.file_path,
               o.timestamp_seconds
        FROM eval_merged_events m
        LEFT JOIN alert_images a ON a.id = m.representative_image_id
        LEFT JOIN ocr_results o ON o.alert_image_id = m.representative_image_id
        WHERE m.task_id = ?
        ORDER BY m.event_type, m.video_id, m.ts_start
    ''', (task_id,))
    merged_rows = [dict(r) for r in cursor.fetchall()]

    cursor.execute('''
        SELECT g.id, g.video_id, g.event_type, g.start_sec, g.end_sec,
               g.expected_count, g.confirmed_count, g.actual_count,
               g.mid_frame_id, g.mid_frame_path
        FROM eval_gt_events g
        WHERE g.task_id = ?
        ORDER BY g.event_type, g.video_id, g.start_sec
    ''', (task_id,))
    gt_rows = [dict(r) for r in cursor.fetchall()]

    # ── 4. 计算整体统计 ──────────────────────────────────────────────────────
    total_duration = 0
    cursor.execute('SELECT video_ids FROM eval_video_sets WHERE id = ?', (task['eval_set_id'],))
    eval_set = cursor.fetchone()
    if eval_set and eval_set['video_ids']:
        try:
            video_db_ids = json.loads(eval_set['video_ids'])
            if video_db_ids:
                placeholders = ','.join('?' for _ in video_db_ids)
                cursor.execute(f'SELECT SUM(duration) as total FROM videos WHERE id IN ({placeholders})', video_db_ids)
                total_duration = cursor.fetchone()['total'] or 0
        except Exception:
            pass

    gt_intervals = [(g['start_sec'], g['end_sec']) for g in gt_rows
                    if g.get('start_sec') is not None and g.get('end_sec') is not None]
    merged_gt_intervals = merge_intervals(gt_intervals)
    gt_coverage_seconds = sum(end - start for start, end in merged_gt_intervals)
    gt_coverage_rate = gt_coverage_seconds / total_duration if total_duration > 0 else 0.0
    expected_alert_total = sum(g.get('expected_count', 0) or 0 for g in gt_rows)
    gt_event_count = len(gt_rows)
    total_duration_hours = total_duration / 3600 if total_duration else 0

    # ── 5. 按事件类型收集误检/漏检样本 ───────────────────────────────────────
    em_by_type = {em['event_type']: em for em in event_metrics}
    all_event_types = sorted(set(em['event_type'] for em in event_metrics))

    event_detail_list = []
    for etype in all_event_types:
        em = em_by_type.get(etype, {})

        # 误检样本
        fp_items = []
        for m in merged_rows:
            if m['event_type'] != etype:
                continue
            status = get_effective_status(m)
            if status != 'false_positive':
                continue
            ts = m.get('ts_start') or m.get('timestamp_seconds') or 0
            fp_items.append({
                'video_id': m['video_id'],
                'ts_start': ts,
                'file_path': m.get('file_path'),
                'time_str': f"{int(ts//3600):02d}:{int((ts%3600)//60):02d}:{int(ts%60):02d}",
            })
        fp_samples = _select_sample_items(fp_items, 'video_id', 'ts_start', max_items=10)
        for s in fp_samples:
            s['b64'] = _img_to_base64(s['file_path'], max_width=400)

        # 漏检样本
        miss_items = []
        for g in gt_rows:
            if g['event_type'] != etype:
                continue
            if (g.get('actual_count') or 0) >= (g.get('confirmed_count') or 0):
                continue
            start_sec = g.get('start_sec') or 0
            miss_items.append({
                'video_id': g['video_id'],
                'start_sec': start_sec,
                'file_path': g.get('mid_frame_path'),
                'time_str': f"{int(start_sec//3600):02d}:{int((start_sec%3600)//60):02d}:{int(start_sec%60):02d}",
            })
        miss_samples = _select_sample_items(miss_items, 'video_id', 'start_sec', max_items=10)
        for s in miss_samples:
            s['b64'] = _img_to_base64(s['file_path'], max_width=400)

        event_detail_list.append({
            'event_type': etype,
            'metrics': em,
            'fp_samples': fp_samples,
            'miss_samples': miss_samples,
        })

    # ── 6. 视频维度统计 ──────────────────────────────────────────────────────
    video_stats_map = defaultdict(lambda: {'alert_count': 0, 'hit_count': 0, 'fp_count': 0, 'miss_count': 0})
    for m in merged_rows:
        vid = m['video_id']
        status = get_effective_status(m)
        video_stats_map[vid]['alert_count'] += 1
        if status == 'correct':
            video_stats_map[vid]['hit_count'] += 1
        elif status == 'false_positive':
            video_stats_map[vid]['fp_count'] += 1

    for g in gt_rows:
        vid = g['video_id']
        if (g.get('actual_count') or 0) < (g.get('confirmed_count') or 0):
            video_stats_map[vid]['miss_count'] += 1

    video_stats = []
    for vid, stats in sorted(video_stats_map.items()):
        total_alert = stats['alert_count']
        correct = stats['hit_count']
        precision = f"{correct/total_alert*100:.1f}%" if total_alert > 0 else 'N/A'
        video_stats.append({
            'video_id': vid,
            'alert_count': total_alert,
            'hit_count': correct,
            'fp_count': stats['fp_count'],
            'miss_count': stats['miss_count'],
            'precision': precision,
            'recall': '-',
        })

    # ── 7. 生成报告 PNG（复用已有函数）───────────────────────────────────────
    report_png_b64 = None
    try:
        buf = generate_report_image(
            task, event_metrics, accuracy, recall, avg_fp_per_hour,
            total_duration_hours=total_duration / 3600 if total_duration else 0,
            total_duration=total_duration,
            gt_event_count=gt_event_count,
            gt_coverage_seconds=gt_coverage_seconds,
            gt_coverage_rate=gt_coverage_rate,
            expected_alert_total=expected_alert_total,
        )
        report_png_b64 = 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode('ascii')
    except Exception:
        pass

    # ── 8. 摘要和结论 ────────────────────────────────────────────────────────
    # 优先使用 config 中用户确认的文本，否则调用 Claude API
    summary_text = config.get('summary_text', '')
    conclusion_text = config.get('conclusion_text', '')

    if not summary_text and not conclusion_text:
        api_key = os.environ.get('ANTHROPIC_AUTH_TOKEN')
        if api_key:
            metrics_json = json.dumps({
                'accuracy': accuracy,
                'recall': recall,
                'avg_fp_per_hour': avg_fp_per_hour,
                'total_duration_seconds': total_duration,
                'gt_event_count': gt_event_count,
                'event_metrics': [{k: v for k, v in em.items() if k != 'avg_fp_per_hour'} for em in event_metrics],
            }, ensure_ascii=False, indent=2)

            summary_prompt = f'''你是一位计算机视觉算法验证专家。请根据以下视频水印 OCR 算法的评测数据，生成一段简洁的中文测试摘要（200-300 字）。

要求：
1. 先给一句整体评价
2. 列出 2-3 个关键发现，指出表现最好和最差的事件类型
3. 指出最需要关注的问题
4. 语言简洁专业，适合放在正式报告中

数据：
{metrics_json}'''
            summary_text = _call_claude(summary_prompt, api_key) or ''

            full_data = {
                'event_metrics': event_metrics,
                'video_stats': video_stats,
                'total_fp': sum(em.get('false_positive_count', 0) for em in event_metrics),
                'total_miss': sum(em.get('missed_gt_count', 0) for em in event_metrics),
            }
            conclusion_prompt = f'''你是一位计算机视觉算法验证专家。请根据以下完整的算法评测数据，生成"结论与改进建议"章节（400-600 字）。

要求：
1. 总体评价算法在精确率、召回率、误检控制三个维度的表现
2. 按优先级列出 3-5 条具体、可操作的改进建议
3. 建议要具体到事件类型或视频维度的问题
4. 语言正式、专业，适合算法验证报告

数据：
{json.dumps(full_data, ensure_ascii=False, indent=2)}'''
            conclusion_text = _call_claude(conclusion_prompt, api_key) or ''

    # ── 9. 组装 HTML ─────────────────────────────────────────────────────────
    return _build_report_html(
        task, event_metrics, summary_text, conclusion_text,
        event_detail_list, video_stats,
        total_duration, gt_event_count, gt_coverage_seconds,
        gt_coverage_rate, expected_alert_total,
        report_png_b64,
        config,
    )


def compute_overall_avg_fp(event_metrics):
    """从事件级指标聚合整体平均误检数/小时

    用于从缓存的 event_metrics 中恢复 overall.avg_fp_per_hour，
    避免整体指标与事件级数据不一致。
    """
    avg_fp_values = [em.get('avg_fp_per_hour', 0) for em in event_metrics if em.get('avg_fp_per_hour') is not None]
    return round(sum(avg_fp_values) / len(avg_fp_values), 2) if avg_fp_values else 0


def compute_task_metrics(task_id, cursor, eval_set_id, get_all_event_types_fn=None):
    """计算评测任务的完整指标，返回 (accuracy, recall, avg_fp_per_hour, event_metrics_list, total_duration)。

    cursor: sqlite3.Cursor（兼容 Flask get_db() 和独立 connection）
    get_all_event_types_fn: 可选，用于获取配置文件中的事件类型列表的函数
    """
    import json

    # ── 检查是否为实时采集模式 ──────────────────────────────────────────────────
    cursor.execute('SELECT t.dataset_id, d.mode, t.duration_hours FROM eval_tasks t LEFT JOIN datasets d ON d.id = t.dataset_id WHERE t.id = ?', (task_id,))
    task_info = cursor.fetchone()
    is_realtime = task_info and task_info['mode'] == 'realtime'
    duration_hours = task_info['duration_hours'] if task_info else None

    # 整体精确率
    cursor.execute('SELECT is_false_positive, manual_status FROM eval_merged_events WHERE task_id=?', (task_id,))
    total = 0
    correct = 0
    fp_count = 0
    for row in cursor.fetchall():
        status = get_effective_status(row)
        if status == 'ignored':
            continue
        total += 1
        if status == 'correct':
            correct += 1
        elif status == 'false_positive':
            fp_count += 1
    accuracy = correct / total if total > 0 else None

    if is_realtime:
        # ── 实时模式：无 GT，使用 duration_hours 计算误检数/小时 ─────────────────
        recall = None
        total_duration = 0

        # 按事件类型计算精确率和误检数
        all_event_types = get_all_event_types_fn() if get_all_event_types_fn else []
        if not all_event_types:
            cursor.execute('''
                SELECT DISTINCT event_type FROM eval_merged_events WHERE task_id=?
            ''', (task_id,))
            all_event_types = [r['event_type'] for r in cursor.fetchall() if r['event_type']]

        event_metrics = []
        for etype in all_event_types:
            cursor.execute('SELECT is_false_positive, manual_status FROM eval_merged_events WHERE task_id=? AND event_type=?', (task_id, etype))
            alert_count = 0
            correct_pred_count = 0
            fp_count_et = 0
            for row in cursor.fetchall():
                status = get_effective_status(row)
                if status == 'ignored':
                    continue
                alert_count += 1
                if status == 'correct':
                    correct_pred_count += 1
                elif status == 'false_positive':
                    fp_count_et += 1

            precision = correct_pred_count / alert_count if alert_count > 0 else None
            avg_fp_et = round(fp_count_et / duration_hours, 2) if duration_hours else 0

            event_metrics.append({
                'event_type': etype,
                'alert_count': alert_count,
                'gt_count': 0,
                'correct_pred_count': correct_pred_count,
                'false_positive_count': fp_count_et,
                'hit_count': 0,
                'missed_gt_count': 0,
                'precision': precision,
                'recall': None,
                'avg_fp_per_hour': avg_fp_et
            })

        avg_fp_values = [em['avg_fp_per_hour'] for em in event_metrics if em['avg_fp_per_hour'] is not None]
        avg_fp_per_hour = round(sum(avg_fp_values) / len(avg_fp_values), 2) if avg_fp_values else 0

        return accuracy, recall, avg_fp_per_hour, event_metrics, total_duration

    # ── 以下为原有普通模式逻辑 ──────────────────────────────────────────────────

    # 整体召回率
    cursor.execute('SELECT confirmed_count, actual_count FROM eval_gt_events WHERE task_id=?', (task_id,))
    total_expected = 0
    total_actual = 0
    for ev in cursor.fetchall():
        confirmed = ev['confirmed_count'] or 0
        actual = ev['actual_count'] or 0
        if confirmed == 0:
            if actual > 0:
                total_expected += 1
                total_actual += min(actual, 1)
        else:
            total_expected += confirmed
            total_actual += min(actual, confirmed)
    recall = total_actual / total_expected if total_expected > 0 else None

    # 评测视频总时长
    total_duration = 0
    if eval_set_id:
        cursor.execute('SELECT video_ids FROM eval_video_sets WHERE id = ?', (eval_set_id,))
        eval_set = cursor.fetchone()
        if eval_set and eval_set['video_ids']:
            try:
                video_db_ids = json.loads(eval_set['video_ids'])
                if video_db_ids:
                    placeholders = ','.join('?' for _ in video_db_ids)
                    cursor.execute(f'SELECT SUM(duration) as total FROM videos WHERE id IN ({placeholders})', video_db_ids)
                    total_duration = cursor.fetchone()['total'] or 0
            except Exception:
                pass
    total_duration_hours = total_duration / 3600 if total_duration else 0

    # 平均误检/小时（整体）
    avg_fp_per_hour = round(fp_count / total_duration_hours, 2) if total_duration_hours else 0

    # 按事件类型计算指标
    all_event_types = get_all_event_types_fn() if get_all_event_types_fn else []
    if not all_event_types:
        cursor.execute('''
            SELECT DISTINCT event_type FROM eval_merged_events WHERE task_id=?
            UNION
            SELECT DISTINCT event_type FROM eval_gt_events WHERE task_id=?
        ''', (task_id, task_id))
        all_event_types = [r['event_type'] for r in cursor.fetchall() if r['event_type']]

    event_metrics = []
    for etype in all_event_types:
        cursor.execute('SELECT is_false_positive, manual_status FROM eval_merged_events WHERE task_id=? AND event_type=?', (task_id, etype))
        alert_count = 0
        correct_pred_count = 0
        fp_count_et = 0
        for row in cursor.fetchall():
            status = get_effective_status(row)
            if status == 'ignored':
                continue
            alert_count += 1
            if status == 'correct':
                correct_pred_count += 1
            elif status == 'false_positive':
                fp_count_et += 1

        cursor.execute('SELECT confirmed_count, actual_count FROM eval_gt_events WHERE task_id=? AND event_type=?', (task_id, etype))
        gt_count = 0
        hit_count = 0
        missed_gt_count = 0
        for ev in cursor.fetchall():
            confirmed = ev['confirmed_count'] or 0
            actual = ev['actual_count'] or 0
            if confirmed == 0:
                if actual > 0:
                    gt_count += 1
                    hit_count += min(actual, 1)
            else:
                gt_count += confirmed
                hit_count += min(actual, confirmed)
                if actual < confirmed:
                    missed_gt_count += 1

        precision = correct_pred_count / alert_count if alert_count > 0 else None
        event_recall = hit_count / gt_count if gt_count > 0 else None
        avg_fp_et = round(fp_count_et / total_duration_hours, 2) if total_duration_hours else 0

        event_metrics.append({
            'event_type': etype,
            'alert_count': alert_count,
            'gt_count': gt_count,
            'correct_pred_count': correct_pred_count,
            'false_positive_count': fp_count_et,
            'hit_count': hit_count,
            'missed_gt_count': missed_gt_count,
            'precision': precision,
            'recall': event_recall,
            'avg_fp_per_hour': avg_fp_et
        })

    # 整体召回率 = gt_count > 0 的事件类型召回率的算术平均
    recalls_with_gt = [em['recall'] for em in event_metrics if em['recall'] is not None and em['gt_count'] > 0]
    recall = sum(recalls_with_gt) / len(recalls_with_gt) if recalls_with_gt else None

    # 整体平均误检数/小时 = 各事件类型平均误检数/小时的算术平均
    avg_fp_per_hour = compute_overall_avg_fp(event_metrics)

    return accuracy, recall, avg_fp_per_hour, event_metrics, total_duration


def _call_claude_chat(messages, current_summary, current_conclusion, metrics_json, api_key, base_url=None):
    """根据对话上下文调用 Claude API 修改摘要和结论。

    api_key/base_url/model 优先用传入参数，缺失时回退到统一 API 配置（api_config_service）。
    """
    from app.services import api_config_service
    creds = api_config_service.get_claude_creds()
    if not api_key:
        api_key = creds.get('auth_token')
    if not base_url:
        base_url = creds.get('base_url')
    model = creds.get('model', 'claude-sonnet-5')
    if not api_key:
        return {'summary': current_summary, 'conclusion': current_conclusion}

    # 只保留 user 消息；assistant 消息中的内容和 system prompt 中的
    # current_summary/current_conclusion 完全重复，传给模型会造成混淆，
    # 导致模型直接复制已有内容而不做修改。
    claude_messages = []
    for msg in messages:
        if msg.get('role') == 'user':
            claude_messages.append({'role': 'user', 'content': msg['content']})

    # 最后一条必须是用户消息
    if not claude_messages or claude_messages[-1]['role'] != 'user':
        return {'summary': current_summary, 'conclusion': current_conclusion}

    system_prompt = f'''你是一位计算机视觉算法验证专家。用户正在编辑一份算法验证报告的测试摘要和结论章节。

当前版本（供参考，禁止直接复制）：
【测试摘要】
{current_summary}

【结论与改进建议】
{current_conclusion}

评测数据：
{metrics_json}

格式要求（重要）：
- 使用 HTML 标签输出，不要 Markdown
- 小标题用 <strong> 标签，如 <strong>整体评价：</strong>
- 列表用 <ul><li></li></ul>
- 换行用 <br>
- 只输出 HTML 片段，不要包裹 html/head/body

内容要求：
1. 根据用户的请求修改对应章节
2. 返回格式必须包含两个标记：
   【测试摘要】[修改后的摘要]
   【结论与改进建议】[修改后的结论]
3. 如果用户只要求修改其中一个，另一个保持不变
4. 用中文回答，语言简洁专业
5. 重要：不要直接复制"当前版本"中的内容。必须根据用户的具体要求做出实际修改。'''

    try:
        import anthropic
        kwargs = {'api_key': api_key}
        if base_url:
            kwargs['base_url'] = base_url
        client = anthropic.Anthropic(**kwargs)
        resp = client.messages.create(
            model=model,
            max_tokens=4096,
            system=system_prompt,
            messages=claude_messages,
        )
        text = resp.content[0].text if resp.content else ''

        # 解析返回文本
        summary = current_summary
        conclusion = current_conclusion

        if '【测试摘要】' in text:
            parts = text.split('【测试摘要】', 1)
            rest = parts[1]
            if '【结论与改进建议】' in rest:
                s_part, c_part = rest.split('【结论与改进建议】', 1)
                summary = s_part.strip()
                conclusion = c_part.strip()
            else:
                summary = rest.strip()
        elif '【执行摘要】' in text:
            parts = text.split('【执行摘要】', 1)
            rest = parts[1]
            if '【结论建议】' in rest:
                s_part, c_part = rest.split('【结论建议】', 1)
                summary = s_part.strip()
                conclusion = c_part.strip()
            else:
                summary = rest.strip()
        elif '【结论与改进建议】' in text:
            parts = text.split('【结论与改进建议】', 1)
            conclusion = parts[1].strip()
        elif '【结论建议】' in text:
            parts = text.split('【结论建议】', 1)
            conclusion = parts[1].strip()

        return {'summary': summary, 'conclusion': conclusion}
    except Exception:
        return {'summary': current_summary, 'conclusion': current_conclusion}
