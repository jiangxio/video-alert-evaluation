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

    # 裁剪左上角（只裁剪水印部分，避免后面的字母干扰）
    crop = img.crop((0, 0, min(450, w), min(40, h)))

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
    # 把字母 O 替换成数字 0（OCR 容易把 0 认成 O）
    cleaned = re.sub(r'[Oo]', '0', cleaned)
    # 把逗号归一化为点（OCR 容易把小数点 . 认成逗号 ,）
    cleaned = re.sub(r',', '.', cleaned)

    # ── 时间戳清洗（多阶段，按 OCR 常见误识别逐一修正） ──────────────────────
    # 阶段1: 合并连续标点（如 ".:" ":." ".."）
    cleaned = re.sub(r'[.:]{2,}', lambda m: ':' if ':' in m.group() else '.', cleaned)

    # 阶段2: 部分归一化后的残留（如 00.03:17.933 → 00:03:17.933）
    cleaned = re.sub(r'(\d{2})\.(\d{2}):(\d{2})\.(\d{3})', r'\1:\2:\3.\4', cleaned)

    # 阶段3: 标准全点/冒号分隔 → HH:MM:SS.sss
    cleaned = re.sub(r'(\d{2})[:.](\d{2})[:.](\d{2})[:.](\d{3})', r'\1:\2:\3.\4', cleaned)

    # 阶段4: 兜底A — 前段粘连 HHMM[噪声][.:]SS.ms
    #   00330.02.800  → HH=00 MM=30 SS=02 ms=800
    #   00331:21.200  → HH=00 MM=31 SS=21 ms=200
    m = re.search(r'\b(\d{4,5})[.:](\d{2})\.(\d{3})\b', cleaned)
    if m:
        first, ss, ms = m.group(1), m.group(2), m.group(3)
        hh = first[:2]
        mm = first[-2:]
        cleaned = re.sub(
            re.escape(m.group(0)),
            f'{hh}:{mm}:{ss}.{ms}',
            cleaned
        )

    # 阶段5: 兜底B — 中间数字粘连 HH.MMSS[噪声].ms
    #   00.01331.267 → HH=00 MM=01 SS=31 ms=267
    m = re.search(r'(\d{2})[.:](\d{3,5})[.:](\d{3})', cleaned)
    if m:
        hh, middle, ms = m.group(1), m.group(2), m.group(3)
        mm = middle[:2]
        ss = middle[-2:]
        cleaned = re.sub(
            re.escape(m.group(0)),
            f'{hh}:{mm}:{ss}.{ms}',
            cleaned
        )

    # 阶段6: 兜底C — 冒号被OCR识别为数字2或3（如 00331331.333 → 00:31:31.333, 00331222.133 → 00:31:22.133）
    m = re.search(r'\b(\d{2})[23](\d{2})[23](\d{2})\.(\d{3})\b', cleaned)
    if m:
        hh, mm, ss, ms = m.group(1), m.group(2), m.group(3), m.group(4)
        hh_int, mm_int, ss_int = int(hh), int(mm), int(ss)
        if hh_int < 24 and mm_int < 60 and ss_int < 60:
            cleaned = re.sub(
                re.escape(m.group(0)),
                f'{hh}:{mm}:{ss}.{ms}',
                cleaned
            )

    # 阶段7: 兜底D — 第一个冒号变空格、第二个冒号变点（如 00 34.50.133 → 00:34:50.133）
    m = re.search(r'\b(\d{2})\s+(\d{2})\.(\d{2})\.(\d{3})\b', cleaned)
    if m:
        hh, mm, ss, ms = m.group(1), m.group(2), m.group(3), m.group(4)
        hh_int, mm_int, ss_int = int(hh), int(mm), int(ss)
        if hh_int < 24 and mm_int < 60 and ss_int < 60:
            cleaned = re.sub(
                re.escape(m.group(0)),
                f'{hh}:{mm}:{ss}.{ms}',
                cleaned
            )

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
