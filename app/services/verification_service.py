"""OCR和验证服务 - 调用现有脚本"""
import json
import subprocess
import sys
from pathlib import Path

# 将 scripts 目录加入 Python 路径，以便直接导入 ocr_easy
_SCRIPTS_DIR = Path(__file__).parent.parent.parent / 'scripts'
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import ocr_easy

# 预加载并复用 EasyOCR Reader（避免每次识别都重新初始化模型）
_ocr_reader = None

def _get_ocr_reader():
    global _ocr_reader
    if _ocr_reader is None:
        _ocr_reader = ocr_easy.get_reader()
    return _ocr_reader


def run_ocr(image_path):
    """直接调用 ocr_easy 模块进行 OCR 识别（复用 Reader）"""
    try:
        reader = _get_ocr_reader()
        ocr_text = ocr_easy.preprocess_and_ocr(str(image_path), reader=reader)
        parsed = ocr_easy.parse_watermark_text(ocr_text)
        return {
            "image": str(image_path),
            **parsed
        }
    except Exception as e:
        return {'error': str(e)}


def ocr_and_save(conn, image_id, image_path):
    """对单张告警图执行 OCR 并写入 ocr_results 表。

    复用 _get_ocr_reader；成功（无 error）才写表并 commit，失败返回 (ocr_result, None)。
    conn 由调用方传入——请求上下文传 get_db()，后台线程传独立 sqlite3 连接，
    故 helper 不绑死连接来源，兼容线程（不踩 get_db 跨线程坑）。

    Returns: (ocr_result: dict, ocr_result_id: int|None)
    """
    ocr_result = run_ocr(image_path)
    ocr_result_id = None
    if 'error' not in ocr_result:
        cur = conn.cursor()
        cur.execute('''INSERT INTO ocr_results
            (alert_image_id, raw_ocr_text, video_id, timestamp, timestamp_seconds, success, full_result)
            VALUES (?, ?, ?, ?, ?, ?, ?)''', (
            image_id,
            ocr_result.get('raw_ocr_text'),
            ocr_result.get('video_id'),
            ocr_result.get('timestamp'),
            ocr_result.get('timestamp_seconds'),
            ocr_result.get('success', False),
            json.dumps(ocr_result, ensure_ascii=False),
        ))
        conn.commit()
        ocr_result_id = cur.lastrowid
    return ocr_result, ocr_result_id


def verify_alert(image_path, mock_ocr=None):
    """调用verify_alert.py进行验证"""
    script_path = Path(__file__).parent.parent.parent / 'scripts' / 'verify_alert.py'

    cmd = [sys.executable, str(script_path), str(image_path), '--quiet']
    if mock_ocr:
        cmd.extend(['--mock-ocr', json.dumps(mock_ocr)])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
        else:
            return {'error': result.stderr or 'Verification failed'}
    except Exception as e:
        return {'error': str(e)}


def parse_alert_config(config_path):
    """解析告警配置文件"""
    config = {}
    config_path = Path(config_path)
    if not config_path.exists():
        return config
    with open(config_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                alert_id, alert_type = parts
                config[alert_id] = alert_type
    return config


def extract_alert_type_id(filename):
    """从文件名提取告警类型ID（支持所有图片后缀，横线或下划线分隔均可）"""
    import re
    # 支持格式：xxx-105.png 或 xxx_105.png，取最后一串数字作为类型ID
    match = re.search(r'[_\-](\d+)\.[^.]+$', filename)
    if match:
        return match.group(1)
    # 兜底：直接取后缀前的数字（如 "105.png"）
    match2 = re.search(r'(\d+)\.[^.]+$', filename)
    if match2:
        return match2.group(1)
    return None
