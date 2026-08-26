# REST API 错误码规范

> 适用范围：`/api/v1/*` REST API。所有新端点 `raise ApiError(code, message, http_status)` 时，`code` 必须遵循本规范。旧端点（`app/routes/*.py`）不受影响。

## 码格式：5 位 = `H FF SS`

```
H  FF  SS
│  │   └─ 族内错误（2 位，按 HTTP 子类分区，见下）
│  └──── 资源族（2 位，全项目唯一分配，见分配表）
└─────── HTTP 错误类（1 位）
```

- **H（第 1 位）= HTTP 错误类**
  - `1` 客户端请求错误（400 参数 / 405 方法不允许）
  - `2` 资源不存在（404）
  - `3` 状态冲突（409）
  - `4` 服务端错误（500）
  - `5` 异步任务失败
- **FF（第 2-3 位）= 资源族**（全项目唯一分配，00-99 共 100 个位）
- **SS（第 4-5 位）= 族内错误**（每族 100 个码；按 HTTP 子类分区）

**约束**：`H` 必须与 `http_status` 对应（`1↔400/405`、`2↔404`、`3↔409`、`4↔500`、`5↔500/202`）。`raise ApiError(code, message, http_status)` 时三者一致，不能矛盾。

## FF 资源族分配表

| FF | 资源族 | 状态 | 已用码（按 `app/api/v1/` 实际 `raise ApiError` 代码，2026-08-20 核对） |
|----|--------|------|------|
| 00 | 通用（跨资源/路由兜底） | 已用 | `10000`/`10005`/`20000`/`30000`/`40000`（errorhandler 兜底） |
| 01 | videos | 已用 | 旧4位 `1001-1004/1010-1012/1020-1022/1099/1100-1101`、`2001-2003/2010`、`3001-3002`、`4001`（已迁 5 位，见对齐表） |
| 02 | datasets | 已用 | 旧4位 `1200-1202/1204/1299`、`2100-2103`、`4002`（已迁 5 位）；OCR 复用其 5位 `20220` |
| 03 | alert-images（含 OCR） | 已用 | OCR 5位 `10311/20320/30340`（+复用 FF=02 `20220`）；其余旧4位 `1205/1210/1220-1221/1298/2200-2201`（已迁 5 位） |
| 04 | eval-video-sets | 预留 | —（当前 videos/eval-sets 视为 videos 子资源用 01） |
| 05 | eval-alert-sets | 已用 | 旧4位 `1300-1301/1310-1311/1399`、`2300`（已迁 5 位） |
| 06 | algorithm-versions | 已用 | `10600-10607/20600-20602/30600/40600`（详见下「新模块已用码」） |
| 07 | event-types | 已用 | `10700-10706/20700/30700-30702`（详见下「新模块已用码」） |
| 08 | config | 已用 | `10800/40880`（详见下「新模块已用码」） |
| 09 | stream-tasks | 已用 | `10900-10905`/`20900`/`30900-30904`/`40900-40901`（详见下「新模块已用码」） |
| 10 | auto-annotation-tasks | 已用 | `11000-11003`/`21020-21023`/`31040-31041`/`41080`（详见下「新模块已用码」） |
| 11 | frame-extraction-tasks | 已用 | `11100-11102`/`21100-21102`/`41100`（详见下「新模块已用码」） |
| 12 | assistant | 已用 | `11200-11202`/`21200`/`41200`（详见下「新模块已用码」） |
| 13 | evaluation-tasks | 已用 | `11300-11313`/`21300-21311`/`31300-31303`/`41300-41303`（详见下「新模块已用码」） |
| 14 | review | 已用 | `11400-11403`/`21400-21402`/`41400`（详见下「新模块已用码」） |
| 15-99 | 余量 | — | — |

**FF 判定规则**：按 URL 末端被操作的**主资源**，不按路径含哪个父段。例：`POST /alerts/datasets/<id>/images` 末端主资源是 images → FF=03，虽路径含 datasets。`POST /alerts/datasets/<id>/images:batch-delete` 同理 FF=03。

## SS 族内分区

