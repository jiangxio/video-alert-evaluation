#!/usr/bin/env python3
"""
批量处理所有视频并添加水印
替代 batch_process.sh，兼容 Linux/macOS/Windows
"""
import subprocess
import sys
from pathlib import Path

from process_single import add_watermark, _default_output_dir


def find_videos(project_root, output_dir):
    """查找项目下所有 mp4 文件，排除输出目录"""
    output_dir = Path(output_dir).resolve()
    videos = []
    for pattern in ('*.mp4', '*.MP4'):
        for p in Path(project_root).rglob(pattern):
            resolved = p.resolve()
            # 排除位于 output_dir 内的文件
            try:
                resolved.relative_to(output_dir)
                continue
            except ValueError:
                pass
            videos.append(resolved)
    videos.sort()
    return videos


def main():
    project_root = Path(__file__).parent.parent.resolve()
    output_dir = _default_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 40)
    print("   视频批量水印处理工具")
    print("=" * 40)
    print(f"  输出目录: {output_dir}")
    print()

    videos = find_videos(project_root, output_dir)
    total = len(videos)
    print(f"开始处理 {total} 个视频...")

    for count, video in enumerate(videos, start=1):
        # 计算输出路径（保持相对目录结构）
        try:
            rel_path = video.relative_to(project_root)
        except ValueError:
            rel_path = video.name
        output_video = output_dir / rel_path

        if output_video.exists():
            print(f"\n=== [{count}/{total}] 跳过 (已存在): {video.name} ===")
            continue

        # 确保子目录存在
        output_video.parent.mkdir(parents=True, exist_ok=True)

        print(f"\n=== [{count}/{total}] 处理: {video.name} ===")
        success = add_watermark(str(video), output_dir=str(output_video.parent))
        if not success:
            print(f"  处理失败: {video.name}", file=sys.stderr)

    print()
    print("=" * 40)
    print("   处理完成！")
    print(f"   输出目录: {output_dir}")
    print("=" * 40)
    print("  已处理文件列表:")
    for p in sorted(output_dir.rglob('*.mp4')):
        print(f"    {p}")


if __name__ == '__main__':
    main()
