#!/bin/bash
# 视频水印添加工具 - 主入口

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 显示帮助
show_help() {
    cat <<EOF
视频水印添加工具

用法:
  ./process.sh [选项]

选项:
  --single <视频文件>    仅处理单个视频
  --batch                批量处理所有视频（默认）
  --install              安装Python依赖
  --help                 显示此帮助信息

示例:
  ./process.sh --install
  ./process.sh --single video1/046-3.30-18:16.mp4
  ./process.sh --batch
EOF
}

# 安装依赖
install_deps() {
    echo "安装Python依赖..."
    pip3 install -r "$SCRIPT_DIR/requirements.txt"
    echo "依赖安装完成！"
}

# 主逻辑
main() {
    if [ $# -eq 0 ]; then
        # 默认执行批量处理
        "$SCRIPT_DIR/scripts/batch_process.sh"
        return
    fi

    case "$1" in
        --single)
            if [ -z "$2" ]; then
                echo "错误: 请指定视频文件路径"
                show_help
                exit 1
            fi
            "$SCRIPT_DIR/scripts/process_single.sh" "$2"
            ;;
        --batch)
            "$SCRIPT_DIR/scripts/batch_process.sh"
            ;;
        --install)
            install_deps
            ;;
        --help)
            show_help
            ;;
        *)
            echo "错误: 未知选项 $1"
            show_help
            exit 1
            ;;
    esac
}

main "$@"
