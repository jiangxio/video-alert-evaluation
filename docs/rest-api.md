# REST API 参考文档

> 本文件是 `/api/v1` REST API 的收口汇总。错误码完整规范见 [`docs/rest-api-error-codes.md`](./rest-api-error-codes.md)；各模块实现细节另见 `docs/rest-api-<module>.md` 子文档（见文末索引）。

## 1. 概述

项目把原先混在 11 个旧 Flask 蓝图里的**页面路由 + 内联 JSON API**，改造为独立的 `/api/v1/*` REST 命名空间。

**设计原则**：只改 URL 资源化 + 正确 HTTP 动词 + 统一响应信封 + 统一错误处理，**不改交互语义**。

**新旧并行**：旧端点（`app/routes/*.py`）保留运行，前端继续走旧 URL；新端点独立挂在 `/api/v1/` 下。旧端点的响应自动带弃用 header（见 §2.8）。

**不在本次范围内**：鉴权、JSON schema 校验。

## 2. 通用约定

### 2.1 基础 URL 与注册

- 所有端点前缀：`/api/v1`
- 注册入口：`app/api/__init__.py` 的 `register_api(app)`，在 `app/__init__.py:create_app()` 末尾调用一次，完成三件事：
  1. 注册 v1 蓝图 `v1_bp`（`app/api/v1/__init__.py`）+ 导入各资源模块（导入即注册路由）
  2. 注册 app 级错误处理器（§2.3）
  3. 注册旧端点弃用 after_request 钩子（§2.8）

### 2.2 统一响应信封

所有 JSON 端点统一走信封（定义于 `app/api/v1/responses.py`）：

| 构造器 | HTTP | 响应体 |
|--------|------|--------|
| `ok(data)` | 200 | `{"code":0,"message":"ok","data":<data>}` |
| `created(data, location)` | 201 | `{"code":0,"message":"created","data":<data>}`，可选 `Location` header |
| `accepted(location)` | 202 | `{"code":0,"message":"accepted","data":null}`，可选 `Location` header（异步任务已排队） |
| `no_content()` | 204 | 无 body |
| `ok(paginate(items,total,page,page_size))` | 200 | `data:{items,total,page,page_size,has_next}` |

二进制响应（文件下载、缩略图、ZIP）**不走信封**，直接 `send_file`/`send_file_with_cache`；其错误仍 `raise ApiError`/`return err()` 走统一错误信封。委托旧视图用 `wrap_old_view`（盲委托自动套信封）/`_extract`（peek 旧 status/body 再加工）/`paginate_old_list`（旧裸列表内存分页）；PATCH 白名单 `reject_unknown_fields`。

### 2.3 错误响应与错误码体系（方案3）

新端点内 `raise ApiError(code, message, errors=None, error_code=None)` 或 `return err(code, message, errors=None, error_code=None)` 抛出/返回错误，由 `app/api/v1/errors.py` 的 app 级 errorhandler 转为统一错误信封：

```json
{"code": <HTTP 状态码>, "message": "...", "error_code": "...?", "errors": [{"field":"...","reason":"..."}]?}
```

- `code` = **标准 HTTP 状态码**（成功 `0`；错误 `400`/`404`/`409`/`500` 等）。
- `error_code`（可选）= 业务码字符串（如 `"DATASET_NOT_FOUND"`、`"UNKNOWN_FIELD"`），同类 HTTP 状态需区分时传，不传则不出现。
- `errors`（可选）= 字段级错误列表 `[{"field","reason"}]`。

**分流机制**：errorhandler 按 `request.path.startswith("/api/v1/")` 判断——命中则返回统一错误信封；否则 `return e` 回退 Flask 默认行为（HTML），**不破坏旧端点与页面**。旧端点用 `return jsonify({'error':...}), <code>` 主动返回（不 raise、不 abort），故错误格式不受影响。errorhandler 仅注册 `404`/`500`/`ApiError`；405 等 HTTP 错误由端点主动 `return err()` 产生，未挂 handler 的走 Flask 默认。

错误码完整规范（含 PATCH 白名单、分页工具）见 [`rest-api-error-codes.md`](./rest-api-error-codes.md)。

> **已废弃（5 位 `H FF SS` 方案）**：本仓早期用 5 位业务码 + `ApiError(code, message, http_status)` 三参签名 + 每模块独立 `BLUEPRINTS` 蓝图 + `call_old_view` 委托。上游 `origin/main` 明确拒绝该方案（见 `app/api/v1/alerts_ocr.py` 注释），本仓已对齐方案3：`code` 即 HTTP 状态、单一共享 `v1_bp`（`from app.api.v1 import v1_bp`）、`wrap_old_view`/`_extract` 委托。本文 §3 各端点错误码列遗留的 5 位码仅作历史参考，以代码实际行为为准。

### 2.4 HTTP 动词

| 动词 | 语义 |
|------|------|
| POST | 创建资源 / 触发动作 |
| GET | 读取（单条或列表） |
| PATCH | 部分更新 |
| PUT | 整体替换 |
| DELETE | 删除（成功返回 **204**，无 body） |

### 2.5 URL 结构与 RPC 动作后缀

- 资源路径：`/api/v1/<功能域>/<资源>`
- RPC 风格动作用 `:action` 后缀（冒号前缀），如 `:execute`、`:ocr:batch`、`:start`、`:stop`、`:batch-delete`、`:convert-to-events`、`:preview`
- 子资源用名词，如 `/tasks/<id>/logs`、`/tasks/<id>/progress`

### 2.6 分页

列表端点统一支持（`app/api/v1/_helpers.py:parse_pagination`）：

| 参数 | 类型 | 默认 | 约束 |
|------|------|------|------|
| `page` | int | 1 | ≥ 1 |
| `page_size` | int | 20 | 1..100 |

实现为 SQL 层真分页（`LIMIT ? OFFSET ?` + 独立 `COUNT(*) FROM (base) _c` 子查询），非 fetchall 后切片。分页响应见 §2.2 `paginated`。

### 2.7 二进制响应

文件下载、缩略图、打包 ZIP 等端点直接 `send_file` 返回二进制流，**不套信封**；成功时无 `code/message` 包装。这些端点出错时仍 `raise ApiError` 走统一错误信封，前端需按 `Content-Type` 区分成功（二进制）与错误（JSON 信封）。

