"""videos 资源 v1 端点（sxs.txt 本轮子集）。

策略（对应 docs/rest-api-feasibility-and-test-plan.md「方案 A：不改旧视图、接受重复」）：
- 列表端点：委托旧视图函数取数据 + 内存分页。复用已测查询逻辑、不写新 SQL，
  规避未经验证的 SQL 风险。服务端 LIMIT/OFFSET 分页是后续优化，当前数据规模内存分页足够。
- CRUD / 二进制：wrap_old_view 委托——信封转换，二进制（send_file）原样透传。

旧→新映射（sxs.txt）：
  GET    /api/v1/videos                  ← /videos/api/all + /videos/api/search(?q=)
  POST   /api/v1/videos                  ← /videos/api/upload
  DELETE /api/v1/videos/<id>             ← /videos/api/<id>
  GET    /api/v1/videos/<id>/download    ← /videos/api/<id>/download（二进制透传）
  GET    /api/v1/videos/eval-sets       ← /videos/api/eval-sets
  POST   /api/v1/videos/eval-sets       ← /videos/api/eval-sets

本轮未做（下一轮）：GET /api/v1/videos/<id>（单视频，旧无对应 GET，需新查询）、
PATCH /api/v1/videos/<id>（合并 rename/video-id，新形状）、GET /api/v1/videos/watermarked
（复杂 JOIN+文件系统检查，重实现需谨慎）。
"""
from flask import request

from app.api.v1 import v1_bp
from app.api.v1.compat import paginate_old_list, wrap_old_view
from app.routes.videos import (
    create_eval_set,
    delete_video,
    download_video,
    list_all_videos,
    list_eval_sets,
    search_videos,
    upload_video,
)

# 预包装旧视图（CRUD/二进制），避免每次请求重复构造
_upload = wrap_old_view(upload_video)
_delete = wrap_old_view(delete_video)
_download = wrap_old_view(download_video)
_create_eval_set = wrap_old_view(create_eval_set)


@v1_bp.route("/videos", methods=["GET"])
def v1_list_videos():
    """视频列表，支持 ?q= 过滤，返回分页信封。"""
    q = request.args.get("q", "").strip()
    return paginate_old_list(lambda: search_videos() if q else list_all_videos())


@v1_bp.route("/videos", methods=["POST"])
def v1_upload_video():
    return _upload()


@v1_bp.route("/videos/<int:video_id>", methods=["DELETE"])
def v1_delete_video(video_id):
    return _delete(video_id)


@v1_bp.route("/videos/<int:video_id>/download", methods=["GET"])
def v1_download_video(video_id):
    return _download(video_id)


@v1_bp.route("/videos/eval-sets", methods=["GET"])
def v1_list_eval_sets():
    return paginate_old_list(list_eval_sets, list_key="sets")


@v1_bp.route("/videos/eval-sets", methods=["POST"])
def v1_create_eval_set():
    return _create_eval_set()
