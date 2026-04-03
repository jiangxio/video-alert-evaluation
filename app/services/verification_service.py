"""OCR和验证服务 - 调用现有脚本"""
import subprocess
import json
import sys
from pathlib import Path


def run_ocr(image_path):
    """调用ocr_easy.py进行OCR识别"""
    script_path = Path(__file__).parent.parent.parent / 'scripts' / 'ocr_easy.py'

    try:
        result = subprocess.run(
            [sys.executable, str(script_path), str(image_path)],
            capture_output=True,
            text=True,
            timeout=60
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
        else:
            return {'error': result.stderr or 'OCR failed'}
    except Exception as e:
        return {'error': str(e)}


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