| SS 段 | 含义 | 对应 H |
|-------|------|--------|
| 00-19 | 参数/校验错误 | 1（400） |
| 20-39 | 资源不存在 | 2（404） |
| 40-59 | 状态冲突 | 3（409） |
| 60-79 | 异步任务相关 | 5 |
| 80-99 | 服务端/外部错误 | 4（500） |

族内同类错误聚拢、从段首递增。例：videos 族（FF=01）参数错误是 `10100-10119`，不存在是 `20120-20139`，冲突是 `30140-30159`。

## 通用码（FF=00，errorhandler 兜底/跨资源）

| 码 | 含义 | HTTP |
|----|------|------|
| 10000 | 通用参数错误 | 400 |
| 10005 | HTTP 方法不被允许 | 405 |
| 20000 | 通用资源不存在（路由未命中兜底） | 404 |
| 30000 | 通用状态冲突 | 409 |
| 40000 | 通用服务端错误 | 500 |

仅在 errorhandler 兜底或跨资源场景用；资源内具体错误用对应 FF。

## 对齐表（已用码 → 新 5 位码）

> videos/alerts 旧 4 位码已于 2026-08-24 按本对齐表迁移至 5 位；本表保留映射作历史记录。

### videos（FF=01）

| 现码 | 新码 | 含义 | HTTP |
|---|---|---|---|
| 1001 | 10100 | 没有上传文件 | 400 |
| 1002 | 10101 | 没有选择文件 | 400 |
| 1003 | 10102 | 不支持的文件格式 | 400 |
| 1004 | 10103 | 非法文件名 | 400 |
| 1010 | 10104 | 文件名不能为空 | 400 |
| 1011 | 10105 | 非法文件名 | 400 |
| 1012 | 10106 | 不支持的文件格式 | 400 |
| 1020 | 10107 | video_id 不能为空 | 400 |
| 1021 | 10108 | video_id 必须为10位数字 | 400 |
| 1022 | 10109 | video_id 已被使用 | 400 |
| 1099 | 10110 | 没有可更新的字段 | 400 |
| 1100 | 10111 | 评测集名称不能为空 | 400 |
| 1101 | 10112 | 名称不能为空 | 400 |
| 2001 | 20120 | 视频不存在 | 404 |
| 2002 | 20121 | 尚未生成水印视频 | 404 |
| 2003 | 20122 | 文件不存在于磁盘 | 404 |
| 2010 | 20123 | 评测集不存在 | 404 |
| 3001 | 30140 | 文件已存在 | 409 |
| 3002 | 30141 | 文件名被占用 | 409 |
| 4001 | 40180 | 文件重命名失败 | 500 |

### datasets（FF=02）

| 现码 | 新码 | 含义 | HTTP |
|---|---|---|---|
| 1200 | 10200 | 数据集名称不能为空 | 400 |
| 1201 | 10201 | 算法版本校验失败 | 400 |
| 1202 | 10202 | 无效的模式 | 400 |
| 1204 | 10203 | 算法版本校验失败 | 400 |
| 1299 | 10210 | 没有可更新的字段（mode） | 400 |
| 2100 | 20220 | 数据集不存在 | 404 |
| 2101 | 20221 | 没找到符合条件的图片 | 404 |
| 2102 | 20222 | 数据集为空 | 404 |
| 2103 | 20223 | 没有可下载的文件 | 404 |
| 4002 | 40280 | 打包下载失败 | 500 |

### alert-images（FF=03，含 OCR）

| 现码 | 新码 | 含义 | HTTP |
|---|---|---|---|
| 1205 | 10300 | event_label 不能为空 | 400 |
| 1210 | 10301 | 没有上传文件（images 上传） | 400 |
| 1220 | 10302 | 没有上传文件（import） | 400 |
| 1221 | 10303 | 不支持的压缩格式 | 400 |
| 1298 | 10310 | 没有可更新的字段（event_label） | 400 |
| — | 10311 | 没有需要 OCR 的图片 | 400 |
| 2200 | 20320 | 图片不存在 | 404 |
| 2201 | 20321 | 文件不存在于磁盘 | 404 |
| — | 30340 | OCR 正在运行中 | 409 |

### eval-alert-sets（FF=05）

