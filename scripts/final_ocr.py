#!/usr/bin/env python3
"""
最终版：只识别截图水印文字
依赖：Pillow + PaddleOCR
"""

import argparse
import json
import re
import sys
from pathlib import Path


def get_watermark_text(image_path):
    """
    获取水印文字 - 尝试多种OCR方式
    """
    # 先尝试预处理+PaddleOCR
    try:
        from PIL import Image, ImageEnhance, ImageOps
        img = Image.open(image_path).convert('L')
        w, h = img.size
        crop = img.crop((0, 0, min(700, w), min(120, h)))
        enhancer = ImageEnhance.Contrast(crop)
        img_enhanced = enhancer.enhance(2.5)
        img_inverted = ImageOps.invert(img_enhanced)
    except Exception:
        img_inverted = None

    # 尝试PaddleOCR
    text = None
    try:
        from paddleocr import PaddleOCR
        import numpy as np
        ocr = PaddleOCR(use_angle_cls=True, lang='en', show_log=False, use_gpu=False)
        if img_inverted:
            result = ocr.ocr(np.array(img_inverted), cls=True)
        else:
            result = ocr.ocr(image_path, cls=True)
        if result and result[0]:
            texts = [line[1][0] for line in result[0]]
            text = ' '.join(texts)
    except ImportError:
        pass
    except Exception:
        pass

    return text


def parse_text(text):
    """解析水印文本，返回video_id和timestamp"""
    result = {
        "raw_text": text.strip() if text else "",
        "video_id": None,
        "timestamp": None,
        "timestamp_seconds": None,
        "success": False
    }

    if not text:
        return result

    cleaned = re.sub(r'\s+', ' ', text.strip())

    # 提取视频ID（恰好10位数字）
    id_match = re.search(r'\b(\d{10})\b', cleaned)
    if id_match:
        result["video_id"] = id_match.group(1)

    # 提取时间戳
    time_match = re.search(r'(\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?)', cleaned)
    if time_match:
        result["timestamp"] = time_match.group(1)
        try:
            t_str = time_match.group(1).replace(',', '.')
            if '.' in t_str:
                hms, ms = t_str.split('.', 1)
            else:
                hms, ms = t_str, '0'
            hms_parts = hms.split(':')
            while len(hms_parts) < 3:
                hms_parts = ['0'] + hms_parts
            h, m, s = map(int, hms_parts)
            result["timestamp_seconds"] = round(h * 3600 + m * 60 + s + float(f"0.{ms:<03}"), 3)
        except Exception:
            pass

    result["success"] = (result["video_id"] is not None or result["timestamp"] is not None)
    return result


def main():
    parser = argparse.ArgumentParser(description='识别截图水印文字')
    parser.add_argument('image', help='截图文件')
    args = parser.parse_args()

    if not Path(args.image).exists():
        print(json.dumps({"error": "文件不存在"}))
        sys.exit(1)

    text = get_watermark_text(args.image)
    result = parse_text(text)

    # 包装成最终输出
    output = {
        "image": str(args.image),
        **result
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
