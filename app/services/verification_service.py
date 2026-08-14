"""OCR和验证服务 - 调用现有脚本"""
import json
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
    """从文件名提取告警类型ID。

    标准格式优先严格匹配，兜底兼容历史命名：
    - 标准：{video_id}_{unix时间戳}_{alert_type_id}.ext  → 取第三段
    - 兜底1：{任意}_{id}.ext / {任意}-{id}.ext          → 取末尾数字段
    - 兜底2：{id}.ext                                    → 取扩展名前数字
    返回 ID 字符串或 None。注意：返回的 ID 未必在 alert_types.json 中登记，
    调用方应自行校验有效性（见 config.get(alert_type_id)）。
    """
    import re
    # 标准三段式：数字_数字_数字.ext
    m = re.search(r'^\d+_\d+_(\d+)\.[^.]+$', filename)
    if m:
        return m.group(1)
    # 兜底1：分隔符（横线/下划线）+ 数字 + 扩展名
    m = re.search(r'[_\-](\d+)\.[^.]+$', filename)
    if m:
        return m.group(1)
    # 兜底2：裸数字 + 扩展名
    m = re.search(r'(\d+)\.[^.]+$', filename)
    if m:
        return m.group(1)
    return None
