"""OCR和验证相关路由"""
from flask import Blueprint, request, jsonify
import json

from app.database import get_db
from app.services.verification_service import run_ocr, verify_alert

bp = Blueprint('verification', __name__)


@bp.route('/api/alerts/<int:alert_id>/ocr', methods=['POST'])
def ocr_alert(alert_id):
    """运行OCR识别"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, filename, file_path, alert_type_id, alert_type, file_size, uploaded_at, dataset_id, image_width, image_height, event_label FROM alert_images WHERE id = ?', (alert_id,))
    alert = cursor.fetchone()

    if not alert:
        return jsonify({'error': '告警图片不存在'}), 404

    result = run_ocr(alert['file_path'])

    if 'error' in result:
        return jsonify({'error': result['error']}), 500

    # 保存OCR结果
    cursor.execute('''
        INSERT INTO ocr_results
        (alert_image_id, raw_ocr_text, video_id, timestamp, timestamp_seconds, success, full_result)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (
        alert_id,
        result.get('raw_ocr_text', ''),
        result.get('video_id'),
        result.get('timestamp'),
        result.get('timestamp_seconds'),
        result.get('success', False),
        json.dumps(result, ensure_ascii=False)
    ))
    db.commit()

    return jsonify({
        'success': True,
        'ocr_result_id': cursor.lastrowid,
        'ocr_result': result
    })


@bp.route('/api/alerts/<int:alert_id>/verify', methods=['POST'])
def verify_alert_image(alert_id):
    """运行验证"""
    mock_ocr = request.json.get('mock_ocr') if request.is_json else None

    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, filename, file_path, alert_type_id, alert_type, file_size, uploaded_at, dataset_id, image_width, image_height, event_label FROM alert_images WHERE id = ?', (alert_id,))
    alert = cursor.fetchone()

    if not alert:
        return jsonify({'error': '告警图片不存在'}), 404

    result = verify_alert(alert['file_path'], mock_ocr)

    if 'error' in result:
        return jsonify({'error': result['error']}), 500

    # 保存OCR结果（如果有）
    ocr_result_id = None
    ocr_result = result.get('ocr_result')
    if ocr_result:
        cursor.execute('''
            INSERT INTO ocr_results
            (alert_image_id, raw_ocr_text, video_id, timestamp, timestamp_seconds, success, full_result)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            alert_id,
            ocr_result.get('raw_ocr_text', ''),
            ocr_result.get('video_id'),
            ocr_result.get('timestamp'),
            ocr_result.get('timestamp_seconds'),
            ocr_result.get('success', False),
            json.dumps(ocr_result, ensure_ascii=False)
        ))
        ocr_result_id = cursor.lastrowid

    # 保存验证结果
    matched_event = result.get('matched_event')
    cursor.execute('''
        INSERT INTO verification_results
        (alert_image_id, ocr_result_id, verdict, reason, ground_truth_file, matched_event)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (
        alert_id,
        ocr_result_id,
        result.get('verdict'),
        result.get('reason'),
        result.get('ground_truth_file'),
        json.dumps(matched_event, ensure_ascii=False) if matched_event else None
    ))
    db.commit()

    return jsonify({
        'success': True,
        'verification_result_id': cursor.lastrowid,
        'verification_result': result
    })


@bp.route('/api/alerts/<int:alert_id>/results', methods=['GET'])
def get_alert_results(alert_id):
    """获取告警图片的验证结果"""
    db = get_db()
    cursor = db.cursor()

    cursor.execute('SELECT id, alert_image_id, raw_ocr_text, video_id, timestamp, timestamp_seconds, success, full_result, created_at FROM ocr_results WHERE alert_image_id = ? ORDER BY created_at DESC', (alert_id,))
    ocrs = cursor.fetchall()

    cursor.execute('SELECT id, alert_image_id, ocr_result_id, verdict, reason, ground_truth_file, matched_event, created_at FROM verification_results WHERE alert_image_id = ? ORDER BY created_at DESC', (alert_id,))
    verifications = cursor.fetchall()

    return jsonify({
        'ocr_results': [dict(o) for o in ocrs],
        'verification_results': [dict(v) for v in verifications]
    })


@bp.route('/api/verification/batch', methods=['POST'])
def batch_verify():
    """批量验证（可选：使用mock数据）"""
    mock_ocr = request.json.get('mock_ocr') if request.is_json else None

    db = get_db()
    cursor = db.cursor()
    cursor.execute('SELECT id, filename, file_path, alert_type_id, alert_type, file_size, uploaded_at, dataset_id, image_width, image_height, event_label FROM alert_images ORDER BY uploaded_at')
    alerts = cursor.fetchall()

    results = []
    for alert in alerts:
        result = verify_alert(alert['file_path'], mock_ocr)
        results.append({
            'alert_id': alert['id'],
            'filename': alert['filename'],
            'result': result
        })

    return jsonify({'results': results})
