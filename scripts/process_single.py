#!/usr/bin/env python3
"""
跨平台视频水印处理脚本
替代 process_single.sh，兼容 Linux/macOS/Windows
"""
import argparse
import platform
import subprocess
import sys
from pathlib import Path


# 默认配置
DEFAULT_CONFIG = {
    'font_size': 32,
    'font_color': 'white',
    'box_color': 'black@0.6',
    'box_border_width': 12,
    'watermark_x': 20,
    'watermark_y': 20,
    'video_codec': 'libx264',
    'crf': 23,
    'preset': 'medium',
    'audio_codec': 'copy',
}


_FONT_CANDIDATES = {
    'Linux': [
        '/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    ],
    'Darwin': [
        '/System/Library/Fonts/Helvetica.ttc',
        '/Library/Fonts/Arial.ttf',
        '/System/Library/Fonts/Supplemental/Arial.ttf',
    ],
    'Windows': [
        'C:/Windows/Fonts/arial.ttf',
        'C:/Windows/Fonts/segoeui.ttf',
        'C:/Windows/Fonts/calibrib.ttf',
    ],
}


def _find_font():
    """根据操作系统查找可用的字体文件"""
    system = platform.system()
    for path in _FONT_CANDIDATES.get(system, []):
        if Path(path).exists():
            return path
    return None


def _project_root():
    """从脚本位置推导项目根目录"""
    return Path(__file__).parent.parent.resolve()


def _default_output_dir():
    return _project_root() / 'output'


def _to_ffmpeg_path(path):
    """转换为 FFmpeg 安全的路径字符串

    Windows 上直接使用绝对路径（file: 前缀可能与盘符冲突）；
    Linux/macOS 上使用 file: 前缀避免文件名中的冒号被解析为协议分隔符。
    """
    p = Path(path).resolve()
    if platform.system() == 'Windows':
        return str(p)
    return f"file:{p.as_posix()}"


def _build_ffmpeg_cmd(input_path, output_path, video_id, font_file, config):
    """构建 FFmpeg drawtext 命令"""
    safe_video_id = video_id.replace(':', '-')

    # pts:hms 中的冒号在 drawtext filter 中需要转义
    drawtext = (
        f"drawtext=fontfile='{font_file}':"
        f"text='{safe_video_id} | %{{pts\\:hms}}':"
        f"x={config['watermark_x']}:"
        f"y={config['watermark_y']}:"
        f"fontsize={config['font_size']}:"
        f"fontcolor={config['font_color']}:"
        f"box=1:"
        f"boxcolor='{config['box_color']}':"
        f"boxborderw={config['box_border_width']}"
    )

    return [
        'ffmpeg', '-y',
        '-i', _to_ffmpeg_path(input_path),
        '-vf', drawtext,
        '-c:v', config['video_codec'],
        '-crf', str(config['crf']),
        '-preset', config['preset'],
        '-c:a', config['audio_codec'],
        '-movflags', '+faststart',
        '-hide_banner',
        '-loglevel', 'error',
        _to_ffmpeg_path(output_path),
    ]


def add_watermark(input_video, output_dir=None, video_id=None):
    """给视频添加左上角文字水印，返回是否成功"""
    input_path = Path(input_video)
    if not input_path.exists():
        print(f"错误: 请提供有效的视频文件路径: {input_video}", file=sys.stderr)
        return False

    # 视频ID：优先使用参数，否则从文件名提取（与 shell cut -d'-' -f1 一致）
    if video_id is None:
        video_id = input_path.stem.split('-')[0]

    output_dir = Path(output_dir) if output_dir else _default_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)

    ext = input_path.suffix.lstrip('.') or 'mp4'
    output_path = output_dir / f"{video_id}.{ext}"

    font_file = _find_font()
    if not font_file:
        print("错误: 找不到合适的字体文件", file=sys.stderr)
        return False

    print(f"处理视频: {input_video}")
    print(f"  视频ID: {video_id}")
    print(f"  输出到: {output_path}")

    cmd = _build_ffmpeg_cmd(str(input_path), str(output_path), video_id, font_file, DEFAULT_CONFIG)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode == 0:
            print(f"  完成: {output_path}")
            return True
        print(f"错误: FFmpeg 退出码 {result.returncode}", file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        return False
    except subprocess.TimeoutExpired:
        print("错误: 处理超时", file=sys.stderr)
        return False
    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(description='给视频添加文字水印')
    parser.add_argument('input_video', help='输入视频文件路径')
    parser.add_argument('--output-dir', help='输出目录（默认: 项目根目录/output）')
    parser.add_argument('--video-id', help='视频ID（默认从文件名提取）')
    args = parser.parse_args()

    success = add_watermark(args.input_video, args.output_dir, args.video_id)
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
