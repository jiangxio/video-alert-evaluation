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


def preprocess_and_ocr(image_path):
    """预处理 + EasyOCR识别"""
    if not HAS_PIL:
        raise ImportError("需要Pillow: pip install Pillow")

    img = Image.open(image_path).convert('L')
    w, h = img.size

    # 裁剪左上角（宽度需覆盖完整水印文字，10位ID + 时间戳约需600px）
    crop = img.crop((0, 0, min(700, w), min(120, h)))

    # 增强对比度
    enhancer = ImageEnhance.Contrast(crop)
    img_enhanced = enhancer.enhance(2.5)

    # 反色（黑底白字→白底黑字）
    img_inverted = ImageOps.invert(img_enhanced)

    # EasyOCR识别
    try:
        import easyocr
        import numpy as np
        reader = easyocr.Reader(['en'], gpu=False)
        result = reader.readtext(np.array(img_inverted))
        texts = [item[1] for item in result]
        return ' '.join(texts)
    except Exception as e:
        print(json.dumps({"error": f"EasyOCR失败: {str(e)}"}))
        sys.exit(1)


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

    # 提取视频ID（恰好10位数字）
    id_match = re.search(r'\b(\d{10})\b', cleaned)
    if id_match:
        result["video_id"] = id_match.group(1)

    # 提取时间戳（支持冒号、点或空格作为分隔符）
    time_match = re.search(r'(\d{1,2}[:. ]\d{2}[:. ]\d{2}(?:[.,]\d+)?)', cleaned)
    if time_match:
        result["timestamp"] = time_match.group(1)
        try:
            t_str = time_match.group(1).replace(',', '.')
            # 把所有非数字/点的分隔符统一换成冒号
            t_str = re.sub(r'[^0-9.]', ':', t_str)
            # 确保有两个冒号分隔时分秒
            t_str = re.sub(r':+', ':', t_str)
            if '.' in t_str:
                hms_part, ms_part = t_str.split('.', 1)
                ms = ms_part
            else:
                hms_part, ms = t_str, '0'
            hms_parts = hms_part.split(':')
            # 去掉开头结尾的空字符串
            hms_parts = [p for p in hms_parts if p]
            while len(hms_parts) < 3:
                hms_parts = ['0'] + hms_parts
            h, m, s = map(int, hms_parts)
            result["timestamp_seconds"] = round(h * 3600 + m * 60 + s + float(f"0.{ms:<03}"), 3)
        except Exception:
            pass

    result["success"] = (result["video_id"] is not None or result["timestamp"] is not None)
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
