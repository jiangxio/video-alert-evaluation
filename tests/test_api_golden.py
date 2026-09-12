"""L5b 护栏：旧 /api/* GET 端点的 golden 快照回归。

目的：当后续接入 app/api 的全局 errorhandler / after_request 弃用钩子后，
立刻发现"全局钩子悄悄改坏了旧 API 响应（status/content_type/body）"的回归。
这是整个 v1 改造里最该先就位的护栏——先采基线，再动全局钩子。

基线语义：
- 本轮 v1 基础设施尚未接入 create_app，故现在采集的是"无全局钩子"的干净基线。
- 首次运行自动写入快照并 skip；之后严格比对，任何差异即失败。
- 若某端点天然非确定性（含时间戳/计数），把它从 CURATED_PATHS 移除即可。
- 若某端点在 fresh DB 上本就 500（缺文件等），快照会记录 500；建议从清单移除以免
  把"一直 500"误当基线掩盖真实回归。

重采基线（确认是有意变更后）：
    UPDATE_SNAPSHOTS=1 pytest tests/test_api_golden.py
"""
import pytest

from tests.snapshot import assert_unchanged

# 旧 GET 列表端点（fresh 空 DB 上确定性的，返回空列表/对象）。
# 取自各蓝图的 /api 列表路由，url_prefix 已拼全。
CURATED_PATHS = [
    "/videos/api/all",
    "/videos/api/watermarked",
    "/videos/api/eval-sets",
    "/evaluation/api/tasks",
    "/evaluation/api/eval-sets",
    "/evaluation/api/eval-sets/with-analysis-count",
    "/streaming/api/tasks",
    "/streaming/api/videos",
    "/streaming/api/video-sets",
    "/assistant/api/tasks",
    "/alerts/api/datasets",
    "/alerts/api/eval-sets",
    "/alerts/api/event-types",
    "/algorithms/api/types",
    "/algorithms/api/event-types",
    "/auto-annotation/api/tasks",
    "/extract/api/tasks",
]


@pytest.mark.parametrize("path", CURATED_PATHS)
def test_old_api_get_unchanged(app_client, path):
    seeded, actual, expected, snap_path = assert_unchanged(app_client, path)
    if seeded:
        pytest.skip(f"已采集基线快照（{snap_path.name}）；再次运行将以严格比对生效")
    msg = (
        f"\n旧端点 {path} 响应与基线不一致——全局 errorhandler/after_request "
        f"可能改坏了旧 API。\n  快照: {snap_path}\n"
        f"  expected: {expected}\n  actual:   {actual}\n"
        f"如为有意变更，用 UPDATE_SNAPSHOTS=1 重采。"
    )
    assert actual == expected, msg