| 现码 | 新码 | 含义 | HTTP |
|---|---|---|---|
| 1300 | 10500 | 评测集名称不能为空 | 400 |
| 1301 | 10501 | 名称不能为空 | 400 |
| 1310 | 10502 | 请选择要添加的数据集 | 400 |
| 1311 | 10503 | 请选择要移出的数据集 | 400 |
| 1399 | 10510 | 没有可更新的字段（name） | 400 |
| 2300 | 20520 | 评测集不存在 | 404 |

> 注：videos 的 eval-sets 视为 videos 子资源，用 FF=01（10111/10112/20123）。FF=04 预留给未来独立的 eval-video-sets 资源（若从 videos 拆出）。

## 新模块已用码（FF=06/07/08/09，直接 5 位，无旧码对齐）

> 与上面的「对齐表」不同：FF=06/07/08/09 是 REST 化新建的资源，无旧码可对齐，直接用 5 位新码。下表按 `app/api/v1/{algorithms,event_types,config,streaming}.py` 实际 `raise ApiError(...)` 代码核对（2026-08-20）。

### FF=06 algorithm-versions（`app/api/v1/algorithms.py`）

| 码 | 含义 | HTTP |
|---|---|---|
| `10600` | 算法类型无效 | 400 |
| `10601` | 算法名不能为空 | 400 |
| `10602` | 算法日期不能为空 | 400 |
| `10603` | 没有要更新的字段 | 400 |
| `10604` | 缺少 path 参数 | 400 |
| `10605` | 非法路径 | 400 |
| `10606` | 请选择要下载的版本 | 400 |
| `10607` | 选中的版本不存在 | 400 |
| `20600` | 算法版本不存在 | 404 |
| `20601` | 文件不存在 | 404 |
| `20602` | 没有可下载的文件 | 404 |
| `30600` | 有 N 个数据集正在使用此算法版本，无法删除 | 409 |
| `40600` | 打包失败 | 500 |

### FF=07 event-types（`app/api/v1/event_types.py`）

| 码 | 含义 | HTTP |
|---|---|---|
| `10700` | 英文标识不能为空 | 400 |
| `10701` | 中文名不能为空 | 400 |
| `10702` | 英文标识只能包含字母、数字和下划线 | 400 |
| `10703` | 标签必须是数组 | 400 |
| `10704` | ID 必须是整数 | 400 |
| `10705` | 字段格式错误 | 400 |
| `10706` | 没有要更新的字段 | 400 |
| `20700` | 事件类型不存在 | 404 |
| `30700` | 英文标识已存在 | 409 |
| `30701` | ID 已存在 | 409 |
| `30702` | 仍有 N 处引用，无法删除 | 409 |

### FF=08 config（`app/api/v1/config.py`）

| 码 | 含义 | HTTP |
|---|---|---|
| `10800` | 未知的 provider（test-connection 的 provider 非 openai/claude 或缺失） | 400 |
| `40880` | 保存失败（save_config 抛异常，保留「保存失败：xxx」message） | 500 |

### FF=09 stream-tasks（`app/api/v1/streaming.py`）

| 码 | 含义 | HTTP |
|---|---|---|
| `10900` | 来源类型无效 / 解析来源失败（水印视频或视频集不存在/为空） | 400 |
| `10901` | 请选择视频或视频集（source_id 缺失） | 400 |
| `10902` | 流名称不能为空 | 400 |
| `10903` | 流名称只能包含字母、数字、连字符和下划线 | 400 |
| `10904` | 参数不完整（preview 缺 source_type/source_id） | 400 |
| `10905` | 视频文件不存在于磁盘（start 的 Path.exists 失败） | 400 |
| `20900` | 任务不存在 | 404 |
| `30900` | 任务已在运行中（start 冲突，旧 400→409） | 409 |
| `30901` | 任务运行中，无法编辑（PATCH 冲突，旧 400→409） | 409 |
| `30902` | 请先停止任务再删除（DELETE 冲突，旧 400→409） | 409 |
| `30903` | 任务状态不可启动（start，近乎不可达） | 409 |
| `30904` | 任务未在运行中（stop 冲突，旧 400→409） | 409 |
| `40900` | 启动失败（`_start_task_internal`/`_play_video` 失败，保留具体 message） | 500 |
| `40901` | 日志读取失败 | 500 |

