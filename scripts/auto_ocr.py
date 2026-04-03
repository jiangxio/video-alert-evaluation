#!/usr/bin/env python3
"""
自动化水印OCR识别脚本
用法: python scripts/auto_ocr.py <截图文件>
"""

import argparse
import json
import re
import sys
from pathlib import Path


def crop_watermark_region(image_path, output_path=None):
    """裁剪左上角水印区域"""
    try:
        from PIL import Image
    except ImportError:
        print(json.dumps({"error": "请先安装Pillow: pip install Pillow"}))
        sys.exit(1)

    img = Image.open(image_path)
    w, h = img.size

    # 裁剪左上角区域 - 根据水印大小调整
    crop_w = min(400, w)
    crop_h = min(120, h)
    region = img.crop((0, 0, crop_w, crop_h))

    if output_path:
        region.save(output_path)
        print(f"水印区域已保存: {output_path}", file=sys.stderr)

    return region


def parse_from_filename_or_text(image_path, ocr_text=None):
    """
    尝试从文件名或OCR文本解析
    如果没有OCR库，至少可以从文件名提取视频ID
    """
    result = {
        "video_id": None,
        "timestamp": None,
        "timestamp_seconds": None,
        "source": "filename",
        "success": False
    }

    # 尝试从文件名提取ID
    filename = Path(image_path).stem
    id_match = re.search(r'(\d{2,3})', filename)
    if id_match:
        result["video_id"] = id_match.group(1)
        result["success"] = True

    # 如果有OCR文本，也尝试解析
    if ocr_text:
        result["source"] = "ocr"
        time_match = re.search(r'(\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?)', ocr_text)
        if time_match:
            result["timestamp"] = time_match.group(1)
            try:
                t_str = time_match.group(1).replace(',', '.')
                if '.' in t_str:
                    hms, ms = t_str.split('.', 1)
                else:
                    hms, ms = t_str, '0'
                parts = hms.split(':')
                while len(parts) < 3:
                    parts = ['0'] + parts
                h, m, s = map(int, parts)
                result["timestamp_seconds"] = round(h * 3600 + m * 60 + s + float(f"0.{ms:<03}"), 3)
            except:
                pass

    return result


def ocr_with_tesseract(image):
    """尝试用tesseract"""
    try:
        import pytesseract
        return pytesseract.image_to_string(image)
    except Exception:
        return None


def ocr_with_easyocr(image):
    """尝试用easyocr"""
    try:
        import easyocr
        import numpy as np
        reader = easyocr.Reader(['en'], gpu=False)
        img_np = np.array(image)
        result = reader.readtext(img_np)
        return ' '.join([item[1] for item in result])
    except Exception:
        return None


def ocr_with_paddle(image):
    """尝试用paddleocr"""
    try:
        from paddleocr import PaddleOCR
        import numpy as np
        ocr = PaddleOCR(use_angle_cls=True, lang='en', show_log=False)
        img_np = np.array(image)
        result = ocr.ocr(img_np, cls=True)
        if result and result[0]:
            return ' '.join([line[1][0] for line in result[0]])
        return ""
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description='自动识别视频截图水印')
    parser.add_argument('image', help='截图文件路径')
    parser.add_argument('--dump-crop', action='store_true', help='保存裁剪的水印区域')

    args = parser.parse_args()

    if not Path(args.image).exists():
        print(json.dumps({"error": "文件不存在", "path": args.image}))
        sys.exit(1)

    # 裁剪水印区域
    crop_path = f"crop_{Path(args.image).name}" if args.dump_crop else None
    img_cropped = crop_watermark_region(args.image, crop_path)

    # 尝试各种OCR引擎
    ocr_text = None
    ocr_engine = None

    for name, func in [
        ("paddle", ocr_with_paddle),
        ("easyocr", ocr_with_easyocr),
        ("tesseract", ocr_with_tesseract),
    ]:
        text = func(img_cropped)
        if text and text.strip():
            ocr_text = text
            ocr_engine = name
            break

    # 解析结果
    parsed = parse_from_filename_or_text(args.image, ocr_text)

    output = {
        "image": str(args.image),
        "ocr_engine_used": ocr_engine,
        "raw_ocr_text": ocr_text,
        **parsed
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
