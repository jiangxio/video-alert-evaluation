"""视频水印服务 - 实时进度 + 任务取消

直接通过 ``subprocess.Popen`` 拉起 FFmpeg：
- 启用 ``-progress pipe:1`` 解析 ``out_time_us`` 得到真实进度。
- 在模块级登记 Popen 对象，``cancel_task(task_id)`` 通过 ``terminate()`` 终止进程
  （Linux/macOS 发送 SIGTERM，Windows 调 TerminateProcess，三平台通用）。
"""
import shutil
import subprocess
import sys
import threading
from pathlib import Path


# 复用 scripts/process_single.py 中的命令构建器与字体查找
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / 'scripts'
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
from process_single import build_ffmpeg_cmd, find_font, DEFAULT_CONFIG  # noqa: E402


# task_id -> Popen
_processes = {}
# 记录通过 cancel_task 显式取消的任务，用于在 add_watermark 中区分取消与失败
_cancelled_task_ids = set()
_processes_lock = threading.Lock()


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


def add_watermark(video_path, output_dir, video_id=None,
                  task_id=None, progress_callback=None):
    """给视频添加水印，支持实时进度回调和外部取消

    :param video_path: 输入视频路径
    :param output_dir: 输出目录
    :param video_id: 视频ID（用于水印文字与输出文件名），默认从文件名提取
    :param task_id: 用于 :func:`cancel_task` 反向找到 Popen，None 则不可取消
    :param progress_callback: ``callable(pct: int)``，pct ∈ [0, 99]
    :return: ``{'success': bool, 'cancelled': bool, 'stderr': str, 'output_path': str|None}``
    """
    video_path = Path(video_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not video_path.exists():
        return {'success': False, 'cancelled': False,
                'stderr': f'视频不存在: {video_path}', 'output_path': None}

    if video_id is None:
        video_id = video_path.stem.split('-')[0]
    video_id = str(video_id)

    font_file = find_font()
    if not font_file:
        return {'success': False, 'cancelled': False,
                'stderr': '找不到合适的字体文件', 'output_path': None}

    ext = video_path.suffix.lstrip('.') or 'mp4'
    output_path = output_dir / f"{video_id}.{ext}"

    duration = _probe_duration(video_path)
    cmd = build_ffmpeg_cmd(
        str(video_path), str(output_path), video_id,
        font_file=font_file, config=DEFAULT_CONFIG, progress_pipe=True,
    )

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        return {'success': False, 'cancelled': False,
                'stderr': 'ffmpeg 未安装或不在 PATH 中', 'output_path': None}
    except Exception as e:
        return {'success': False, 'cancelled': False,
                'stderr': str(e), 'output_path': None}

    if task_id is not None:
        with _processes_lock:
            _processes[task_id] = process

    # 后台线程消费 stderr，避免缓冲区写满阻塞 ffmpeg
    stderr_chunks = []

    def _drain_stderr():
        try:
            for line in process.stderr:
                stderr_chunks.append(line)
        except Exception:
            pass

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    last_pct = -1
    try:
        if process.stdout is not None:
            for line in process.stdout:
                if not (duration and progress_callback):
                    continue
                if not line.startswith('out_time_us='):
                    continue
                try:
                    us = int(line.split('=', 1)[1].strip())
                except ValueError:
                    continue
                try:
                    pct = int(us / 1_000_000 / duration * 100)
                except ZeroDivisionError:
                    continue
                pct = max(0, min(pct, 99))
                if pct != last_pct:
                    last_pct = pct
                    try:
                        progress_callback(pct)
                    except Exception:
                        # callback 异常不应中断转码
                        pass
        returncode = process.wait()
    finally:
        if task_id is not None:
            with _processes_lock:
                _processes.pop(task_id, None)
                cancelled = task_id in _cancelled_task_ids
                _cancelled_task_ids.discard(task_id)
        else:
            cancelled = False

    stderr_thread.join(timeout=2)
    stderr = ''.join(stderr_chunks)

    success = returncode == 0 and not cancelled
    return {
        'success': success,
        'cancelled': cancelled,
        'stderr': stderr,
        'returncode': returncode,
        'output_path': str(output_path) if success else None,
    }


def cancel_task(task_id):
    """请求终止指定任务的 FFmpeg 进程；返回是否成功发出终止信号"""
    with _processes_lock:
        process = _processes.get(task_id)
        if process is None or process.poll() is not None:
            return False
        _cancelled_task_ids.add(task_id)
    try:
        process.terminate()
        return True
    except Exception:
        with _processes_lock:
            _cancelled_task_ids.discard(task_id)
        return False
