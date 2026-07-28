"""路由模块公共工具"""
import io

from flask import send_file, make_response


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


def _image_format_from_path(file_path, mimetype=None):
    """根据文件扩展名或 mimetype 判断输出格式。"""
    if mimetype:
        if 'png' in mimetype.lower():
            return 'PNG', 'image/png'
        if 'jpeg' in mimetype.lower() or 'jpg' in mimetype.lower():
            return 'JPEG', 'image/jpeg'
    ext = (file_path or '').lower().split('.')[-1] if '.' in (file_path or '') else ''
    if ext == 'png':
        return 'PNG', 'image/png'
    if ext in ('jpg', 'jpeg'):
        return 'JPEG', 'image/jpeg'
    return 'PNG', 'image/png'


def send_image_with_thumbnail(file_path, max_width=None, max_height=None, mimetype=None):
    """发送图片，支持按最大宽高生成缩略图。

    未指定尺寸或 Pillow 不可用时返回原图。
    """
    if (not max_width and not max_height) or not file_path:
        return send_file_with_cache(file_path, mimetype=mimetype)
    try:
        from PIL import Image
    except Exception:
        return send_file_with_cache(file_path, mimetype=mimetype)

    try:
        img = Image.open(file_path)
        size = (max_width or 99999, max_height or 99999)
        # 保持比例缩放到限定框内
        img.thumbnail(size, Image.Resampling.LANCZOS)
        fmt, out_mimetype = _image_format_from_path(file_path, mimetype)
        # JPEG 不支持透明通道
        if fmt == 'JPEG' and img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')
        buf = io.BytesIO()
        img.save(buf, format=fmt)
        buf.seek(0)
        response = make_response(send_file(buf, mimetype=out_mimetype))
        response.headers['Cache-Control'] = 'public, max-age=86400'
        return response
    except Exception:
        return send_file_with_cache(file_path, mimetype=mimetype)