> FF=03 OCR 系列复用已分配码（见对齐表 alert-images/datasets 行）：`10311` 没有需要 OCR 的图片、`20320` 图片不存在、`30340` OCR 正在运行中、`20220` 数据集不存在（复用 FF=02），定义在 `app/api/v1/alerts_ocr.py:22-25`。

### FF=10 auto-annotation-tasks（`app/api/v1/auto_annotation.py`）

> 4 端点委托旧视图（start/stop/status/convert-to-events：起后台线程/操作模块级任务态），6 端点原位重写（videos-without-events/tasks 列表/by-video/get-json/delete/clear）。委托端点按旧 `error` 文案子串映射多码；stop/convert 的旧 400 修正为 409，H 位对齐 http_status。

| 码 | 含义 | HTTP | 端点 |
|---|---|---|---|
| `11000` | 未选择视频（video_db_id 缺失） | 400 | start |
| `11001` | 抽帧间隔至少为1秒（frame_interval_sec<1） | 400 | start |
| `11002` | 合并间隔不能为负数（merge_interval_sec<0） | 400 | start |
| `11003` | 至少选择一个事件类型（event_types 为空） | 400 | start |
| `21020` | 任务不存在 | 404 | delete / get-json / convert |
| `21021` | 视频不存在 | 404 | start |
| `21022` | 尚未生成水印视频（旧 400→404，对齐 videos 族 `20121`） | 404 | start |
| `21023` | JSON 文件不存在（旧 400→404，对齐同模块 get-json） | 404 | get-json / convert |
| `31040` | 当前没有运行中的任务（stop，旧 400→409，对齐 streaming `30904`） | 409 | stop |
| `31041` | 任务尚未完成（convert，旧 400→409，状态冲突） | 409 | convert |
| `41080` | JSON 文件读取失败 | 500 | get-json |

### FF=11 frame-extraction-tasks（`app/api/v1/extract.py`）

> 2 端点委托旧视图（start/status：起 daemon 线程 `_do_extract_batch` 跑真 ffmpeg + 模块级 `_extract_tasks`/`_extract_lock`），3 端点原位重写（download/delete/list）。worker 被 `assistant_tools` 共享，委托不改不重测。DELETE→204、start 保 200。

| 码 | 含义 | HTTP | 端点 |
|---|---|---|---|
| `11100` | 缺少 wm_ids 列表 | 400 | start |
| `11101` | 抽帧间隔必须大于0（interval_sec≤0，负数；0 因旧 `or 1.0` 被当默认不触发） | 400 | start |
| `11102` | 选中的视频均不可抽帧（未设 video_id 或文件不存在） | 400 | start |
| `21100` | 水印视频不存在 | 404 | start |
| `21101` | 任务不存在 | 404 | status / download / delete |
| `21102` | 帧目录不存在 | 404 | download |
| `41100` | 服务端错误（委托 fallback 兜底） | 500 | start / status |

### FF=14 review（`app/api/v1/review.py`）

> 2 端点委托旧视图（ai-check/ai-check/status：起 daemon 线程 `_ai_check_worker` 调多模态 OpenAI + 模块级 `_ai_batches`/`_ai_batches_lock`），2 端点原位重写（alerts/gt-context，复用纯读 `eval_service.get_effective_status`，只写 ai_suggestion 不碰 manual_status/指标）。review 是全仓唯一自带 LLM `timeout=120` 的调用点，不受 #20 影响。无 DELETE 端点；ai-check 成功保 200、status 保 query param batch_id。

| 码 | 含义 | HTTP | 端点 |
|---|---|---|---|
| `11400` | 请提供 merged_ids 列表（缺/非 list） | 400 | ai-check |
| `11401` | 缺少 video_id | 400 | gt-context |
| `11402` | 未配置 OpenAI 兼容 API | 400 | ai-check |
| `11403` | 缺少 batch_id | 400 | ai-check/status |
| `21400` | 任务不存在 | 404 | alerts / ai-check |
| `21401` | 未找到匹配的告警记录 | 404 | ai-check |
| `21402` | 批次不存在 / 批次不属于该任务 | 404 | ai-check/status |
| `41400` | 服务端错误（委托 fallback 兜底） | 500 | ai-check / status |

### FF=12 assistant（`app/api/v1/assistant.py`）

