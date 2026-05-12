#!/usr/bin/env python3
"""
视频水印添加工具 - 跨平台主入口
替代 process.sh，兼容 Linux/macOS/Windows
"""
import argparse
import subprocess
import sys
from pathlib import Path


def show_help():
    print("""视频水印添加工具

用法:
  python process.py [选项]

选项:
  --single <视频文件>    仅处理单个视频
  --batch                批量处理所有视频（默认）
  --install              安装 Python 依赖
  --help                 显示此帮助信息

示例:
  python process.py --install
  python process.py --single video1/046-3.30-18:16.mp4
  python process.py --batch
""")


def install_deps():
    print("安装 Python 依赖...")
    req_file = Path(__file__).parent / 'requirements.txt'
    subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', str(req_file)], check=True)
    print("依赖安装完成！")


def process_single(video_file):
    script = Path(__file__).parent / 'scripts' / 'process_single.py'
    subprocess.run([sys.executable, str(script), str(video_file)], check=False)


def process_batch():
    script = Path(__file__).parent / 'scripts' / 'batch_process.py'
    subprocess.run([sys.executable, str(script)], check=False)


def main():
    parser = argparse.ArgumentParser(description='视频水印添加工具', add_help=False)
    parser.add_argument('--single', metavar='FILE', help='仅处理单个视频')
    parser.add_argument('--batch', action='store_true', help='批量处理所有视频')
    parser.add_argument('--install', action='store_true', help='安装 Python 依赖')
    parser.add_argument('--help', action='store_true', help='显示帮助信息')
    args = parser.parse_args()

    if args.help:
        show_help()
        return

    if args.install:
        install_deps()
        return

    if args.single:
        process_single(args.single)
        return

    # 默认批量处理
    process_batch()


if __name__ == '__main__':
    main()
