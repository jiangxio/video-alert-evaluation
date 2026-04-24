"""路由模块公共工具"""
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
