"""旧 /xxx/api/* 端点的弃用标记 after_request 钩子。

命中旧 API 路径前缀即给响应加：
  Deprecation: true
  Link: </api/v1/<successor>>; rel="successor-version"

不影响 /api/v1/ 新命名空间（先排除），也不影响页面与二进制响应：
浏览器忽略未知 header；send_file 的 header 与 body 独立，Cache-Control
已是先例。after_request 在错误响应（413/500/404）时也会触发——这是期望
行为，弃用端点的错误也标 Deprecation。
"""
from flask import request

# 旧 API URL 前缀 → 新资源 successor（未命中的只加 Deprecation 不加 Link）
_LEGACY_PREFIXES = {
    "/videos/api/": "/api/v1/videos",
    "/alerts/api/": "/api/v1/alerts",
    "/evaluation/api/": "/api/v1/evaluation",
    "/auto-annotation/api/": "/api/v1/auto-annotation",
    "/streaming/api/": "/api/v1/streaming",
    "/algorithms/api/": "/api/v1/algorithms",
    "/assistant/api/": "/api/v1/assistant",
    "/api-config/api/": "/api/v1/config",
    "/review/api/": "/api/v1/review",
    "/extract/api/": "/api/v1/extract",
    # verification 蓝图无 url_prefix，端点挂在根：
    "/api/alerts/": "/api/v1/alerts",
    "/api/verification/": "/api/v1/alerts",
}


def register_deprecation(app):
    @app.after_request
    def mark_deprecated(response):
        path = request.path
        # 新命名空间不标记
        if path.startswith("/api/v1/"):
            return response
        for prefix, successor in _LEGACY_PREFIXES.items():
            if path.startswith(prefix):
                response.headers["Deprecation"] = "true"
                response.headers["Link"] = '<{}>; rel="successor-version"'.format(successor)
                break
        return response
