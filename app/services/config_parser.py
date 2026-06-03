"""配置文件解析服务

解析 JSON/YAML/XML 配置文件，提取关键信息：
- 置信度阈值、NMS阈值、输入尺寸、类别列表等
"""

import json
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

# 算法配置中常见的键名映射
KEY_MAP = {
    "confidence_threshold": [
        "confidence", "conf", "conf_thresh", "confidence_threshold",
        "conf_threshold", "threshold", "score_thresh", "score_threshold",
    ],
    "nms_threshold": [
        "nms", "nms_thresh", "nms_threshold", "iou_threshold", "iou_thresh",
    ],
    "input_size": [
        "input_size", "input_shape", "img_size", "image_size", "input_dimension",
    ],
    "classes": [
        "classes", "categories", "class_names", "names", "labels", "category",
    ],
}

# 反向映射：小写键名 → 标准键名
_REVERSE_MAP = {}
for std_key, aliases in KEY_MAP.items():
    for alias in aliases:
        _REVERSE_MAP[alias.lower()] = std_key

MAX_RAW_LINES = 500


def parse_config(file_path):
    """解析配置文件，提取关键信息 + 原始内容

    Args:
        file_path: 配置文件路径（str 或 Path）

    Returns:
        dict with keys: format, summary, raw_content, parse_error
    """
    file_path = Path(file_path) if not isinstance(file_path, Path) else file_path

    if not file_path.exists():
        return {
            "format": "unknown",
            "summary": {},
            "raw_content": "",
            "parse_error": f"文件不存在: {file_path}",
        }

    suffix = file_path.suffix.lower()

    # 读取原始内容（截断到 MAX_RAW_LINES 行）
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return {
            "format": "unknown",
            "summary": {},
            "raw_content": "",
            "parse_error": f"无法读取文件: {e}",
        }

    raw_content = "".join(lines[:MAX_RAW_LINES])
    if len(lines) > MAX_RAW_LINES:
        raw_content += f"\n... (共 {len(lines)} 行，仅展示前 {MAX_RAW_LINES} 行)"

    # 按格式解析
    if suffix in (".json",):
        return _parse_json(raw_content, file_path)
    elif suffix in (".yaml", ".yml"):
        return _parse_yaml(raw_content, file_path)
    elif suffix in (".xml",):
        return _parse_xml(raw_content, file_path)
    else:
        # txt 等未知格式，只返回原始内容
        return {
            "format": "unknown",
            "summary": {},
            "raw_content": raw_content,
            "parse_error": None,
        }


def _parse_json(raw_content, file_path):
    try:
        data = json.loads(raw_content)
    except json.JSONDecodeError as e:
        return {
            "format": "json",
            "summary": {},
            "raw_content": raw_content,
            "parse_error": f"JSON 解析失败: {e}",
        }

    summary = _extract_known_keys(data)
    return {
        "format": "json",
        "summary": summary,
        "raw_content": raw_content,
        "parse_error": None,
    }


def _parse_yaml(raw_content, file_path):
    if not HAS_YAML:
        return {
            "format": "yaml",
            "summary": {},
            "raw_content": raw_content,
            "parse_error": "PyYAML 未安装，无法解析 YAML 文件",
        }

    try:
        data = yaml.safe_load(raw_content)
    except yaml.YAMLError as e:
        return {
            "format": "yaml",
            "summary": {},
            "raw_content": raw_content,
            "parse_error": f"YAML 解析失败: {e}",
        }

    if not isinstance(data, dict):
        return {
            "format": "yaml",
            "summary": {},
            "raw_content": raw_content,
            "parse_error": None,
        }

    summary = _extract_known_keys(data)
    return {
        "format": "yaml",
        "summary": summary,
        "raw_content": raw_content,
        "parse_error": None,
    }


def _parse_xml(raw_content, file_path):
    """简单键值提取：遍历 XML 元素，匹配已知键名"""
    try:
        root = ET.fromstring(raw_content)
    except ET.ParseError as e:
        return {
            "format": "xml",
            "summary": {},
            "raw_content": raw_content,
            "parse_error": f"XML 解析失败: {e}",
        }

    summary = {}
    _walk_xml(root, summary)
    return {
        "format": "xml",
        "summary": summary,
        "raw_content": raw_content,
        "parse_error": None,
    }


def _walk_xml(element, summary):
    """递归遍历 XML 元素，将标签名+文本值映射到标准键"""
    tag_lower = element.tag.lower()
    std_key = _REVERSE_MAP.get(tag_lower)

    if std_key and element.text and element.text.strip():
        text = element.text.strip()
        # 尝试数值转换
        try:
            val = float(text)
            if val == int(val):
                val = int(val)
            summary[std_key] = val
        except ValueError:
            # 尝试解析为列表（逗号分隔）
            if "," in text:
                summary[std_key] = [item.strip() for item in text.split(",")]
            else:
                summary[std_key] = text

    for child in element:
        _walk_xml(child, summary)


def _extract_known_keys(data):
    """从 dict 数据中提取已知键名对应的值"""
    summary = {}
    if not isinstance(data, dict):
        return summary

    # 第一层直接匹配
    for key, value in data.items():
        key_lower = str(key).lower()
        std_key = _REVERSE_MAP.get(key_lower)
        if std_key is not None:
            summary[std_key] = value

    # 如果第一层没找到，尝试进入嵌套 dict（最多2层）
    if not summary:
        for key, value in data.items():
            if isinstance(value, dict):
                for inner_key, inner_value in value.items():
                    inner_lower = str(inner_key).lower()
                    std_key = _REVERSE_MAP.get(inner_lower)
                    if std_key is not None:
                        summary[std_key] = inner_value

    # classes 特殊处理：如果是 dict（如 YOLO 的 {0: "rat", 1: "smoke"}），转为列表
    if "classes" in summary and isinstance(summary["classes"], dict):
        summary["classes"] = list(summary["classes"].values())

    # input_size 特殊处理：如果是整数（如 640），转为 [640, 640]
    if "input_size" in summary and isinstance(summary["input_size"], (int, float)):
        val = int(summary["input_size"])
        summary["input_size"] = [val, val]

    return summary