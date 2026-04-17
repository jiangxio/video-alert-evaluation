#!/bin/bash
# 处理单个视频，添加文字水印

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

source "$PROJECT_ROOT/config.sh"

# 输入参数
INPUT_VIDEO="$1"

if [ -z "$INPUT_VIDEO" ] || [ ! -f "$INPUT_VIDEO" ]; then
    echo "错误: 请提供有效的视频文件路径"
    exit 1
fi

# 视频ID：优先使用第二个参数，否则从文件名提取
if [ -n "$2" ]; then
    VIDEO_ID="$2"
else
    FILENAME=$(basename "$INPUT_VIDEO")
    VIDEO_ID=$(echo "$FILENAME" | cut -d'-' -f1)
fi

# 输出路径：以视频ID命名，放在 OUTPUT_DIR 根目录
FILENAME=$(basename "$INPUT_VIDEO")
EXT="${FILENAME##*.}"
OUTPUT_VIDEO="$OUTPUT_DIR/${VIDEO_ID}.${EXT}"
mkdir -p "$OUTPUT_DIR"

# 用于 drawtext 的 ID：把冒号替换成横杠，避免 FFmpeg filter 语法错误
SAFE_VIDEO_ID="${VIDEO_ID//:/-}"

echo "处理视频: $INPUT_VIDEO"
echo "  视频ID: $VIDEO_ID"
echo "  输出到: $OUTPUT_VIDEO"

# 构建FFmpeg命令 - 简洁版本
# 左上角显示：ID + 时间，带半透明背景
# 使用 pts 时间戳（从0开始的播放时间）
# 输入/输出加 file: 前缀，防止文件名中的冒号被当成协议分隔符

ffmpeg -y -i "file:${INPUT_VIDEO}" \
    -vf "drawtext=
        fontfile='${FONT_FILE}':
        text='${SAFE_VIDEO_ID} | %{pts\:hms}':
        x=${WATERMARK_X}:
        y=${WATERMARK_Y}:
        fontsize=${FONT_SIZE}:
        fontcolor=${FONT_COLOR}:
        box=1:
        boxcolor='${BOX_COLOR}':
        boxborderw=${BOX_BORDER_WIDTH}" \
    -c:v "$VIDEO_CODEC" \
    -crf "$CRF" \
    -preset "$PRESET" \
    -c:a "$AUDIO_CODEC" \
    -hide_banner \
    -loglevel error \
    "file:${OUTPUT_VIDEO}"

echo "  完成: $OUTPUT_VIDEO"
