"""after_request 弃用钩子：命中旧 API 前缀即加 Deprecation + Link 头。

注册于 register_api(app)。只加 header、绝不动 body/status——保旧端点逐字节不变
（golden 护栏 snapshot.py 的 _signature 不比 headers，加头不会破坏快照，但若误改
body/status 会被抓住）。

前缀映射既是匹配规则也是 Link 来源：命中旧前缀 → Link 指向新功能域根。
Link 表达「successor-version」迁移意图，不承诺目标已落地（部分功能域 v1 尚未实现）。
"""
from flask import request

# 旧前缀 → 新功能域根。两类旧路径模式：
#   /<bp>/api/...  （有 url_prefix 的蓝图，如 /videos/api/all、/alerts/api/datasets）
#   /api/...       （verification 蓝图无 url_prefix，端点如 /api/alerts/<id>/ocr）
DEPRECATED_PREFIXES = {
    "/videos/api/": "/api/v1/videos",
    "/alerts/api/": "/api/v1/alerts",
    "/api/alerts/": "/api/v1/alerts",          # verification 蓝图: ocr/verify/results
    "/api/verification/": "/api/v1/alerts",    # verification 蓝图: batch-verify
    "/evaluation/api/": "/api/v1/evaluation",
    "/assistant/api/": "/api/v1/assistant",
    "/streaming/api/": "/api/v1/streaming",
    "/algorithms/api/": "/api/v1/algorithms",
    "/auto-annotation/api/": "/api/v1/auto-annotation",
    "/review/api/": "/api/v1/review",
    "/extract/api/": "/api/v1/extract",
    "/api-config/api/": "/api/v1/config",
}


def register_deprecation(app):
    """注册 after_request 钩子，给旧 API 响应加弃用头。"""

    @app.after_request
    def mark_deprecated(response):
        path = request.path
        for old_prefix, new_root in DEPRECATED_PREFIXES.items():
            if path.startswith(old_prefix):
                response.headers["Deprecation"] = "true"
                response.headers["Link"] = f'<{new_root}>; rel="successor-version"'
                break
        return response