> 5 端点委托旧视图（chat/confirm/cancel/clear/history：调 LLM/可能起 worker/用 session+过滤逻辑，委托避免复制偏差），4 端点原位重写（settings GET/POST、tasks 列表、tasks/<id>）。**核心约束**：chat/confirm/cancel 的 200+`{type:'error',...}`（NOT_CONFIGURED/EXECUTION_FAILED/CONFIRMATION_EXPIRED）是前端契约，v1 **保 200 套 ok(body)，绝不映射 4xx/5xx**；仅纯参数缺失映射 400。无 DELETE 端点、无 400→409（HTTP 语义已干净）；tasks 列表真分页。

| 码 | 含义 | HTTP | 端点 |
|---|---|---|---|
| `11200` | 消息不能为空（chat） | 400 | chat |
| `11201` | 缺少 confirmation_id（confirm） | 400 | confirm |
| `11202` | 缺少 confirmation_id（cancel） | 400 | cancel |
| `21200` | 任务不存在 | 404 | tasks/<id> |
| `41200` | 服务端错误（委托 fallback 兜底） | 500 | chat / confirm / cancel |

### FF=13 evaluation-tasks（`app/api/v1/evaluation.py`）

> 13 端点委托（execute/finalize/confirm/unconfirm/get_results/get_event_metrics/get_report_image/detailed-report×4/sync-gt/eval-status：起 worker/调 compute_task_metrics 等指标函数/生成报告/调 LLM，**指标算法只调不改**），23 端点原位重写（任务+测前分析+评测集+chat-sessions 的查询/CRUD）。get_results 含内联 realtime 指标公式按 D3 委托避转录风险。PUT→PATCH×4、DELETE→204×3、create/clone→201、400→409（finalize/unconfirm/report「请先完成评测」）、400→404（clone 源视频集不存在/sync-gt）。

| 码 | 含义 | HTTP | 端点 |
|---|---|---|---|
| `11300` | 任务名称不能为空 | 400 | create |
| `11301` | 请选择告警数据集或告警评测集 | 400 | create |
| `11302` | 请选择评测视频集 | 400 | create |
| `11303` | 无效的状态值（manual_status 非法） | 400 | update/batch manual-status |
| `11304` | 请提供 merged_ids 列表 | 400 | batch-status |
| `11305` | 缺少要更新的字段（confirmed/actual_count） | 400 | update_gt_event_counts |
| `11306` | 缺少视频ID | 400 | sync-gt |
| `11307` | 同步方向必须是 db_to_gt 或 gt_to_db | 400 | sync-gt |
| `11308` | 视频ID未设置 | 400 | sync-gt |
| `11309` | 请选择评测视频集 / 评测视频集不存在（pre-analysis） | 400 | create_pre_analysis |
| `11310` | 缺少 API Key（Claude） | 400 | report:preview / report:chat |
| `11312` | 数据集关联多个同类算法版本（execute 校验） | 400 | execute |
| `11313` | video_id 不在评测视频集中（confirm 校验） | 400 | confirm |
| `21300` | 任务不存在 / Frame not found（通用） | 404 | 多端点 / gt-frames |
| `21301` | 告警数据集不存在 | 404 | create |
| `21302` | 评测视频集不存在 | 404 | create |
| `21303` | 告警评测集不存在 | 404 | create |
| `21304` | 源任务不存在 | 404 | clone |
| `21305` | 源任务关联的视频集已不存在（旧 400→404） | 404 | clone |
| `21306` | 记录不存在（merged_event/gt_event） | 404 | manual-status / gt-counts |
| `21307` | 分析记录不存在 | 404 | pre-analysis |
| `21308` | 会话不存在 | 404 | chat-session |
| `21309` | 视频不存在 | 404 | sync-gt |
| `21310` | GT 文件不存在 | 404 | sync-gt |
| `21311` | 没有正在运行的评测 | 404 | eval-status |
| `31300` | 评测正在运行中（execute 冲突） | 409 | execute |
| `31301` | 只有已完成的任务才能确认结果（旧 400→409） | 409 | finalize |
| `31302` | 任务尚未确认（旧 400→409） | 409 | unconfirm |
| `31303` | 请先完成评测（旧 400→409） | 409 | report / report-pdf |
| `41300` | 分析出错 / 委托 fallback 兜底 | 500 | analyze / 委托兜底 |
| `41301` | 生成报告失败 | 500 | report / report-pdf |
| `41302` | PDF 生成失败 | 500 | report-pdf |
| `41303` | 读取 GT 文件失败 | 500 | sync-gt |

