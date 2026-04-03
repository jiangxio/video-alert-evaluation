#!/bin/bash
# 批量处理所有视频

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

source "$PROJECT_ROOT/config.sh"

# 查找所有视频文件
find_videos() {
    find "$PROJECT_ROOT" -type f \( -name "*.mp4" -o -name "*.MP4" \) \
        | grep -v "$OUTPUT_DIR" \
        | sort
}

# 串行处理所有视频
process_all() {
    local videos=($(find_videos))
    local total=${#videos[@]}
    local count=0

    echo "开始处理 $total 个视频..."

    for video in "${videos[@]}"; do
        count=$((count + 1))

        # 检查是否已处理
        RELATIVE_PATH=$(realpath --relative-to="$PROJECT_ROOT" "$video")
        OUTPUT_VIDEO="$OUTPUT_DIR/$RELATIVE_PATH"

        if [ -f "$OUTPUT_VIDEO" ]; then
            echo -e "\n=== [$count/$total] 跳过 (已存在): $(basename "$video") ==="
            continue
        fi

        echo -e "\n=== [$count/$total] 处理: $(basename "$video") ==="
        "$SCRIPT_DIR/process_single.sh" "$video"
    done
}

# 主函数
main() {
    mkdir -p "$OUTPUT_DIR"

    echo "===================================="
    echo "   视频批量水印处理工具"
    echo "===================================="
    echo "  输出目录: $OUTPUT_DIR"
    echo

    process_all

    echo
    echo "===================================="
    echo "   处理完成！"
    echo "   输出目录: $OUTPUT_DIR"
    echo "===================================="
    echo "  已处理文件列表:"
    find "$OUTPUT_DIR" -name "*.mp4" | sort
}

main