### 2.8 旧端点弃用标记

`app/api/v1/deprecation.py` 注册 app 级 `after_request` 钩子：请求路径命中旧 API 前缀时，给响应加两个 header——

```
Deprecation: true
Link: </api/v1/<successor>; rel="successor-version"
```

旧前缀 → 新资源 successor 映射：

| 旧前缀 | successor |
|--------|-----------|
| `/videos/api/` | `/api/v1/videos` |
| `/alerts/api/` | `/api/v1/alerts` |
| `/evaluation/api/` | `/api/v1/evaluation` |
| `/auto-annotation/api/` | `/api/v1/auto-annotation` |
| `/streaming/api/` | `/api/v1/streaming` |
| `/algorithms/api/` | `/api/v1/algorithms` |
| `/assistant/api/` | `/api/v1/assistant` |
| `/api-config/api/` | `/api/v1/config` |
| `/review/api/` | `/api/v1/review` |
| `/extract/api/` | `/api/v1/extract` |
| `/api/alerts/` | `/api/v1/alerts` |
| `/api/verification/` | `/api/v1/alerts` |

新命名空间 `/api/v1/` 不标记；页面与二进制响应不受影响（header 与 body 独立）。错误响应也会带弃用 header（期望行为）。

## 3. 资源端点参考

> 以下按功能域列出全部端点。每个端点标注：方法 + 路径、描述、请求参数、成功响应、错误码。

>
> **关于错误码**：全部 12 个模块现已统一使用 5 位新码（H FF SS，详见 [`rest-api-error-codes.md`](./rest-api-error-codes.md)）。videos/alerts 的旧 4 位码已按对齐表迁移到 5 位（如 1001→10100、2001→20120、3001→30140、4001→40180），下文端点参考中的错误码均为迁移后的 5 位值。
>
> **委托端点的请求体**：标注"旧视图自读"的端点委托旧视图处理，请求体字段由旧视图实际校验；v1 端点仅套信封 + 错误码映射，凡无法从本文件确认的字段标注"未明确"。
>
> **二进制端点**：标注"二进制，不走信封"的端点直接返回文件流（`send_file`），前端需按 `Content-Type` 区分成功（二进制）与错误（JSON 信封）。

### 3.1 videos（FF=01，蓝图 `api_v1_videos`）

> 原位重写。视频与评测视频集资源（FF=01）。

- **`GET /api/v1/videos`** — 视频列表，分页 + 搜索。
  - 参数：query `q`(str,默认"",按 filename/video_id 模糊)；`page`(1)/`page_size`(20)。
  - 成功：200 分页，items 含 `id,filename,video_id,has_watermark,watermarked?` 等。
- **`POST /api/v1/videos`** — 上传视频（multipart）。
  - 参数：form `video`(文件,必填)；`already_watermarked`("1"/"true"/"on" 视为真)。
  - 成功：201 `{id,filename,video_id,has_watermark,duration}` + Location。
  - 错误：`10100`(400) 无文件；`10101`(400) 文件名空；`10102`(400) 扩展名不支持；`10103`(400) 非法文件名；`30140`(409) 同名已存在。
- **`GET /api/v1/videos/watermarked`** — 水印视频列表（每视频取最新水印版）。
  - 参数：query `eval_set_id`(可选,按评测集过滤)；`q`；`page`/`page_size`。
  - 成功：200 分页，items 含 `video_db_id,video_id,event_types,has_ground_truth,ocr_check_status` 等。
- **`GET /api/v1/videos/{video_id}`** — 视频详情。
  - 成功：200 视频 dict。错误：`20120`(404) 不存在。
- **`DELETE /api/v1/videos/{video_id}`** — 删除视频及水印版本文件。
  - 成功：204。错误：`20120`(404)。
- **`PATCH /api/v1/videos/{video_id}`** — 部分更新（重命名 / 设 video_id，至少传一个）。
  - 参数：body JSON `filename`(str)；`video_id`(str,须 10 位数字)。
  - 成功：200 更新后视频 dict。
  - 错误：`20120`(404)；`10104`(400) filename 空；`10105`(400) 非法；`10106`(400) 扩展名不支持；`30141`(409) 文件名被占用；`40180`(500) 重命名失败；`10107`(400) video_id 空；`10108`(400) 非 10 位数字；`10109`(409) video_id 已使用；`10110`(400) 无可更新字段。
- **`GET /api/v1/videos/{video_id}/download`** — 下载 / 内联播放视频（二进制，不走信封）。
  - 参数：query `type`(默认 `original`,可选 `watermarked`)；`inline`("true" 则内联 video/mp4)。
  - 成功：200 二进制流。错误：`20120`(404)；`20121`(404) 无水印视频；`20122`(404) 文件不在磁盘。
- **`GET /api/v1/videos/eval-sets`** — 评测视频集列表（分页）。
  - 成功：200 分页，items 含 `id,name,notes,video_ids,created_at`。
- **`POST /api/v1/videos/eval-sets`** — 创建评测视频集。
  - 参数：body JSON `name`(必填)；`notes`(可选)；`video_ids`(默认 [])。
  - 成功：201 `{id,name,notes,video_ids}` + Location。错误：`10111`(400) name 空。
- **`PUT /api/v1/videos/eval-sets/{set_id}`** — 整体替换评测集元数据（含 video_ids 则替换成员）。
  - 参数：body JSON `name`(必填)；`notes`；`video_ids`(可选,存在则替换)。
  - 成功：200 `{id,name,notes,video_ids}`。错误：`10112`(400) name 空；`20123`(404) 不存在。
- **`DELETE /api/v1/videos/eval-sets/{set_id}`** — 删除评测视频集。
  - 成功：204。错误：`20123`(404)。

### 3.2 alerts（FF=02/03/05，蓝图 `api_v1_alerts`）

> 原位重写。含数据集（FF=02）、告警图片（FF=03）、告警评测集（FF=05）。

- **`GET /api/v1/alerts/datasets`** — 数据集列表（分页）。
  - 成功：200 分页，items 含 `id,name,mode,image_count,algorithm_versions` 等。
