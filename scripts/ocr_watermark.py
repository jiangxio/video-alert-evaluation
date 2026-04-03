#!/usr/bin/env python3
"""
视频水印OCR识别脚本
从截图中识别左上角的文字水印，返回JSON格式结果

支持多种OCR引擎：
1. PaddleOCR (推荐，中文+英文识别效果好
2. EasyOCR
3. Tesseract
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


def preprocess_image(image_path, watermark_region=None):
    """
    图像预处理 - 增强水印区域对比度
    watermark_region: (x1, y1, x2, y2) - 左上角区域坐标
    """
    if not HAS_PIL:
        raise ImportError("需要安装Pillow: pip install Pillow")

    img = Image.open(image_path).convert('L')  # 转为灰度

    # 默认取左上角区域 (假设水印在左上角 300x150区域)
    if watermark_region is None:
        w, h = img.size
        crop_w = min(400, w)
        crop_h = min(150, h)
        watermark_region = (0, 0, crop_w, crop_h)

    # 裁剪水印区域
    img_cropped = img.crop(watermark_region)

    # 增强对比度
    enhancer = ImageEnhance.Contrast(img_cropped)
    img_enhanced = enhancer.enhance(2.0)

    # 反转（黑底白字转为白底黑字
    img_inverted = ImageOps.invert(img_enhanced)

    # 再次增强
    enhancer2 = ImageEnhance.Contrast(img_inverted)
    img_final = enhancer2.enhance(2.0)

    return img_final, watermark_region


def parse_watermark_text(text):
    """
    解析水印文本，提取视频ID和时间戳
    水印格式: "046 | 00:00:03.088"
    """
    result = {
        "raw_text": text.strip(),
        "video_id": None,
        "timestamp": None,
        "timestamp_seconds": None,
        "success": False
    }

    if not text:
        return result

    # 清理文本
    cleaned = re.sub(r'\s+', ' ', text.strip())

    # 尝试匹配 "ID | 时间" 格式
    # 支持多种分隔符: |, /, -, 或空格
    patterns = [
        r'(\d+)\s*[\|\-/\s]\s*(\d{1,2}:\d{2}:\d{2}[\.,]?\d*',
        r'(\d+).*?(\d{1,2}:\d{2}:\d{2})',
        r'ID[_\-]?(\d+).*?(\d{1,2}:\d{2}:\d{2}',
    ]

    video_id = None
    time_str = None

    for pattern in patterns:
        m = re.search(pattern, cleaned)
        if m:
            video_id = m.group(1)
            time_str = m.group(2)
            break

    # 如果没找到，尝试只提取数字ID
    if not video_id:
        id_match = re.search(r'(\d{2,3)', cleaned)
        if id_match:
            video_id = id_match.group(1)

    result["video_id"] = video_id
    result["timestamp"] = time_str

    # 解析时间戳为秒
    if time_str:
        try:
            # 处理 HH:MM:SS 或 HH:MM:SS.mmm
            parts = time_str.replace(',', '.')
            if '.' in parts:
                hms, ms = parts.split('.', 1)
            else:
                hms, ms = parts, '0'

            hms_parts = hms.split(':')
            while len(hms_parts) < 3:
                hms_parts = ['0'] + hms_parts

            h, m, s = map(int, hms_parts)
            total_seconds = h * 3600 + m * 60 + s + float(f"0.{ms}")
            result["timestamp_seconds"] = round(total_seconds, 3)
        except Exception:
            pass

    if video_id or time_str:
        result["success"] = True

    return result


def ocr_with_paddleocr(image):
    """使用PaddleOCR进行识别"""
    try:
        from paddleocr import PaddleOCR
        ocr = PaddleOCR(use_angle_cls=True, lang='ch', show_log=False)
        result = ocr.ocr(image, cls=True)

        if result and result[0]:
            texts = [line[1][0] for line in result[0]]
            return ' '.join(texts)
        return ""
    except ImportError:
        return None
    except Exception as e:
        print(f"PaddleOCR error: {e}", file=sys.stderr)
        return None


def ocr_with_easyocr(image):
    """使用EasyOCR进行识别"""
    try:
        import easyocr
        #reader = easyocr.Reader(['ch_sim', 'en'], gpu=False)
        reader = easyocr.Reader(
            ['ch_sim', 'en'], 
            model_storage_directory='/home/jx/.EasyOCR/model',  # 1. 指定模型存放的文件夹
            download_enabled=False)
        result = reader.readtext(image)
        texts = [item[1] for item in result]
        return ' '.join(texts)
    except ImportError:
        return None
    except Exception as e:
        print(f"EasyOCR error: {e}", file=sys.stderr)
        return None


def ocr_with_tesseract(image):
    """使用Tesseract进行识别"""
    try:
        import pytesseract
        # 配置为只识别数字和特定字符
        custom_config = r'--oem 3 --psm 6 -c tessedit_char_whitelist="0123456789:|. '
        text = pytesseract.image_to_string(image, config=custom_config)
        return text
    except ImportError:
        return None
    except Exception as e:
        print(f"Tesseract error: {e}", file=sys.stderr)
        return None


def main():
    parser = argparse.ArgumentParser(description='视频水印OCR识别')
    parser.add_argument('image_path', help='截图文件路径')
    parser.add_argument('--engine', choices=['auto', 'paddle', 'easyocr', 'tesseract'],
                       default='auto', help='OCR引擎 (默认: auto)')
    parser.add_argument('--region', nargs=4, type=int, metavar=('x1', 'y1', 'x2', 'y2'),
                       help='水印区域坐标 (默认: 自动左上角)')
    parser.add_argument('--dump-image', action='store_true',
                       help='保存预处理后的图片用于调试')

    args = parser.parse_args()

    if not Path(args.image_path).exists():
        print(json.dumps({"error": "文件不存在", "path": args.image_path}, ensure_ascii=False))
        sys.exit(1)

    # 预处理图像
    try:
        region = tuple(args.region) if args.region else None
        img_processed, crop_region = preprocess_image(args.image_path, region)

        if args.dump_image:
            debug_path = str(Path(args.image_path).parent / f"debug_{Path(args.image_path).name}")
            img_processed.save(debug_path)
            print(f"调试图片已保存: {debug_path}", file=sys.stderr)
    except Exception as e:
        print(json.dumps({"error": f"图像处理失败: {str(e)}"}, ensure_ascii=False))
        sys.exit(1)

    # 尝试OCR识别
    raw_text = ""
    engine_used = None

    engines = []
    if args.engine == 'auto':
        engines = [('paddle', ocr_with_paddleocr),
                  ('easyocr', ocr_with_easyocr),
                  ('tesseract', ocr_with_tesseract)]
    elif args.engine == 'paddle':
        engines = [('paddle', ocr_with_paddleocr)]
    elif args.engine == 'easyocr':
        engines = [('easyocr', ocr_with_easyocr)]
    elif args.engine == 'tesseract':
        engines = [('tesseract', ocr_with_tesseract)]

    for name, engine_func in engines:
        result = engine_func(img_processed)
        if result is not None:
            raw_text = result
            engine_used = name
            break

    # 解析结果
    parsed = parse_watermark_text(raw_text)

    # 构建输出
    output = {
        "image_path": args.image_path,
        "crop_region": {
            "x1": crop_region[0],
            "y1": crop_region[1],
            "x2": crop_region[2],
            "y2": crop_region[3]
        },
        "ocr_engine": engine_used,
        **parsed
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
