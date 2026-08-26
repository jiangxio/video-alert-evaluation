"""/api/v1 REST API 命名空间：聚合各资源蓝图。

每新增一个资源模块，在此 import 并加入 BLUEPRINTS。
"""
from . import alerts, alerts_ocr, algorithms, assistant, auto_annotation, config, evaluation, event_types, extract, review, streaming, videos


BLUEPRINTS = [videos.bp, alerts.bp, alerts_ocr.bp, algorithms.bp, assistant.bp, auto_annotation.bp,
              config.bp, evaluation.bp, event_types.bp, extract.bp, review.bp, streaming.bp]