- **`POST /api/v1/alerts/datasets`** — 创建数据集。
  - 参数：body JSON `name`(必填)；`notes`；`mode`(默认 `normal`)；`algorithm_version_ids`(默认 [])。
  - 成功：201 数据集字段 + image_count + algorithm_versions + Location。错误：`10200`(400) name 空；`10201`(400) 算法版本校验失败。
- **`GET /api/v1/alerts/datasets/{dataset_id}`** — 数据集详情。错误：`20220`(404)。
- **`DELETE /api/v1/alerts/datasets/{dataset_id}`** — 删除数据集及全部图片（含磁盘文件）。成功：204。错误：`20220`(404)。
- **`PATCH /api/v1/alerts/datasets/{dataset_id}`** — 部分更新（仅 mode）。
  - 参数：body JSON `mode`(必填,`normal`/`realtime`)。成功：200。
  - 错误：`10210`(400) 未传 mode；`10202`(400) mode 非法；`20220`(404)。
- **`GET /api/v1/alerts/datasets/{dataset_id}/algorithm-versions`** — 启用的算法版本列表。成功：200 数组。错误：`20220`(404)。
- **`POST /api/v1/alerts/datasets/{dataset_id}/algorithm-versions`** — 设置启用算法版本集合（整体覆盖）。
  - 参数：body JSON `algorithm_version_ids`(必填)。成功：200 数组。错误：`20220`(404)；`10203`(400) 校验失败。
- **`GET /api/v1/alerts/datasets/{dataset_id}/images`** — 数据集内图片列表（分页,附最新 OCR）。
  - 成功：200 分页，items 含 `id,filename,event_label,ocr` 等。错误：`20220`(404)。
- **`POST /api/v1/alerts/datasets/{dataset_id}/images`** — 多文件上传图片（multipart）。
  - 参数：form `image`(文件,可多,必填至少一个)。成功：200 `{uploaded:[...],errors:[...]}`。错误：`20220`(404)；`10301`(400) 无文件。
- **`POST /api/v1/alerts/datasets/{dataset_id}/images:import`** — 从压缩包导入图片（multipart）。
  - 参数：form `file`(必填,zip/tar/tar.gz/tgz)。成功：200 `{imported,skipped,skipped_files}`。错误：`20220`(404)；`10302`(400) 无文件；`10303`(400) 格式不支持。

- **`POST /api/v1/alerts/datasets/{dataset_id}/images:batch-delete`** — 按条件批量删除图片。

  - 参数：body JSON `video_id`(可选)；`event_type`(可选,均缺省删全部)。成功：200 `{deleted_count}`。错误：`20220`(404)；`20221`(404) 无符合条件图片。
- **`GET /api/v1/alerts/datasets/{dataset_id}/images/logs`** — 图片操作日志（最近 50 条）。成功：200 数组。错误：`20220`(404)。
- **`GET /api/v1/alerts/datasets/{dataset_id}/download`** — 打包下载全部图片 zip（二进制，不走信封）。
  - 成功：200 zip 流。错误：`20220`(404)；`20222`(404) 数据集为空；`20223`(404) 无可下载文件；`40280`(500) 打包失败。
- **`GET /api/v1/alerts/images/{image_id}`** — 单张图片详情（含最新 OCR,展开 full_result）。成功：200。错误：`20320`(404)。
- **`GET /api/v1/alerts/images/{image_id}/file`** — 图片文件 / 缩略图（二进制，不走信封）。
  - 参数：query `w`(int,可选)；`h`(int,可选,有则生成缩略图)。成功：200 二进制。错误：`20320`(404)；`20321`(404) 文件不在磁盘。
- **`PATCH /api/v1/alerts/images/{image_id}`** — 部分更新（仅 event_label）。
  - 参数：body JSON `event_label`(必填)。成功：200 `{event_label}`。错误：`10310`(400) 未传；`10300`(400) 为空；`20320`(404)。
- **`DELETE /api/v1/alerts/images/{image_id}`** — 删除单张图片。成功：204。错误：`20320`(404)。
- **`GET /api/v1/alerts/eval-sets`** — 告警评测集列表（分页）。
  - 成功：200 分页，items 含 `id,name,dataset_ids,dataset_count,image_count,dataset_names`。
- **`POST /api/v1/alerts/eval-sets`** — 创建告警评测集。
  - 参数：body JSON `name`(必填)；`notes`；`dataset_ids`(默认 [])。成功：201 `{id,name,notes,dataset_ids}` + Location。错误：`10500`(400) name 空。
- **`GET /api/v1/alerts/eval-sets/{set_id}`** — 评测集详情。成功：200 `{id,name,notes,dataset_ids,created_at,dataset_count}`。错误：`20520`(404)。
- **`PATCH /api/v1/alerts/eval-sets/{set_id}`** — 部分更新（仅 name）。
  - 参数：body JSON `name`(必填)。成功：200。错误：`10510`(400) 未传；`10501`(400) 为空；`20520`(404)。
- **`DELETE /api/v1/alerts/eval-sets/{set_id}`** — 删除告警评测集。成功：204。错误：`20520`(404)。
- **`POST /api/v1/alerts/eval-sets/{set_id}/datasets:batch-add`** — 批量添加数据集成员（去重）。
  - 参数：body JSON `dataset_ids`(必填非空)。成功：200 `{added_count,dataset_ids}`。错误：`10502`(400) 为空；`20520`(404)。
- **`POST /api/v1/alerts/eval-sets/{set_id}/datasets:batch-remove`** — 批量移除数据集成员。
  - 参数：body JSON `dataset_ids`(必填非空)。成功：200 `{removed_count,dataset_ids}`。错误：`10503`(400) 为空；`20520`(404)。

### 3.3 alerts OCR（FF=02/03，蓝图 `api_v1_alerts_ocr`）

> 路由委托旧视图（`compat.call_old_view`），请求体由旧视图自读，新端点套信封 + 错误码映射。5 位新码。

- **`POST /api/v1/alerts/images/{image_id}/ocr`** — 单张同步 OCR（失败仍 200，`success:false`）。
  - 成功：200 `{success,ocr}`。错误：`20320`(404) 图片不存在。
