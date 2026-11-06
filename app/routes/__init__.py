"""路由模块公共工具"""
import hashlib
import io
import os
import tempfile
from pathlib import Path

from flask import current_app, make_response, send_file


def send_file_with_cache(file_path, mimetype=None, as_attachment=False, download_name=None):
    """发送文件并启用 HTTP Range 请求和浏览器缓存"""
    response = send_file(
        file_path,
        mimetype=mimetype,
        as_attachment=as_attachment,
        download_name=download_name,
        conditional=True,
    )
    response.headers['Cache-Control'] = 'public, max-age=86400'
    return response


def _thumbnail_cache_path(file_path, max_width, max_height):
    """缩略图缓存路径：源文件 mtime/size 变化时自动换 key。"""
    src = Path(file_path)
    st = src.stat()
    key = hashlib.sha1(
        f'{src.resolve()}|{st.st_mtime_ns}|{st.st_size}|{max_width}x{max_height}'.encode()
    ).hexdigest()[:20]
    cache_dir = Path(current_app.config.get('THUMBNAILS_DIR', 'thumbnails')) / 'imgcache'
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f'{key}.jpg'


def _render_thumbnail(file_path, max_width, max_height):
    """解码原图并缩放为 RGB 图像（透明通道合成白底）。"""
    from PIL import Image, ImageOps

    with Image.open(file_path) as source:
        img = ImageOps.exif_transpose(source)
        img.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGBA')
            background = Image.new('RGB', img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[-1])
            img = background
        else:
            img = img.convert('RGB')
    return img


def _atomic_save_jpeg(img, cache_path):
    """先写临时文件再替换，避免并发读到半张图。"""
    fd, tmp_path = tempfile.mkstemp(dir=str(cache_path.parent), suffix='.tmp')
    os.close(fd)
    try:
        img.save(tmp_path, format='JPEG', quality=82, optimize=True)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, cache_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def send_image_with_thumbnail(file_path, max_width=None, max_height=None, mimetype=None):
    """发送图片，按最大宽高生成缩略图并落盘缓存。

    缩略图统一编码为 JPEG；未指定尺寸或 Pillow 不可用时返回原图。
    """
    if (not max_width and not max_height) or not file_path:
        return send_file_with_cache(file_path, mimetype=mimetype)
    try:
        from PIL import Image  # noqa: F401
    except Exception:
        return send_file_with_cache(file_path, mimetype=mimetype)

    # 限制尺寸，避免异常参数
    max_width = min(max_width or 2048, 2048)
    max_height = min(max_height or 2048, 2048)

    try:
        cache_path = _thumbnail_cache_path(file_path, max_width, max_height)
        if cache_path.exists():
            return send_file_with_cache(str(cache_path), mimetype='image/jpeg')

        img = _render_thumbnail(file_path, max_width, max_height)
        try:
            _atomic_save_jpeg(img, cache_path)
            return send_file_with_cache(str(cache_path), mimetype='image/jpeg')
        except OSError:
            # 缓存目录不可写时退回内存输出
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=82, optimize=True)
            buf.seek(0)
            response = make_response(send_file(buf, mimetype='image/jpeg'))
            response.headers['Cache-Control'] = 'public, max-age=86400'
            return response
    except Exception:
        return send_file_with_cache(file_path, mimetype=mimetype)
