#!/usr/bin/env python3
"""
跨平台视频水印处理脚本
替代 process_single.sh，兼容 Linux/macOS/Windows
"""
import argparse
import json
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


# 默认配置
DEFAULT_CONFIG = {
    'font_size': 32,
    'font_color': 'white',
    'box_color': 'black',
    'box_border_width': 0,
    'watermark_x': 0,
    'watermark_y': 0,
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


def find_font():
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


def _probe_duration(video_path):
    """返回视频时长（秒），失败返回 None"""
    if not shutil.which('ffprobe'):
        return None
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error',
             '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1',
             str(video_path)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            value = result.stdout.strip()
            if value:
                return float(value)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass
    return None


def _has_audio_stream(video_path):
    """检测视频是否包含音频流"""
    if not shutil.which('ffprobe'):
        return False
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'a',
             '-show_entries', 'stream=codec_type',
             '-of', 'csv=p=0',
             str(video_path)],
            capture_output=True, text=True, timeout=10,
        )
        return 'audio' in result.stdout
    except Exception:
        return False


def _extract_middle_frame(video_path, output_frame):
    """提取视频中间帧"""
    duration = _probe_duration(video_path)
    if not duration:
        return False
    mid_time = duration / 2
    cmd = [
        'ffmpeg', '-y', '-i', _to_ffmpeg_path(video_path),
        '-ss', str(mid_time),
        '-vframes', '1', '-q:v', '2',
        '-loglevel', 'error',
        _to_ffmpeg_path(output_frame)
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return result.returncode == 0
    except Exception:
        return False


def _verify_ocr(output_path, expected_video_id, reader=None):
    """验证水印 OCR 可读性"""
    try:
        if str(Path(__file__).parent) not in sys.path:
            sys.path.insert(0, str(Path(__file__).parent))
        import ocr_easy
    except Exception as e:
        return 'failed', f"OCR 模块加载失败: {e}"

    with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
        tmp_frame = tmp.name

    try:
        if not _extract_middle_frame(output_path, tmp_frame):
            return 'failed', "无法提取中间帧进行验证"

        if reader is None:
            reader = ocr_easy.get_reader()
        ocr_text = ocr_easy.preprocess_and_ocr(tmp_frame, reader=reader)
        parsed = ocr_easy.parse_watermark_text(ocr_text)

        if not parsed.get('success'):
            return 'failed', f"OCR 无法识别水印: raw_text={parsed.get('raw_ocr_text', '')}"
        if parsed.get('video_id') != expected_video_id:
            return 'failed', (
                f"OCR 识别的 video_id 不匹配: "
                f"期望={expected_video_id}, 实际={parsed.get('video_id')}"
            )
        return 'passed', None
    finally:
        try:
            Path(tmp_frame).unlink(missing_ok=True)
        except Exception:
            pass


def build_ffmpeg_cmd(input_path, output_path, video_id,
                     *, font_file=None, config=None, progress_pipe=False,
                     tpad_duration=5):
    """构建 FFmpeg drawtext 命令

    progress_pipe=True 时追加 ``-progress pipe:1``，让 FFmpeg 把
    ``out_time_us``、``progress`` 等字段写到 stdout，调用方可解析实时进度。
    """
    if font_file is None:
        font_file = find_font()
    if config is None:
        config = DEFAULT_CONFIG

    safe_video_id = video_id.replace(':', '-')

    # pts:hms 中的冒号在 drawtext filter 中需要转义
    drawtext = (
        f"drawtext=fontfile='{font_file}':"
        f"text='{safe_video_id} %{{pts\\:hms}}':"
        f"x={config['watermark_x']}:"
        f"y={config['watermark_y']}:"
        f"fontsize={config['font_size']}:"
        f"fontcolor={config['font_color']}:"
        f"box=1:"
        f"boxcolor='{config['box_color']}':"
        f"boxborderw={config['box_border_width']}"
    )

    # tpad 在开头插入黑帧，然后 drawtext 叠加
    vf = f"tpad=start_duration={tpad_duration}:color=black,{drawtext}"

    has_audio = _has_audio_stream(input_path)

    cmd = [
        'ffmpeg', '-y',
        '-i', _to_ffmpeg_path(input_path),
        '-vf', vf,
        '-c:v', config['video_codec'],
        '-crf', str(config['crf']),
        '-preset', config['preset'],
        '-movflags', '+faststart',
        '-hide_banner',
        '-loglevel', 'error',
    ]

    if has_audio:
        cmd.extend(['-af', 'adelay=5000|5000', '-c:a', 'aac', '-b:a', '128k'])
    else:
        cmd.extend(['-an'])

    if progress_pipe:
        cmd.extend(['-progress', 'pipe:1'])

    cmd.append(_to_ffmpeg_path(output_path))
    return cmd


def add_watermark(input_video, output_dir=None, video_id=None, reader=None):
    """给视频添加左上角文字水印，返回结果字典"""
    input_path = Path(input_video)
    if not input_path.exists():
        print(f"错误: 请提供有效的视频文件路径: {input_video}", file=sys.stderr)
        return {'success': False, 'stderr': '视频不存在', 'ocr_check_status': None}

    # 视频ID：优先使用参数，否则从文件名提取（与 shell cut -d'-' -f1 一致）
    if video_id is None:
        video_id = input_path.stem.split('-')[0]

    output_dir = Path(output_dir) if output_dir else _default_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)

    ext = input_path.suffix.lstrip('.') or 'mp4'
    output_path = output_dir / f"{video_id}.{ext}"

    font_file = find_font()
    if not font_file:
        print("错误: 找不到合适的字体文件", file=sys.stderr)
        return {'success': False, 'stderr': '找不到字体', 'ocr_check_status': None}

    print(f"处理视频: {input_video}")
    print(f"  视频ID: {video_id}")
    print(f"  输出到: {output_path}")

    cmd = build_ffmpeg_cmd(str(input_path), str(output_path), video_id,
                           font_file=font_file, config=DEFAULT_CONFIG)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            print(f"错误: FFmpeg 退出码 {result.returncode}", file=sys.stderr)
            if result.stderr:
                print(result.stderr, file=sys.stderr)
            return {'success': False, 'stderr': result.stderr, 'ocr_check_status': None}
    except subprocess.TimeoutExpired:
        print("错误: 处理超时", file=sys.stderr)
        return {'success': False, 'stderr': '处理超时', 'ocr_check_status': None}
    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        return {'success': False, 'stderr': str(e), 'ocr_check_status': None}

    # 中间帧 OCR 验证
    print(f"  验证水印 OCR...")
    ocr_status, ocr_warning = _verify_ocr(output_path, video_id, reader=reader)
    if ocr_status != 'passed':
        print(f"  警告: 水印 OCR 验证失败 - {ocr_warning}", file=sys.stderr)

    print(f"  完成: {output_path}")
    return {
        'success': True,
        'stderr': ocr_warning or '',
        'ocr_check_status': ocr_status,
        'output_path': str(output_path),
    }


def main():
    parser = argparse.ArgumentParser(description='给视频添加文字水印')
    parser.add_argument('input_video', help='输入视频文件路径')
    parser.add_argument('--output-dir', help='输出目录（默认: 项目根目录/output）')
    parser.add_argument('--video-id', help='视频ID（默认从文件名提取）')
    args = parser.parse_args()

    result = add_watermark(args.input_video, args.output_dir, args.video_id)
    sys.exit(0 if result['success'] else 1)


if __name__ == '__main__':
    main()