- **`POST /api/v1/alerts/images/{image_id}/ocr:manual`** — 手动保存 OCR 结果。
  - 参数：body JSON（旧视图自读）`video_id`/`timestamp`/`timestamp_seconds`/`success`（必填性由旧视图校验,未明确）。成功：200 `{ocr}`。错误：`20320`(404)。

- **`POST /api/v1/alerts/datasets/{dataset_id}/ocr:batch`** — 启动批量 OCR（旧视图当场起 daemon 线程,200 非 202）。
  - 参数：body JSON（旧视图自读）`force_all`/`stop_on_failure`（必填性未明确）。成功：200 `{total}`。
  - 错误：`20220`(404) 数据集不存在；`10311`(400) 无需 OCR 的图片；`30340`(409) OCR 运行中。
- **`GET /api/v1/alerts/datasets/{dataset_id}/ocr-status`** — 查询批量 OCR 进度（无任务返回 200 空进度,修正旧版 404）。
  - 成功：200 进度 dict 或 `{total:0,done:0,running:false,...}` + message。错误：无（不 raise）。
- **`POST /api/v1/alerts/datasets/{dataset_id}/ocr-status:cancel`** — 中断批量 OCR（幂等）。成功：200 `{cancelled:true}`。错误：无。

### 3.4 algorithms（FF=06，蓝图 `api_v1_algorithms`）

> 原位重写非委托。算法版本资源。5 位新码。

- **`GET /api/v1/algorithms/types`** — 算法类型 key 列表（即事件类型 key）。成功：200 key 列表。
- **`GET /api/v1/algorithms/versions`** — 算法版本列表（分页,带启用数据集）。
  - 成功：200 分页，items 含 `id,algorithm_type,name,version_date,datasets` 等。
- **`POST /api/v1/algorithms/versions`** — 新增算法版本（multipart）。
  - 参数：form `algorithm_type`(必填)；`name`(必填)；`version_date`(必填)；`description`(可选)；`config_file`/`algorithm_file`(文件,可选)。
  - 成功：201 `{id}` + Location。错误：`10600`(400) 类型无效；`10601`(400) 名空；`10602`(400) 日期空。
- **`GET /api/v1/algorithms/versions/{version_id}`** — 版本详情（含数据集 + 配置解析）。
  - 成功：200 `{version,datasets,config_info}`。错误：`20600`(404)。
- **`PATCH /api/v1/algorithms/versions/{version_id}`** — 部分更新（multipart,存在性语义）。
  - 参数：form `algorithm_type`/`name`/`version_date`(非空才更新)；`description`(存在性检测:未传不动,传空清空)；`config_file`/`algorithm_file`(提供则覆盖)。
  - 成功：200 `{id}`。错误：`20600`(404)；`10600`(400) 类型无效；`10603`(400) 无要更新字段。
- **`DELETE /api/v1/algorithms/versions/{version_id}`** — 删除（有数据集引用拒绝）。成功：204。
  - 错误：`20600`(404)；`30600`(409) 有 N 个数据集使用,无法删除。
- **`GET /api/v1/algorithms/download`** — 下载配置 / 算法文件（二进制,不走信封,路径防穿越）。
  - 参数：query `path`(必填)。成功：200 二进制流。错误：`10604`(400) 缺 path；`10605`(400) 非法路径；`20601`(404) 文件不存在。
- **`POST /api/v1/algorithms/versions:batch-download`** — 批量下载打包 zip（二进制）。
  - 参数：body JSON `ids`(int 数组,必填非空)；`type`(可选,默认 `all`,config/algorithm/all)。成功：200 zip 流。
  - 错误：`10606`(400) ids 空；`10607`(400) 版本不存在；`20602`(404) 无可下载文件；`40600`(500) 打包失败。

### 3.5 event-types（FF=07，蓝图 `api_v1_event_types`）

> 原位重写。create/update/delete 后调 `_sync_alert_types_json()` 同步配置文件。5 位新码。

- **`GET /api/v1/event-types`** — 事件类型列表（分页,tags 已解析为数组）。
  - 成功：200 分页，items 含 `id,key,name,tags,sort_order` 等。
- **`POST /api/v1/event-types`** — 新增事件类型。
  - 参数：body JSON `key`(必填,字母/数字/下划线)；`name`(必填)；`description`(默认 "")；`bg_color`(默认 "#e0e0e0")；`fg_color`(默认 "#333333")；`tags`(默认 [])；`id`(可选,缺省自增)。
  - 成功：201 `{id,key}` + Location。错误：`10700`(400) key 空；`10701`(400) 中文名空；`10702`(400) key 非法字符；`10703`(400) tags 非数组；`10704`(400) id 非整数；`30700`(409) key 已存在；`30701`(409) id 已存在。
- **`PATCH /api/v1/event-types/{et_id}`** — 修改（不允许改 key）。
  - 参数：body JSON `name`/`description`/`bg_color`/`fg_color`/`sort_order`(int)/`tags`(array),仅更新出现的字段。成功：200 `{id}`。
  - 错误：`20700`(404)；`10705`(400) 字段格式错误；`10703`(400) tags 非数组；`10706`(400) 无要更新字段。
- **`GET /api/v1/event-types/{et_id}/references`** — 跨 5 表引用计数（algorithm_versions/events/auto_annotation_tasks/eval_merged_events/eval_gt_events）。
  - 成功：200 `{key,total,refs}`。错误：`20700`(404)。
- **`DELETE /api/v1/event-types/{et_id}`** — 删除（有引用拒绝）。成功：204。
  - 错误：`20700`(404)；`30702`(409) 仍有 N 处引用,无法删除。

### 3.6 config（FF=08，蓝图 `api_v1_config`）

> 原位重写复用 `api_config_service`。单例资源（api_config 表 CHECK id=1）。5 位新码。

- **`GET /api/v1/config`** — 获取 API Token 配置（密钥不返回,仅 `*_key_configured` 标记）。成功：200 配置对象。
- **`PATCH /api/v1/config`** — 保存配置（密钥写 .env,空串/缺失=不改；非敏感项写 DB）。
  - 参数：body JSON 部分更新,空体 `{}` 合法。成功：200 当前配置。错误：`40880`(500) 保存失败。
