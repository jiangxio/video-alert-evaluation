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
        crop = img.crop((0, 0, min(450, w), min(40, h)))
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
        # 移除视频ID，防止其末尾数字被误识别为时间戳的一部分
        cleaned = cleaned.replace(id_match.group(1), '', 1)

    # 提取时间戳（固定格式 MM:SS.ss，只有分秒，无小时）
    time_match = re.search(r'(\d{1,2}:\d{2}(?:\.\d+)?)', cleaned)
    if time_match:
        result["timestamp"] = time_match.group(1)
        try:
            t_str = time_match.group(1).replace(',', '.')
            if '.' in t_str:
                ms_part, frac = t_str.split('.', 1)
                ms = frac
            else:
                ms_part, ms = t_str, '0'
            m, s = map(int, ms_part.split(':'))
            result["timestamp_seconds"] = round(m * 60 + s + float(f"0.{ms:<03}"), 3)
        except Exception:
            pass

    # 校验：时间必须小于1小时
    if result["timestamp_seconds"] is not None and result["timestamp_seconds"] >= 3600:
        result["timestamp"] = None
        result["timestamp_seconds"] = None
        result["error"] = "时间超出范围（应小于1小时），请手动标注"

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