> FF=13 注：`eval_tasks` 无 `updated_at` 列（ALTER 未加）→ `check-updates` 的 `SELECT updated_at` 永抛 OperationalError→except→200 `has_updates:false`，404 不可达（旧既有行为，忠实复刻；属 bug-audit 低危 evaluated_at 恒 null 范畴）。`eval-sets` 用 FF=13 共用（D2，不启 FF=04）。`review` 独立 FF=14（D1，已修 deprecation successor）。

## 选择理由

### 1. 为什么用结构化码而不是全局递增码

全局递增（如 `ERR_VIDEO_NOT_FOUND=10201` 随手编）丢掉"看码知语义"，维护全靠查表，码一多就乱。结构化码 `H FF SS` 让人/程序读出三层语义（HTTP 类 + 资源族 + 子类），定位错误快。代价是码稍长（5 位），但错误码不追求短，追求可读 + 可扩展。

### 2. 为什么 5 位（H 1 + FF 2 + SS 2）而不是 3 位

最初定的 3 位（千位 HTTP + 百位族 + 个位族内）有两个破产点：

- 百位只 10 个位（0-9），后续模块（algorithm / event-types / config / stream / auto-annotation / frame-extraction / assistant / evaluation / review）加起来超 10，会撞号；
- 族内个位只 10 个码，evaluation 这种重模块光参数错误就 10+ 个，加上不存在/冲突/服务远超 10，不够。

5 位给 FF 两位（100 个族位）、SS 两位（每族 100 码），容量充足，重模块也够。

### 3. 为什么 FF 两位够、不要三位

全项目资源族约 14 个（见分配表），两位 99 个位用了不到 1/6，余量充足。三位（1000 族位）是几乎不可能触达的过度设计，徒增码长。两位是"够用且不冗余"的平衡点。

### 4. 为什么 SS 按 HTTP 子类分区、而不是族内纯递增

纯递增会让同族同类错误散乱（参数 00、不存在 01、又一个参数 02...），查码靠表。分区后同类聚拢（参数 00-19、不存在 20-39），族内定位有规律，且每子类 20 个位置——evaluation 参数错误 10 个塞 00-19 还有余量，不会溢出。代价是族内每子类上限 20，但 20 对单个子类够用（真不够可借相邻段）。

### 5. 为什么 H 单独 1 位、不融进族内

H 是最高频的语义维度（前端最先按 HTTP 类分流处理），单独成位让码首位即语义，比"族内分区"更醒目。HTTP 错误类只有 5 种（1-5），1 位够。

### 6. 为什么留 FF=00 通用码

跨资源/路由层的错误（如 404 路由未命中、400 JSON 解析失败、500 未捕获异常）不归属任何具体资源族，用 FF=00 兜底，不占用具体族号，也不和资源内码混。

### 7. 为什么对齐表列现码→新码

videos/alerts 旧 4 位码已按本对齐表迁移至 5 位（2026-08-24）；本表保留映射作历史记录。OCR 等新模块直接用 5 位新码，无需对齐。

---

## 使用示例

```python
from app.api.v1.responses import ApiError

# videos 视频不存在（FF=01，SS=20 不存在段）
raise ApiError(20120, "视频不存在", 404)

# alert-images OCR 正在运行（FF=03，SS=40 冲突段）
raise ApiError(30340, "OCR 正在运行中", 409)

# evaluation 第 N 个参数错误（FF=13，SS=05 参数段）
raise ApiError(11305, "merge_interval_sec 必须大于 0", 400)
```

## 前端处理建议

前端按 `code` 的 `H`（首位）分流：

```js
if (body.code === 0) { /* 成功 */ }
else if (String(body.code)[0] === '1') { /* 参数错误，提示 message */ }
else if (String(body.code)[0] === '2') { /* 资源不存在 */ }
else if (String(body.code)[0] === '3') { /* 状态冲突 */ }
else { /* 服务端/异步错误 */ }
```

也可直接读 HTTP 状态码（与 H 对应），二者一致。