- **`POST /api/v1/config:test-connection`** — 测试 LLM 连通性（连接失败是结果非 HTTP 错误）。
  - 参数：body JSON `provider`(必填,`openai`/`claude`)。成功：200 `{ok,msg}`（失败仍 200,ok=false）。错误：`10800`(400) 未知 provider。

### 3.7 streaming/tasks（FF=09，蓝图 `api_v1_streaming`）

> 原位重写 + 函数级复用 `_start_task_internal`（不重写 `_play_video`）。start/stop 用 `:action` 后缀。5 位新码。

- **`GET /api/v1/streaming/videos`** — 已打水印视频列表（分页）。成功：200 分页，items 含 `id,filename,duration,video_id`。
- **`GET /api/v1/streaming/video-sets`** — 评测视频集列表（分页,用 video_count 替代 video_ids）。成功：200 分页，items 含 `id,name,video_count`。
- **`GET /api/v1/streaming/tasks`** — 推流任务列表（分页,先同步假死任务）。
  - 成功：200 分页，items 含任务全列 + `rtsp_urls`/`elapsed_seconds`/`estimated_end_ts`。
- **`POST /api/v1/streaming/tasks`** — 创建任务（不启动）。
  - 参数：body JSON `source_type`(必填,single/set)；`source_id`(必填)；`stream_name`(必填,字母/数字/连字符/下划线)；`loop_count`(默认 1)；`name`(默认 "推流-{stream_name}")。
  - 成功：201 `{id,rtsp_url,total_duration,suggested_algorithms}` + Location。
  - 错误：`10900`(400) source_type 无效/解析失败；`10901`(400) source_id 缺；`10902`(400) stream_name 空；`10903`(400) stream_name 非法字符。
- **`POST /api/v1/streaming/tasks/{task_id}:start`** — 启动推流（委托 `_start_task_internal`）。
  - 参数：body JSON `resume`(bool,默认 false)。成功：200 `{status,pid,rtsp_urls}`。
  - 错误：`20900`(404)；`30900`(409) 已运行中；`30903`(409) 不可启动；`10905`(400) 视频文件不存在；`10900`(400) 其他 400 启动错误；`40900`(500) 其他启动失败。
- **`POST /api/v1/streaming/tasks/{task_id}:stop`** — 停止推流（同步 terminate）。成功：200 `{status}`(stopped/failed)。
  - 错误：`20900`(404)；`30904`(409) 非运行中。
- **`GET /api/v1/streaming/tasks/{task_id}/logs`** — FFmpeg 日志（限 100KB）。成功：200 `{content,lines}`(无文件则空)。
  - 错误：`20900`(404)；`40901`(500) 读取失败。
- **`GET /api/v1/streaming/tasks/{task_id}/progress`** — 播放进度。成功：200 `{task_id,name,status,elapsed_seconds,videos,progress}` 等。
  - 错误：`20900`(404)；`10900`(400) 解析视频失败。
- **`PATCH /api/v1/streaming/tasks/{task_id}`** — 编辑任务（仅非运行中,全量重校验,重置为 created）。
  - 参数：body JSON `source_type`/`source_id`/`stream_name`/`loop_count`/`name`(同 create)。成功：200 `{id,status}`(status 恒 created)。
  - 错误：`20900`(404)；`30901`(409) 运行中无法编辑；`10900`/`10901`/`10902`/`10903`(400) 校验失败。
- **`DELETE /api/v1/streaming/tasks/{task_id}`** — 删除任务（运行中拒绝）+ 清续播临时文件。成功：204。
  - 错误：`20900`(404)；`30902`(409) 运行中需先停止。
- **`POST /api/v1/streaming/tasks:preview`** — 预览未创建任务信息（不校验 stream_name 字符）。
  - 参数：body JSON `source_type`(必填)；`source_id`(必填)；`stream_name`(可选,空则 rtsp_urls 空数组)；`loop_count`(默认 1)。
  - 成功：200 `{rtsp_urls,total_duration,suggested_algorithms,video_count}`。错误：`10904`(400) source_type/source_id 缺；`10900`(400) 解析失败。

### 3.8 auto-annotation（FF=10，蓝图 `api_v1_auto_annotation`）

> 4 端点委托（start/stop/status/convert）+ 6 原位重写。真分页。5 位新码。

- **`GET /api/v1/auto-annotation/videos-without-events`** — 有水印无事件的视频列表（分页）。
  - 成功：200 分页，items 含 `{id,video_db_id,filename,video_id,duration,thumbnail_path}`。
- **`POST /api/v1/auto-annotation/tasks`** — 创建并启动任务（委托旧 start_task）。
  - 参数：body JSON（旧视图自读）`video_db_id`(必填)；`event_types`(必填)；`frame_interval_sec`/`merge_interval_sec`/`api_key`/`base_url`/`model`/`request_interval_sec`(可选,必填性由旧视图校验)。
  - 成功：200 `{task_id,queued}`。错误（文案映射）：`11000`(400) 未选视频；`11001`(400) 抽帧间隔；`11002`(400) 合并间隔；`11003`(400) 至少选一个事件类型；`21021`(404) 视频不存在；`21022`(404) 尚无水印视频；`41080`(500) fallback。
- **`GET /api/v1/auto-annotation/tasks`** — 历史任务列表（分页）。成功：200 分页，items 含 `_TASK_COLS` 全列。
- **`GET /api/v1/auto-annotation/tasks/{task_id}/json`** — 读取 GT JSON 内容。成功：200 GT 对象。
  - 错误：`21020`(404) 任务不存在；`21023`(404) JSON 不存在；`41080`(500) 读取失败。
- **`GET /api/v1/auto-annotation/videos/{video_db_id}/tasks`** — 指定视频的已完成任务（分页）。成功：200 分页。
- **`DELETE /api/v1/auto-annotation/tasks/{task_id}`** — 删除任务及帧数据。成功：204。错误：`21020`(404)。
- **`POST /api/v1/auto-annotation/tasks/{task_id}:clear`** — 清空中间帧数据（幂等,不校验任务存在性）。成功：200 `{task_id}`。
- **`POST /api/v1/auto-annotation/tasks:stop`** — 中断当前运行中任务（委托旧 stop_task）。成功：200 `{task_id}`。
  - 错误：`31040`(409) 无运行中任务；`41080`(500) fallback。
