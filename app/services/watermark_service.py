"""视频水印服务 - 调用现有脚本"""
import subprocess
import sys
from pathlib import Path


def add_watermark(video_path, output_dir, video_id=None):
    """调用process_single.sh给视频添加水印"""
    script_path = Path(__file__).parent.parent.parent / 'scripts' / 'process_single.sh'

    cmd = [str(script_path), str(video_path)]
    if video_id:
        cmd.append(str(video_id))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600  # 10分钟超时
        )
        return {
            'success': result.returncode == 0,
            'stdout': result.stdout,
            'stderr': result.stderr
        }
    except subprocess.TimeoutExpired:
            return {'success': False, 'error': '处理超时'}
    except Exception as e:
        return {'success': False, 'error': str(e)}
