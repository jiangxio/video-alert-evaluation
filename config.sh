#!/bin/bash
# 视频水印配置文件

# 水印样式配置
FONT_FILE="/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"
FONT_SIZE=32
FONT_COLOR="white"
BOX_COLOR="black@0.6"
BOX_BORDER_WIDTH=12

# 水印位置
WATERMARK_X=20
WATERMARK_Y=20

# 二维码尺寸
QR_SIZE=140

# 输出目录：自动推导为项目根目录下的 output 文件夹
CONFIG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="$CONFIG_DIR/output"

# FFmpeg 编码参数
VIDEO_CODEC="libx264"
CRF=23
PRESET="medium"     # ultrafast, superfast, veryfast, faster, fast, medium, slow, slower, veryslow
AUDIO_CODEC="copy"

# 并行处理：兼容 Linux (nproc) 和 macOS (sysctl)
if command -v nproc &> /dev/null; then
    MAX_PARALLEL=$(nproc)
else
    MAX_PARALLEL=$(sysctl -n hw.ncpu 2>/dev/null || echo 4)
fi