- **`GET /api/v1/auto-annotation/status`** — 当前任务状态和排队信息（委托旧 get_status,恒 200）。成功：200 旧 body。
- **`POST /api/v1/auto-annotation/tasks/{task_id}:convert-to-events`** — JSON 转 DB events（委托旧 convert_to_events）。成功：200 `{event_count}`。
  - 错误（文案映射）：`21020`(404)；`31041`(409) 任务未完成；`21023`(404) JSON 不存在；`41080`(500) fallback。

### 3.9 extract（FF=11，蓝图 `api_v1_extract`）

> start/status 委托 + download/delete/list 重写。5 位新码。

- **`POST /api/v1/extract/tasks`** — 提交批量抽帧任务（委托旧 start_extract,起 daemon 线程）。
  - 参数：body JSON（旧视图自读）`wm_ids`(必填)；`target_width`/`interval_sec`/`include_normal`(可选)。
  - 成功：200 `{task_id,video_count}`。错误（文案映射）：`11100`(400) 缺 wm_ids；`11101`(400) 抽帧间隔须 >0；`11102`(400) 均不可抽帧；`21100`(404) 水印视频不存在；`41100`(500) fallback。
- **`GET /api/v1/extract/tasks/{task_id}/status`** — 查询抽帧进度（委托旧 extract_status）。成功：200 `{status,done,total,frame_count,video_count,output_dir,error}`。
  - 错误：`21101`(404) 任务不存在；`41100`(500) fallback。
- **`GET /api/v1/extract/tasks`** — 历史抽帧任务列表（真分页）。成功：200 分页，items 含 `{id,video_id,video_count,target_width,interval_sec,include_normal,status,frame_count,created_at}`。
- **`GET /api/v1/extract/tasks/{task_id}/download`** — 打包下载帧 zip（二进制,不走信封）。成功：200 zip 流。
  - 错误：`21101`(404) 任务不存在；`21102`(404) 帧目录不存在。
- **`DELETE /api/v1/extract/tasks/{task_id}`** — 删除抽帧任务及帧目录。成功：204。错误：`21101`(404)。

### 3.10 review（FF=14，蓝图 `api_v1_review`）

> ai-check/status 委托 + alerts/gt-context 重写。只写 ai_suggestion，不碰 manual_status/指标。5 位新码。

- **`GET /api/v1/review/tasks/{task_id}/alerts`** — 任务全量告警（含 effective_status + ai_suggestion,不分页）。
  - 成功：200 `{alerts,count}`。错误：`21400`(404) 任务不存在。
- **`GET /api/v1/review/tasks/{task_id}/gt-context`** — 某视频 GT 事件区间 + 告警时间点（时间轴渲染）。
  - 参数：query `video_id`(必填)。成功：200 `{gt_events,alerts}`(任务不存在返回空结果,不校验)。错误：`11401`(400) 缺 video_id。
- **`POST /api/v1/review/tasks/{task_id}/ai-check`** — 提交批量智能审查（委托旧 ai_check,起 daemon 线程,200 非 202）。
  - 参数：body JSON（旧视图自读）`merged_ids`(必填,非空 list)。成功：200 `{batch_id,total}`。
  - 错误（文案映射）：`11400`(400) 缺 merged_ids；`21400`(404) 任务不存在；`11402`(400) 未配置 API；`21401`(404) 未找到匹配告警；`41400`(500) fallback。
- **`GET /api/v1/review/tasks/{task_id}/ai-check/status`** — 轮询审查进度（委托旧 ai_check_status）。
  - 参数：query `batch_id`(必填)。成功：200 `{status,total,done,current_id,results,error}`。
  - 错误（文案映射）：`11403`(400) 缺 batch_id；`21402`(404) 批次不存在/不属于该任务；`41400`(500) fallback。

### 3.11 assistant（FF=12，蓝图 `api_v1_assistant`）

> chat/confirm/cancel/clear/history 委托 + settings/tasks 重写。**核心契约**：chat/confirm/cancel 的 200+`{type:'error'}` 保 200 套 ok(body)，**绝不映射 4xx/5xx**；仅纯参数缺失映射 400。5 位新码。

- **`GET /api/v1/assistant/settings`** — 获取助手设置（密钥脱敏）。成功：200 `{settings}`。
- **`POST /api/v1/assistant/settings`** — 更新设置（成功回读脱敏）。参数：body JSON 字段由 `update_assistant_settings` 处理(未明确)。成功：200 `{settings}`。
- **`POST /api/v1/assistant/chat`** — AI 聊天（委托旧 api_chat）。
  - 参数：body JSON（旧视图自读,含 `message`）。成功：200 旧 body（含 type:'error' 也保 200）。
  - 错误：`11200`(400) 消息空；`41200`(500) 兜底。
- **`POST /api/v1/assistant/pending-confirmations:confirm`** — 确认待确认操作（委托旧 api_confirm）。
  - 参数：body JSON（含 `confirmation_id`）。成功：200 旧 body。错误：`11201`(400) 缺 confirmation_id；`41200`(500) 兜底。
- **`POST /api/v1/assistant/pending-confirmations:cancel`** — 取消待确认操作（委托旧 api_cancel）。
  - 参数：body JSON（含 `confirmation_id`）。成功：200 旧 body。错误：`11202`(400) 缺 confirmation_id；`41200`(500) 兜底。
- **`POST /api/v1/assistant/sessions:clear`** — 清除当前会话历史（委托旧 api_clear）。成功：200 `{message}`。
- **`GET /api/v1/assistant/sessions/history`** — 获取当前会话历史（委托旧 api_history）。成功：200 `{messages}`(缺省 [])。
- **`GET /api/v1/assistant/tasks`** — assistant 任务列表（真分页）。成功：200 分页，items 含 `id,task_type,ref_type,ref_id,status,params,result_summary` 等。
- **`GET /api/v1/assistant/tasks/{task_id}`** — 任务状态及进度。成功：200 `{task,progress}`。错误：`21200`(404) 不存在。

### 3.12 evaluation（FF=13，蓝图 `api_v1_evaluation`）

