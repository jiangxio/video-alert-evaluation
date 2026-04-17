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

# 输出目录
OUTPUT_DIR="/data/41-benchmark/output"

# FFmpeg 编码参数
VIDEO_CODEC="libx264"
CRF=23
PRESET="medium"     # ultrafast, superfast, veryfast, faster, fast, medium, slow, slower, veryslow
AUDIO_CODEC="copy"

# 并行处理
MAX_PARALLEL=$(nproc)
