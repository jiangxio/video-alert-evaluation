#!/usr/bin/env python3
"""生成静态二维码图片（包含视频ID）"""

import qrcode
import sys
import os


def generate_qrcode(video_id, output_file, size=140):
    """生成二维码"""
    # 二维码内容：仅视频ID（时间戳通过文字显示）
    data = f'{{"id":"{video_id}"}}'

    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(data)
    qr.make(fit=True)

    img = qr.make_image(fill_color="white", back_color="black")
    img = img.resize((size, size))
    img.save(output_file)
    print(f"二维码已生成: {output_file}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: python generate_qrcode.py <video_id> <output_file> [size]")
        sys.exit(1)

    video_id = sys.argv[1]
    output_file = sys.argv[2]
    size = int(sys.argv[3]) if len(sys.argv) > 3 else 140

    os.makedirs(os.path.dirname(os.path.abspath(output_file)), exist_ok=True)
    generate_qrcode(video_id, output_file, size)