> 13 端点委托（execute/finalize/confirm/unconfirm/results/event-metrics/report×4/sync-gt/eval-status）+ 23 原位重写。**核心指标区**（`compute_task_metrics`/`get_effective_status`/命中判定）**只调不改**。PUT→PATCH×4、DELETE→204×3、create/clone→201、400→409（finalize/unconfirm/report）、400→404（clone/sync-gt）。5 位新码。

- **`GET /api/v1/evaluation/tasks`** — 评测任务列表（真分页,含名称富化 + algo_versions）。成功：200 分页，items 含 `_TASK_COLS` 全列 + 富化字段。
- **`GET /api/v1/evaluation/tasks/{task_id}`** — 任务详情。成功：200 task_dict。错误：`21300`(404)。
- **`GET /api/v1/evaluation/tasks/{task_id}/status`** — 评测运行进度（委托旧 eval_status）。成功：200 旧 body。错误：`21311`(404) 无运行中评测；`41300`(500) 兜底。
- **`GET /api/v1/evaluation/tasks/{task_id}/results`** — 评测结果（委托旧 get_results,含内联 realtime 指标）。成功：200 旧 body。错误：`21300`(404)；`41300`(500) 兜底。
- **`GET /api/v1/evaluation/tasks/{task_id}/event-metrics`** — 事件级指标（委托旧 get_event_metrics,调 compute_task_metrics）。成功：200 旧 body。错误：`21300`(404)；`41300`(500) 兜底。
- **`GET /api/v1/evaluation/tasks/{task_id}/check-updates`** — 检查关联标注是否有更新（比较 updated_at）。
  - 成功：200 `{has_updates:bool}`（`eval_tasks` 无 updated_at 列 → 恒 false,旧既有行为）。错误：`21300`(404)。
- **`POST /api/v1/evaluation/tasks`** — 新建评测任务。
  - 参数：body JSON `name`(必填)；`dataset_id`/`alert_eval_set_id`(至少一个)；`eval_set_id`(非 realtime 必填)；`notes`/`merge_interval_sec`(默认 5.0)/`event_interval_sec`(默认 10.0)/`trigger_rate`(默认 0.5)/`min_event_duration_sec`(默认 0)/`duration_hours`(realtime)。
  - 成功：201 `{task}` + Location。错误：`11300`(400) name 空；`11301`(400) 未选告警数据集/评测集；`21301`(404) 告警数据集不存在；`11302`(400) 未选评测视频集；`21302`(404) 评测视频集不存在；`21303`(404) 告警评测集不存在。
- **`POST /api/v1/evaluation/tasks/{task_id}:clone`** — 复制任务配置。
  - 参数：body JSON `name`(默认 "{源名}(复制)")；`notes`(默认 "")。成功：201 `{task}` + Location。
  - 错误：`21304`(404) 源任务不存在；`21305`(404) 源任务视频集已不存在。
- **`PATCH /api/v1/evaluation/tasks/{task_id}`** — 更新任务参数（部分更新）。
  - 参数：body JSON `merge_interval_sec`/`event_interval_sec`/`trigger_rate`/`min_event_duration_sec`/`duration_hours`(仅传则改)。成功：200 `{task}`。错误：`21300`(404)。
- **`DELETE /api/v1/evaluation/tasks/{task_id}`** — 删除任务及级联数据。成功：204。错误：`21300`(404)。
- **`POST /api/v1/evaluation/tasks/{task_id}:analyze`** — 分析可合并事件（复用 analyze_merged_events）。成功：200 分析结果。错误：`21300`(404)；`41300`(500) 分析出错。
- **`POST /api/v1/evaluation/tasks/{task_id}:confirm`** — 保存合并告警 + GT 事件（委托旧 confirm_merged,body 旧视图自读）。成功：200 旧 body。
  - 错误（文案映射）：`21300`(404)；`11313`(400) video_id 不在评测视频集；`41300`(500) 兜底。
- **`POST /api/v1/evaluation/tasks/{task_id}:execute`** — 执行评测（委托旧 execute_task,起 worker,worker 内联命中判定 + compute_task_metrics,绝不抽改）。成功：200 旧 body。
  - 错误（文案映射）：`21300`(404)；`31300`(409) 评测运行中；`11312`(400) 关联多个同类算法版本；`41300`(500) 兜底。
- **`POST /api/v1/evaluation/tasks/{task_id}:finalize`** — 确认结果 + 算指标 + 锁定（委托旧 finalize_task,调 compute_task_metrics）。成功：200 旧 body。
  - 错误（文案映射）：`21300`(404)；`31301`(409) 只有已完成才能确认（旧 400→409）；`41300`(500) 兜底。
- **`POST /api/v1/evaluation/tasks/{task_id}:unconfirm`** — 取消确认（委托旧 unconfirm_task,清指标）。成功：200 旧 body。
  - 错误（文案映射）：`21300`(404)；`31302`(409) 尚未确认（旧 400→409）；`41300`(500) 兜底。
- **`PATCH /api/v1/evaluation/tasks/{task_id}/merged-events/{merged_id}/status`** — 改单条合并告警人工状态（旧 PUT→PATCH）。
  - 参数：body JSON `manual_status`(必填,auto/correct/false_positive/ignored)。成功：200 `{manual_status}`。
  - 错误：`11303`(400) 无效状态；`21300`(404)；`21306`(404) 记录不存在。
- **`PATCH /api/v1/evaluation/tasks/{task_id}/merged-events:batch-status`** — 批量改人工状态（旧 PUT→PATCH）。
  - 参数：body JSON `manual_status`(必填)；`merged_ids`(list,必填)。成功：200 `{updated_count}`。
  - 错误：`11303`(400) 无效状态；`11304`(400) 缺 merged_ids；`21300`(404)。
- **`PATCH /api/v1/evaluation/tasks/{task_id}/gt-events/{gt_id}`** — 改 GT 事件预期/实际计数（旧 PUT→PATCH）。
  - 参数：body JSON `confirmed_count`/`actual_count`(至少一个)。成功：200 `{}`。
  - 错误：`21300`(404)；`11305`(400) 缺要更新字段；`21306`(404) 记录不存在。
