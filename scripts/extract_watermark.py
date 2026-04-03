#!/usr/bin/env python3
"""
超简单的水印识别脚本 - 只需要Pillow
用法: python scripts/extract_watermark.py 截图.jpg

这个脚本利用"水印是我们自己加的"这一点，
不做OCR，而是：
1. 裁剪左上角区域
2. 保存下来给人工看/或你自己用OCR工具
3. 同时从文件名提取视频ID
"""

import argparse
import json
import re
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description='提取水印区域 + 从文件名解析ID')
    parser.add_argument('image', help='截图文件')
    parser.add_argument('--no-save', action='store_true', help='不保存裁剪图')

    args = parser.parse_args()

    img_path = Path(args.image)
    if not img_path.exists():
        print(json.dumps({"error": "文件不存在"}))
        sys.exit(1)

    result = {
        "image": str(img_path),
        "video_id": None,
        "watermark_region_saved": None,
        "status": "success"
    }

    # 从文件名提取视频ID
    filename = img_path.stem
    id_match = re.search(r'(\d{2,3})', filename)
    if id_match:
        result["video_id"] = id_match.group(1)

    # 裁剪并保存水印区域
    if not args.no_save:
        try:
            from PIL import Image
            img = Image.open(img_path)
            w, h = img.size
            crop = img.crop((0, 0, min(400, w), min(120, h)))
            out_path = img_path.parent / f"watermark_{img_path.name}"
            crop.save(out_path)
            result["watermark_region_saved"] = str(out_path)
        except ImportError:
            result["status"] = "Pillow not installed"
        except Exception as e:
            result["status"] = str(e)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
