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
    # 把 | 或 l（L小写）或 I（i大写）替换成空格（OCR 容易把 | 认成 l 或 I）
    cleaned = re.sub(r'[|lI]', ' ', cleaned)
    # 把字母 O 替换成数字 0（OCR 容易把 0 认成 O）
    cleaned = re.sub(r'[Oo]', '0', cleaned)
    # 自动纠正：把时间戳里的冒号/点混用统一为 HH:MM:SS.sss
    # 支持 00:02:27.440 / 00.02.27.440 / 00:02.27.440 等变体
    cleaned = re.sub(r'(\d{2})[:.](\d{2})[:.](\d{2})[:.](\d{3})', r'\1:\2:\3.\4', cleaned)

    # 提取视频ID（恰好10位数字）
    id_match = re.search(r'\b(\d{10})\b', cleaned)
    if id_match:
        result["video_id"] = id_match.group(1)
        # 移除视频ID，防止其末尾数字被误识别为时间戳的一部分
        cleaned = cleaned.replace(id_match.group(1), '', 1)

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