- **`GET /api/v1/evaluation/tasks/{task_id}/report/image`** — PNG 报告图（委托旧,二进制直传,不走信封）。成功：200 PNG 流。错误：`21300`(404)；`41300`(500) 兜底。
- **`POST /api/v1/evaluation/tasks/{task_id}/report`** — HTML 详细报告（委托旧,二进制直传）。成功：200 HTML 流。
  - 错误（文案映射）：`21300`(404)；`31303`(409) 请先完成评测（旧 400→409）；`41301`(500) 生成报告失败；`41300`(500) 兜底。
- **`POST /api/v1/evaluation/tasks/{task_id}/report/pdf`** — PDF 报告（委托旧,Playwright 渲染,二进制直传）。成功：200 PDF 流。
  - 错误（同 report +）：`41302`(500) PDF 生成失败；`41300`(500) 兜底。
- **`POST /api/v1/evaluation/tasks/{task_id}/report:preview`** — AI 摘要预览（委托旧,调 `_call_claude`）。body（旧视图自读）。成功：200 旧 body。
  - 错误（文案映射）：`21300`(404)；`11310`(400) 缺 API Key；`41300`(500) 兜底。
- **`POST /api/v1/evaluation/tasks/{task_id}/report:chat`** — AI 对话迭代（委托旧,调 `_call_claude_chat`）。body（旧视图自读）。成功：200 旧 body。
  - 错误（同 preview）：`21300`(404)；`11310`(400) 缺 API Key；`41300`(500) 兜底。
- **`GET /api/v1/evaluation/gt-frames/{frame_id}/file`** — GT 帧图片 / 缩略图（二进制直传）。
  - 参数：query `w`(int,可选)/`h`(int,可选)。成功：200 图片流。错误：`21300`(404) Frame/File not found。
- **`POST /api/v1/evaluation/gt:sync`** — GT 同步（委托旧 sync_ground_truth,body 旧视图自读）。成功：200 旧 body。
  - 错误（文案映射）：`11306`(400) 缺视频 ID；`11307`(400) 同步方向非法；`21309`(404) 视频不存在；`11308`(400) 视频 ID 未设置；`21310`(404) GT 文件不存在；`41303`(500) 读取 GT 文件失败；`41300`(500) 兜底。
- **`GET /api/v1/evaluation/pre-analysis`** — 测前分析列表（真分页,result_json 解析为 result）。成功：200 分页，items 含 `id,eval_video_set_id,result,eval_set_name` 等。
- **`GET /api/v1/evaluation/pre-analysis/{record_id}`** — 测前分析详情。成功：200。错误：`21307`(404) 不存在。
- **`GET /api/v1/evaluation/pre-analysis:by-set/{set_id}`** — 某评测视频集的测前分析（真分页）。成功：200 分页。
- **`POST /api/v1/evaluation/pre-analysis`** — 执行测前分析并存记录。
  - 参数：body JSON `eval_video_set_id`(必填)；`merge_interval_sec`(默认 5.0)/`event_interval_sec`(默认 10.0)/`trigger_rate`(默认 0.5)/`min_event_duration_sec`(默认 0)。
  - 成功：201 `{record_id,result}` + Location。错误：`11309`(400) 未选 eval_video_set_id 或分析含 error。
- **`DELETE /api/v1/evaluation/pre-analysis/{record_id}`** — 删除测前分析记录。成功：204。错误：`21307`(404)。
- **`GET /api/v1/evaluation/eval-sets`** — 评测视频集列表（真分页,含 video_count + gt_frame_count）。成功：200 分页。
- **`GET /api/v1/evaluation/eval-sets:with-analysis-count`** — 评测视频集 + 各集分析次数（真分页）。成功：200 分页，items 含 `analysis_count`。
- **`GET /api/v1/evaluation/tasks/{task_id}/chat-sessions`** — 任务 Chat 报告会话列表（真分页,不含 messages）。成功：200 分页，items 含 `id,name,summary_text,conclusion_text`。错误：`21300`(404) 任务不存在。
- **`POST /api/v1/evaluation/tasks/{task_id}/chat-sessions`** — 保存/更新 Chat 会话（upsert）。
  - 参数：body JSON `session_id`(可选,有则更新无则新建)；`name`(默认 "未命名会话")；`messages`(默认 [])；`summary_text`/`conclusion_text`(默认 "")。
  - 成功：新建 201 / 更新 200 `{session_id}`（新建 + Location）。错误：`21300`(404)；`21308`(404) 会话不存在（update 时）。
- **`GET /api/v1/evaluation/tasks/{task_id}/chat-sessions/{session_id}`** — Chat 会话详情（含完整 messages）。成功：200 `{id,name,messages,summary_text,conclusion_text}`。错误：`21308`(404)。
- **`DELETE /api/v1/evaluation/tasks/{task_id}/chat-sessions/{session_id}`** — 删除 Chat 会话。成功：204。错误：`21308`(404)。

### 端点统计

| 模块 | 端点数 |
|------|--------|
| videos | 11 |
| alerts | 24 |
| alerts OCR | 5 |
| algorithms | 8 |
| event-types | 5 |
| config | 3 |
| streaming/tasks | 11 |
| auto-annotation | 10 |
| extract | 5 |
| review | 4 |
| assistant | 9 |
| evaluation | 36 |
| **合计** | **131** |

## 4. 模块文档索引

| 模块 | 子文档 |
|------|--------|
| config | `docs/rest-api-config.md` |
| streaming/tasks | `docs/rest-api-streaming.md` |
| auto-annotation | `docs/rest-api-auto-annotation.md` |
| extract / review / assistant / evaluation | `docs/rest-api-extract-review-assistant-evaluation.md` |
| 错误码规范 | `docs/rest-api-error-codes.md` |

## 5. 测试

- 全套 v1 测试位于 `tests/test_api_v1_*.py`，共享 fixture 在 `tests/conftest.py`（隔离 tmp DB + 双绑定 patch + teardown 删库 + slow marker）。
- EasyOCR 真跑用例标 `@pytest.mark.slow`，默认随主套件运行（CPU 约 7.6s/张）。
- streaming 真实冒烟用例通过 `STREAM_SMOKE=1` 环境变量 opt-in，默认 skip。
