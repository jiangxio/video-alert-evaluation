#!/usr/bin/env python3
"""
用EasyOCR识别截图水印
用法: python scripts/ocr_easy.py <截图文件>
"""

import argparse
import json
import re
import sys
from pathlib import Path

try:
    from PIL import Image, ImageEnhance, ImageOps
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


_reader = None


def get_reader():
    """懒加载并复用 EasyOCR Reader（避免每次重复初始化模型）"""
    global _reader
    if _reader is None:
        import easyocr
        _reader = easyocr.Reader(['en'], gpu=False)
    return _reader


def preprocess_and_ocr(image_path, reader=None):
    """预处理 + EasyOCR识别"""
    if not HAS_PIL:
        raise ImportError("需要Pillow: pip install Pillow")

    img = Image.open(image_path).convert('L')
    w, h = img.size

    # 裁剪左上角（只裁剪水印部分，避免后面的字母干扰，10位ID + 时间戳约需550px）
    crop = img.crop((0, 0, min(540, w), min(50, h)))

    # 增强对比度
    enhancer = ImageEnhance.Contrast(crop)
    img_enhanced = enhancer.enhance(2.5)

    # 反色（黑底白字→白底黑字）
    img_inverted = ImageOps.invert(img_enhanced)

    # 放大提升小字符识别稳定性：水印字符较小，原始分辨率下 EasyOCR 易把
    # 时间戳误识（如 00:03 → 100.3），放大 2× 后置信度与格式正确率显著提升
    scale = 2
    img_inverted = img_inverted.resize(
        (img_inverted.width * scale, img_inverted.height * scale),
        Image.LANCZOS
    )

    # EasyOCR识别
    try:
        import numpy as np
        if reader is None:
            reader = get_reader()
        result = reader.readtext(np.array(img_inverted))
        texts = [item[1] for item in result]
        return ' '.join(texts)
    except Exception as e:
        raise RuntimeError(f"EasyOCR失败: {str(e)}")


def parse_watermark_text(text):
    """解析水印文本"""
    result = {
        "raw_ocr_text": text.strip(),
        "video_id": None,
        "timestamp": None,
        "timestamp_seconds": None,
        "success": False
    }

    if not text:
        return result

    cleaned = re.sub(r'\s+', ' ', text.strip())
    # 把字母 O 替换成数字 0（OCR 容易把 0 认成 O）
    cleaned = re.sub(r'[Oo]', '0', cleaned)

    # 先提取视频ID（恰好10位数字），避免时间戳正则误匹配视频ID中的数字
    id_match = re.search(r'\b(\d{10})\b', cleaned)
    if id_match:
        result["video_id"] = id_match.group(1)
        # 移除视频ID后再做时间戳纠正
        cleaned = cleaned.replace(id_match.group(1), '', 1)

    # 自动纠正：把时间戳里的冒号/点/3/2/空格 混用统一为 HH:MM:SS.sss
    # 支持任意冒号位置被误识别为 3/. 的变体，以及冒号完全消失的情况
    # 如 00:38:26.667 / 0039.30.667 / 00.41224.267 / 00:19.:11.333 / 00 38 26.667 等
    # 首段允许 2~3 位以容错 OCR 多识一位前导数字（如 00:03 被误识为 100.3），
    # 替换时只保留末两位，丢弃多余前导数字
    m = re.search(r'(\d{2,3})[:.,*32 ]*(\d{2})[:.,*32 ]*(\d{2})[:.,*32 ]*[.,](\d{3})', cleaned)
    if m:
        h = m.group(1)[-2:]
        cleaned = cleaned[:m.start()] + f"{h}:{m.group(2)}:{m.group(3)}.{m.group(4)}" + cleaned[m.end():]

    # 提取时间戳（固定格式 HH:MM:SS.sss，必须严格匹配）
    time_match = re.search(r'(\d{2}:\d{2}:\d{2}\.\d{3})', cleaned)
    if time_match:
        result["timestamp"] = time_match.group(1)
        try:
            t_str = time_match.group(1)
            time_part, ms = t_str.split('.', 1)
            h, m, s = map(int, time_part.split(':'))
            result["timestamp_seconds"] = round(h * 3600 + m * 60 + s + float(f"0.{ms}"), 3)
        except Exception:
            pass

    # 只有当时间戳严格匹配 HH:MM:SS.sss 格式时才算成功
    # 视频ID和时间戳都需要识别成功
    result["success"] = (result["video_id"] is not None and result["timestamp"] is not None)
    return result


def main():
    parser = argparse.ArgumentParser(description='EasyOCR水印识别')
    parser.add_argument('image', help='截图文件')
    args = parser.parse_args()

    if not Path(args.image).exists():
        print(json.dumps({"error": "文件不存在"}))
        sys.exit(1)

    ocr_text = preprocess_and_ocr(args.image)
    parsed = parse_watermark_text(ocr_text)

    output = {
        "image": str(args.image),
        **parsed
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
