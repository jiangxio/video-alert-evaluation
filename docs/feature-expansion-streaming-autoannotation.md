# 功能扩展文档：推流增强 + 自动标注置信度/复核/版本/质量 + AI 助手集成

> 本文档是「推流 + 自动标注 + AI 助手」功能扩展的**唯一叙述文档**。它只记录本次扩展**新增/修改**的行为，**不重复**既有收口文档已覆盖的基础 REST 资源化内容。
>
> **与既有文档的边界**（既有文档正文一律不动，仅在此以链接引用）：
> - 基础推流端点（11 个）与基础错误码 → [streaming 改造文档](./rest-api-streaming.md)
> - 基础自动标注端点（10 个）与基础错误码 → [auto-annotation 改造文档](./rest-api-auto-annotation.md)
> - REST 通用约定（信封/分页/错误码：HTTP 状态码即 `code` + 可选 `error_code` 字符串）→ [REST API 总览](./rest-api.md)
> - 错误码注册表 → [错误码文档](./rest-api-error-codes.md)（**本扩展新码仅在本文登记，不改注册表**）
> - CLI 总览 → [统一 CLI 文档](./cli-unified-refactor.md)
>
> 评测指标核心算法（`compute_task_metrics`/`_get_effective_status`）**未修改**，CLAUDE.md「评测核心指标计算逻辑」段不动；标注质量联动只**读取**已存储的评测结果列，详见 §5.4。

## 变更总览（TL;DR）

> 一句话：在既有 REST 基础上给推流加**转码兜底 + 并发上限 + 断流续播**，给自动标注加**置信度分流 + 人工复核 + GT 版本化 + 质量指标**，并把这两簇能力作为 **8 个新助手工具**接入对话（写入操作走确认卡片）。

- **推流增强**：源编码不兼容时 `-c copy` → `libx264` 转码兜底；`STREAM_MAX_CONCURRENT`（默认 2，转码按 2 倍计）超限返 409 `STREAM_CONCURRENCY_LIMIT`；`loop_count` 封顶 100；断流重连调 `_compute_resume_position` 保留进度（`_reconnect_lock` 防竞态）。
- **自动标注**：`analyze_frame` 返 `{"labels":[{label,confidence}]}` JSON（解析失败容错回退标签+1.0）；按 `confidence_threshold`（默认 0.6）分流 `auto_approved`/`pending`；复核端点（`pending-events`/`:review`/`:batch-approve`，**POST** 非 PATCH）；GT 不再静默覆盖——`gt_versions` 表 + `ground_truth_versions/{id}/v{N}.json` 快照 + 版本回溯端点；质量端点返回置信分布/覆盖率/复核拒绝率/下游评测（只读已存值）。
- **动态描述注入**：标注启动可传 `event_descriptions={key:描述}`，prompt **通用不写死**，按类型独立注入，优先级 用户注入 > DB 描述 > 中文名。
- **AI 助手**：新增 `list_stream_tasks`/`get_stream_progress`/`get_annotation_status`/`get_annotation_result`（只读）+ `start_stream`/`stop_stream`/`start_auto_annotation`/`review_annotation`（写入，走确认）。`start_stream` 接原视频 `video_id`，内部解析水印视频 id。确认后反馈以 `role=system` 注入 LLM（**不**用 `role=user`，防内部工具名泄露给前端，规则7）。
- **错误码**：新增 error_code——推流 `STREAM_CONCURRENCY_LIMIT`(409，推流并发超限)；自动标注 `INVALID_PARAMETER`(400)/`VERSION_NOT_FOUND`/`REVIEW_EVENT_NOT_FOUND`(404)/`EVENT_NOT_REVIEWABLE`(409)（复核/版本），均见 §7。
- **CLI**：`scripts/stream_videos.py` 仍走 `-c copy`（转码兜底仅在 Web 平台侧，CLI 不含）；`process.py` 子命令 `stream`/`stream-fight`/`stream-merged` 透传参数。
- **测试**：全量 **380 passed, 3 skipped**（3 个 opt-in 真环境烟测：streaming `STREAM_SMOKE`、annotation `ANNO_SMOKE`）。

## 1. 改动文件清单

| 文件 | 改动 | 关键内容 |
|---|---|---|
| `app/routes/streaming.py` | 改 | `_probe_codec_compatible`/`_build_ffmpeg_cmd` 转码分支、`_start_task_internal` 并发计数、`_compute_resume_position`/`_monitor_video_process` 续播、`stream_tasks.transcode` 列 |
| `app/routes/auto_annotation.py` | 改 | `_do_auto_annotation` 存置信度、`_merge_frame_results`(`is None` 修 or 陷阱)、`_event_review_status` 分流、GT JSON 加 `name`、`event_descriptions` 透传、复核/版本/质量逻辑 |
| `app/services/behavior_analysis_service.py` | 改 | `build_prompt` 通用化+描述优先级、`analyze_frame`/`_parse_label_confidence` JSON+容错 |
| `app/routes/videos.py` | 改 | `generate_ground_truth_json` 加 `name`、`_snapshot_gt_version` 版本快照 |
| `app/api/v1/auto_annotation.py` | 改 | 复核/版本/质量端点 + `get_task_json ?version=` + 新错误码 |
| `app/services/assistant_tools.py` | 改 | 8 个新工具 schema + analyze/execute 分发 + `_localize_ts` + `start_stream` id 解析 |
| `app/services/assistant_service.py` | 改 | `SYSTEM_PROMPT` 规则7 + `confirm()` 改 `role=system` |
| `app/database.py` | 改 | `stream_tasks.transcode`、`auto_annotation_frames.{confidence,review_status}`、`auto_annotation_tasks.{confidence_threshold,event_descriptions}`、`auto_annotation_events`、`gt_versions` 表 |
| `tests/test_api_v1_streaming.py` | 改 | 转码/并发/续播单测 |
| `tests/test_api_v1_auto_annotation.py` | 改 | 置信度/复核/版本/质量 + event_descriptions 注入用例 |
| `tests/test_api_v1_assistant.py` | 改 | Phase4 工具流 + 泄露回归用例 |
| `tests/test_smoke_auto_annotation.py` | 改 | 真 worker 冒烟（受控 mock + opt-in 真 LLM） |

**未触碰**：既有 5 份 docs（rest-api / cli-unified-refactor / rest-api-streaming / rest-api-auto-annotation / rest-api-error-codes）、CLAUDE.md 评测段、`app/routes/evaluation.py` 指标函数体。

---

## 2. 推流增强

基础推流端点见 [streaming 改造文档](./rest-api-streaming.md)。本节只讲扩展的四个机制。

### 2.1 转码兜底

推流默认 `-c copy`（不重编码，低开销）。但源视频编码不被 MediaMTX/RTSP 友好支持时，`-c copy` 会推流失败。故推流前先探测编码：

- **`_probe_codec_compatible(path) -> bool`**（`app/routes/streaming.py:432`）：调 `ffprobe` 取视频/音频 codec。**兼容**（可 `-c copy`）的条件：视频为 `h264`/`hevc`/`h265` **且** 音频为 `aac` 或无音频。任一不满足 → 需转码。探测失败（ffprobe 缺失/超时/非视频）→ 返回 `True`（保持旧的 copy-by-default，失败再靠重连兜底）。
- **转码命令**（`_build_ffmpeg_cmd`，`:562`）：转码分支用
  ```
  -c:v libx264 -preset veryfast -maxrate 2M -bufsize 4M -c:a aac
  ```
  `-maxrate`/`-bufsize` 稳定码率，`-preset veryfast` 限速。**不含 `-an`**——音频要么 copy 要么转 AAC。续播抽帧子命令用 `libx264/veryfast/aac`（不加 maxrate/bufsize）+ `-avoid_negative_ts make_zero`。
- **`stream_tasks.transcode`**（`INTEGER DEFAULT 0`，`app/database.py:566`）：标记本次是否走了转码路径，供并发计数与排查。
- 一轮拼接里**任一**视频不兼容则整轮转码（`needs_transcode = not all(_probe_codec_compatible(v) for v in videos)`）。

### 2.2 并发上限

`_start_task_internal`（`app/routes/streaming.py:1158`，计数在 `:1194-1203`）启动前查运行中任务数：

```sql
SELECT COALESCE(SUM(CASE WHEN transcode=1 THEN 2 ELSE 1 END), 0) AS used
FROM stream_tasks WHERE status='running'
```

- **环境变量 `STREAM_MAX_CONCURRENT`**（默认 `"2"`，`:1195`）。
- **转码任务按 2 倍占配额**（`transcode=1 THEN 2 ELSE 1`）；新任务 `cost = 2 if needs_transcode else 1`。
- 超限（`used + cost > max_concurrent`）→ v1 抛 409 `STREAM_CONCURRENCY_LIMIT`（「超出并发推流上限，请先停止其他任务」），见 §7。
- 设计意图：转码 CPU 开销高，故占双份配额，防一台机跑满。

### 2.3 参数封顶

- `loop_count` 封顶 **100**（`if loop_count > 100: loop_count = 100`，下限 1）。v1 create（`:236`）/update（`:474`）与旧路由均一致，防误操作长循环。

### 2.4 断流续播保留进度

旧实现重连时把 `resume_video_index/offset/loop` 重置为 `0/0/1`（从头丢进度）。现改为：

- **`_compute_resume_position(task_id, started_at)`**（`app/routes/streaming.py:664`）返回 `(current_video_index, current_video_offset, current_loop, round_offset)`。`round_offset` = 当前轮次内的累计偏移秒数，正是 `_build_ffmpeg_cmd` 的 `resume_offset` 所需。任务未启动/已结束/不可算时返回全 `None`。`_compute_resume_offset`（`:708`）是其薄封装，只取 `round_offset`（float，默认 0.0）。
- **重连分支**（`_monitor_video_process:863`）：ffmpeg 非零退出且可重试且 `restart_count < max_restarts` 时，`resume_offset = _compute_resume_offset(task_id, started_at)`，写回 `resume_video_index=0, resume_offset=?, resume_loop=1, restart_count+1`，sleep 5s 后调 `_play_video(task_id, total_loops, resume_offset=resume_offset)` 从偏移处续播。
- **竞态防护**：复用既有 `_reconnect_lock`（`:927` 加 `_reconnecting_tasks`，`:946` finally 释放）。`_sync_running_status`（`:71`）对「DB 仍 running 但进程已死」也有并行重连路径，同样持 `_reconnect_lock` 并算 `resume_offset` 后起 `_delayed_play_video` 线程。

### 2.5 环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `STREAM_MAX_CONCURRENT` | `2` | 并发推流上限（转码按 2 倍计） |
| `STREAM_SMOKE` | 未设→skip | 测试 opt-in：跑真 ffmpeg→MediaMTX 端到端烟测 |

`MEDIAMTX_PORT` 是模块常量 `8554`（`streaming.py:35`），非环境变量。

---

## 3. 自动标注：置信度

基础标注端点见 [auto-annotation 改造文档](./rest-api-auto-annotation.md)。本节起讲扩展。

### 3.1 模型输出格式与容错

`analyze_frame`（`app/services/behavior_analysis_service.py:110`）要求模型返回：

```json
{"labels":[{"label":"fight","confidence":0.82}]}
```

`_parse_label_confidence`（`:135`）容错三级回退（向后兼容旧调用方）：
1. `json.loads(text)`；
2. 失败则取首个 `{` 到末个 `}` 子串再 `json.loads`；
3. 仍无合法 `{"labels":[...]}` → 按逗号/`，`/`、`分隔标签`解析，每标签 `confidence=1.0`。
置信度一律 clamp 到 `[0.0, 1.0]`（`:169`）。全空时 `analyze_frame` 最终回退 `[{"label":"normal","confidence":1.0}]`（`:238`）。

### 3.2 通用 prompt 与描述优先级

`build_prompt(valid_event_types, event_descriptions=None)`（`behavior_analysis_service.py:97`）**通用、不写死任何事件类型**：动态列举传入的 `valid_event_types`，每类显示 `f"{i}. {etype}（{name}）: {desc}"`，并声明 `Valid labels: {types joined}, normal.`（`:102`/`:127`）。

描述来源优先级（`:119`）：
```
event_descriptions.get(etype) or type_descriptions.get(etype) or ""
```
即 **用户注入 > DB 描述（`get_type_descriptions()`）> 仅中文名**。这避免裸 key 问题：旧 prompt 只给 `fight` 时模型按字面读成「打架」，而本配置里 `fight` 的中文名是「人员聚集」，显示 key+中文名+描述后模型才能正确对应语义。

### 3.3 置信度落库与阈值分流

- 每帧存 `auto_annotation_frames.confidence`（`REAL`）+ `review_status`（`TEXT DEFAULT 'auto'`，`database.py:459`）。
- `confidence_threshold` 默认 **0.6**（DB 列 `REAL DEFAULT 0.6`、函数默认 0.6、`start_task` `0.6 if _ct is None else float(_ct)`），存 `auto_annotation_tasks.confidence_threshold`。
- `_event_review_status(conf, threshold)`（`auto_annotation.py:149`）：`conf >= thr → "auto_approved"`，`< thr → "pending"`，`confidence is None → 视为 1.0`（auto_approved）。
- **`_merge_frame_results` 的 or 陷阱修复**（`auto_annotation.py:110`）：
  ```python
  # 不能用 `or 1.0`：0.0（合法低置信）是 falsy 会被提升为 1.0
  conf = 1.0 if _raw_conf is None else float(_raw_conf)
  ```
  事件级置信 = 成员帧最高置信（`:136`/`:144`），4 位小数。
- **GT JSON 只写 `auto_approved` 事件**（`auto_annotation.py:346-353`）；`pending` 事件留 `auto_annotation_events` 表待复核，不进 GT。

### 3.4 GT JSON 格式

`_do_auto_annotation`（`auto_annotation.py:355`）与 `generate_ground_truth_json`（`videos.py:100`）一致写出：

```json
{
  "file": "video.mp4",
  "id": "046",
  "events": [
    {"type": "fight", "name": "人员聚集", "start": 2.0, "end": 8.0}
  ]
}
```

每事件含 `type`/`name`/`start`/`end`。`name` 由 `get_type_names().get(type, type)` 注入（不再只有 key）。

---

## 4. 自动标注：复核、版本、质量

### 4.1 复核端点（新增）

| 方法 + 路径 | 用途 | 请求体 | 响应 | 错误码 |
|---|---|---|---|---|
| `GET /api/v1/auto-annotation/tasks/<id>/pending-events` | 列待复核事件（分页） | — | `id,task_id,video_db_id,event_type,start_sec,end_sec,confidence,review_status` | 404 `AUTO_ANNOTATION_TASK_NOT_FOUND` |
| `POST /api/v1/auto-annotation/events/<event_id>:review` | 复核单事件 | `{action:"approve"\|"reject"\|"edit", type?, start?, end?}` | `{event_id, review_status, db_event_id?}` | 404 `REVIEW_EVENT_NOT_FOUND`（事件不存在）；409 `EVENT_NOT_REVIEWABLE`（非待复核）；400 `INVALID_PARAMETER`（非法操作/起止时间） |
| `POST /api/v1/auto-annotation/tasks/<id>:batch-approve` | 批量通过 | `{event_ids?: [int]}`（省略=全部 pending） | `{task_id, approved_count}` | 404 `AUTO_ANNOTATION_TASK_NOT_FOUND` |

> **注意**：复核动作是 **POST**（非 PATCH），沿用 `:action` 后缀约定（对齐 `:start`/`:stop`/`:convert-to-events`）。
> `approve` 后事件写 DB `events` 表 + 起 `_batch_capture_gt_frames` 抓帧；`reject` 标 `review_status='rejected'`；`edit` 可改 `type/start/end`，状态不变。

### 4.2 GT 版本管理（新增）

不再静默覆盖 `ground_truth/{video_id}.json`，每次提交留版本可回溯。

- **`gt_versions` 表**（`database.py:503`）：`id, video_id, task_id, version_no, path, parent_version_no, review_status(DEFAULT 'archived'), created_at`，索引 `idx_gt_versions_video`。
- **快照目录** `ground_truth_versions/{video_id}/v{N}.json`（`videos.py:118`）。
- **`_snapshot_gt_version(video_id, gt_data, versions_dir, task_id=None)`**（`videos.py:125`）：自开 sqlite 连接（独立于调用方事务）→ `MAX(version_no)+1`（COALESCE 0）→ 写快照文件 → 插 `gt_versions` 行（`parent_version_no = 上一版 or None`）→ 返回 `new_no`。
- **版本端点**：

| 方法 + 路径 | 用途 | 响应 | 错误码 |
|---|---|---|---|
| `GET /api/v1/auto-annotation/videos/<video_id>/gt-versions` | 版本列表（分页，`ORDER BY version_no DESC`） | `id,video_id,task_id,version_no,parent_version_no,review_status,created_at` | — |
| `GET /api/v1/auto-annotation/gt-versions/<version_id>` | 取某版快照内容 | `{version:{...}, content:{...}}` | 404 `VERSION_NOT_FOUND`；404 `RESULT_JSON_NOT_FOUND`（文件不存在） |
| `POST /api/v1/auto-annotation/gt-versions/<version_id>:restore` | 回滚为当前版 | `{video_id, restored_from_version, new_version_no}` | 404 `VERSION_NOT_FOUND`/`RESULT_JSON_NOT_FOUND` |

> `restore` **非破坏性**：把快照内容写回 live GT（`ground_truth/{video_id}.json`），并**记录一个新版本**（内容=被恢复的快照），原历史版本保留。`get_task_json` 支持 `?version=<version_no>` 取指定版本快照（缺省取当前 live 版）。

### 4.3 质量评估端点（新增）

`GET /api/v1/auto-annotation/tasks/<id>/quality`（`auto_annotation.py:450`）返回：

```json
{
  "confidence": {"mean":0.7,"median":0.8,"min":0.0,"max":1.0,
                 "bins":{"0-0.2":1,"0.2-0.4":0,"0.4-0.6":3,"0.6-0.8":2,"0.8-1.0":5},"count":11},
  "coverage_rate": 0.72,
  "review": {"approved":6,"rejected":1,"pending":2,"rejection_rate":0.14},
  "downstream_eval": {"eval_task_id":3,"accuracy":0.9,"recall":0.85,"avg_fp_per_hour":1.2}
}
```

- `confidence`：取 `confidence IS NOT NULL` 的帧；分箱键 `["0-0.2","0.2-0.4","0.4-0.6","0.6-0.8","0.8-1.0"]`，分箱 `min(int((c or 0)//0.2), 4)`；`median` 为排序后中位元素。
- `coverage_rate`：`confidence>0` 帧数 / 总抽帧数。
- `review`：`approved` 含 `auto_approved`+`approved`；`rejection_rate = rejected/(approved+rejected)`。
- `downstream_eval`：**只读**关联评测任务已存储的 `accuracy`/`recall`/`avg_fp_per_hour`（`eval_tasks` 中 `finalized=1` 行，且视频在对应 `eval_video_sets.video_ids` 内）。

> ⚠️ **与原计划的偏差**：计划曾设想质量端点「只读调 `compute_task_metrics`」。**实际实现不调用 `compute_task_metrics`**（它只出现在端点 docstring 注释里）——端点直接读 `eval_tasks` 已存储列。这更严格地满足「评测指标核心只调不改」：既未改算法，也未在标注侧重算，只读取已 finalize 的存储值。`downstream_eval` 为 best-effort，无关联评测时各指标为 null。

### 4.4 动态 event_descriptions 注入

- **DB 列** `auto_annotation_tasks.event_descriptions TEXT`（可空，`database.py:442`）。
- **`start_task`** 读 `data.get("event_descriptions")`，`isinstance(_ed, dict)` 才用，否则 None；存为 `json.dumps(...)` 或 None。
- **`_process_queue`**（排队路径）传 `json.loads(task["event_descriptions"]) if task["event_descriptions"] else None`；立即启动路径直接传内存 dict。
- **worker** `_do_auto_annotation(event_descriptions=None)` → `analyze_frame(..., event_descriptions)` → `build_prompt(valid_event_types, event_descriptions)`。

任一事件类型都通用——传 `['fight','rat']` 就列两个、传 `['smoke']` 就列一个，描述各自独立注入，prompt 不写死成只支持某一种标注。

---

## 5. AI 助手集成

助手原有 9 只读 + 11 写入工具（详见 `assistant_service.SYSTEM_PROMPT`）。本次新增 **8 个工具**（4 只读 + 4 写入），把推流与标注接入对话。工具循环最多 5 轮（`chat()` `max_tool_rounds=5`，`assistant_service.py:389`；`MAX_HISTORY_ROUNDS=10` 是历史裁剪、与循环无关）。

### 5.1 新增工具

**只读（直接返结果）**：

| 工具 | 参数 | 用途 |
|---|---|---|
| `list_stream_tasks` | `limit`(默认20) | 查推流任务列表 |
| `get_stream_progress` | `task_id` | 查单个推流任务进度 |
| `get_annotation_status` | — | 标注引擎状态（当前任务+排队） |
| `get_annotation_result` | `task_id` 或 `video_id`（二选一） | 标注结果事件列表（含中文名/起止秒） |

**写入（返确认卡片，用户确认后才执行）**：

| 工具 | 参数 | 用途 |
|---|---|---|
| `start_stream` | `video_id`(原视频)、`stream_name`、`loop_count`(默认1,上限100) | 创建+启动推流到 MediaMTX RTSP |
| `stop_stream` | `task_id` | 停止运行中推流 |
| `start_auto_annotation` | `video_db_id`、`event_types[]`、`frame_interval_sec`(1)、`merge_interval_sec`(5)、`confidence_threshold`(0.6)、`event_descriptions`({key:desc},可选) | 启动自动标注（抽帧+多模态+生成 GT） |
| `review_annotation` | `event_id`、`action`(approve/reject/edit)、`type?`、`start?`、`end?` | 复核待确认标注事件 |

### 5.2 对话用法示例

用户用自然语言即可，不必知道工具名（规则7）：

- 「把视频 046 推到流 demo」→ 起推流确认卡片 → 确认 → 自然语言回复「推流已启动」。
- 「查看推流任务进度」/「正在推的任务」→ 返列表/进度。
- 「标注视频 2000000009 的人员聚集」→ 起标注确认卡片 → 确认 → 标注完成后用中文名转述结果。
- 「标注视频 X 的人员聚集，描述：画面中多个人物聚集停留」→ `event_descriptions` 动态注入，模型按描述标注。
- 「复核待确认的标注事件 5」→ approve/reject/edit。
- 「视频 046 的标注结果怎样」→ 用中文名（如「人员聚集」）转述，不只报 key。

### 5.3 start_stream 的 video_id 解析

`start_stream` 接**原视频 `video_id`**（非水印视频 id），在 `analyze_start_stream`（`assistant_tools.py:1557`）内部 JOIN 解析为水印视频 `wm_id`：

```sql
SELECT wv.id AS wm_id FROM watermarked_videos wv
JOIN videos v ON v.id = wv.original_video_id WHERE v.video_id = ?
ORDER BY wv.id DESC LIMIT 1
```

无水印视频则返「视频 X 无水印视频，请先打水印」。`loop_count` 同样 clamp 到 `[1,100]`。这样 LLM 不必先查水印视频 id，但**水印视频须已存在**是前提。

### 5.4 确认流与「不泄露内部名」（规则7）

写入工具统一走：`_execute_tool` → `analyze_write_tool`（影响分析）→ `_prepare_confirmation_response`（返确认卡片）→ 用户点确认 → `confirm()` → `execute_write_tool` 执行 → 结果喂回 LLM 生成自然语言回复。

**关键修复**：`confirm()` 把「确认+执行结果」反馈以 **`role='system'`** 注入 LLM（`assistant_service.py:481`），**不是 `role='user'`**。原因：`/api/v1/assistant/sessions/history` 只过滤掉非 user/assistant 角色——若用 `role='user'`，这条内部指令（含 `add_watermark`/`start_stream` 等工具名和「请用中文…告知结果」）会被前端当**用户气泡**渲染，直接泄露给用户（违反规则7）。`system` 角色不向前端展示，仅作 LLM 上下文。同时 `api_history` **不再返回 `tool_calls`**（含工具函数名），只回 user/assistant 文本，空内容的中间步骤跳过。

**SYSTEM_PROMPT 规则7**（`assistant_service.py:80`）：
> 绝不向用户暴露内部工具名/函数名/API 路径（如 get_task_status、start_stream、review_annotation、/api/v1/…）。引导用户用自然语言继续。

### 5.5 时间本地化

`_localize_ts(s)`（`assistant_tools.py:18`）：SQLite `CURRENT_TIMESTAMP` 存 UTC，此函数转系统本地时区（如北京 UTC+8）显示串。用于 `get_task_status`/`list_assistant_tasks`/`list_stream_tasks`/`get_stream_progress` 的时间字段。

---

## 6. CLI 说明

> 本节仅说明本次扩展涉及的 CLI 行为，**不改** [统一 CLI 文档](./cli-unified-refactor.md)。

- **`scripts/stream_videos.py`**：argparse 参数 `videos`(位置,+)、`-s/--stream`(流名)、`--host`(默认 localhost)、`--port`(默认 8554)、`--mode`(`loop`/`once`,默认 loop)、`-l/--loop`(默认 20)、`--no-realtime`、`--keep-list`。其 `build_ffmpeg_cmd` **始终用 `-c copy`**——**转码兜底仅在 Web 平台侧**（`app/routes/streaming.py`），CLI 脚本不含转码逻辑（CLI 推的是已就绪的水印视频，编码兼容性由打水印环节保证）。
- **`process.py` 子命令**（`:50-52`，`has_argparse=True` 透传参数、退出码原样回传）：
  - `stream` → `scripts/stream_videos.py`（按顺序推流）
  - `stream-fight` → `scripts/stream_fight_loop.py`（Fight/NonFight 拼接循环推流）
  - `stream-merged` → `scripts/stream_merged_sources.py`（同源片段合并后推流）
- 自动标注无独立 CLI 子命令，经 Web 平台/REST/助手启动。

---

## 7. 错误码（本次新增）

> 错误码方案 = 方案3（HTTP 状态码即 `code` + 可选 `error_code` 字符串），与 [错误码文档](./rest-api-error-codes.md) 一致。本次新增的 `error_code` 见下。

### 7.1 推流（新增 1 个 error_code）

| HTTP | error_code | 含义 | 触发 |
|---|---|---|---|
| 409 | `STREAM_CONCURRENCY_LIMIT` | 超出并发推流上限，请先停止其他任务 | `_start_task_internal` 计数 `used + cost > STREAM_MAX_CONCURRENT`（转码按 2 倍计） |

> 注意区分：视频文件不存在于磁盘 → 400 `STREAM_FILE_MISSING`；并发超限 → 409 `STREAM_CONCURRENCY_LIMIT`。

### 7.2 自动标注（新增 error_code）

| HTTP | error_code | 含义 | 触发 |
|---|---|---|---|
| 400 | `INVALID_PARAMETER` | 无效版本号 / 无效复核操作 / 无效事件起止时间 | `get_task_json ?version=`、`:review` action 非法、edit 起止时间非法 |
| 404 | `VERSION_NOT_FOUND` | 版本不存在 | `gt-versions/<id>`、`:restore` 版本 id 找不到 |
| 404 | `REVIEW_EVENT_NOT_FOUND` | 复核事件不存在 | `:review` 事件 id 找不到 |
| 409 | `EVENT_NOT_REVIEWABLE` | 事件非待复核状态 | `:review` 时事件 `review_status` 不是 `pending` |

> 复核/版本/质量端点复用 `AUTO_ANNOTATION_TASK_NOT_FOUND`(任务不存在→404)/`RESULT_JSON_NOT_FOUND`(文件不存在→404)/`AUTO_ANNOTATION_FAILED`(兜底 500)，见 `app/api/v1/auto_annotation.py`。

---

## 8. 可直接运行的示例

```bash
# 推流（先确保视频已打水印；转码/并发由平台自动处理）
curl.exe -X POST "http://localhost:8080/api/v1/streaming/tasks" -H "Content-Type: application/json" \
  -d "{\"source_type\":\"single\",\"source_id\":1,\"stream_name\":\"demo\",\"loop_count\":5}"
curl.exe -X POST "http://localhost:8080/api/v1/streaming/tasks/1:start"
curl.exe "http://localhost:8080/api/v1/streaming/tasks/1/progress"

# 自动标注（带动态描述注入）
curl.exe -X POST "http://localhost:8080/api/v1/auto-annotation/tasks" -H "Content-Type: application/json" \
  -d "{\"video_db_id\":79,\"event_types\":[\"fight\"],\"event_descriptions\":{\"fight\":\"画面中多个人物聚集停留\"},\"frame_interval_sec\":1,\"confidence_threshold\":0.6}"
curl.exe "http://localhost:8080/api/v1/auto-annotation/tasks/79/pending-events"        # 待复核事件
curl.exe -X POST "http://localhost:8080/api/v1/auto-annotation/events/5:review" -H "Content-Type: application/json" \
  -d "{\"action\":\"approve\"}"
curl.exe "http://localhost:8080/api/v1/auto-annotation/tasks/79/quality"                # 质量指标
curl.exe "http://localhost:8080/api/v1/auto-annotation/videos/046/gt-versions"         # 版本列表
curl.exe "http://localhost:8080/api/v1/auto-annotation/tasks/79/json?version=2"        # 取指定版本

# AI 助手（在 Web 平台右下角助手 widget 对话）
# 「把视频 046 推到流 demo」  → 确认卡片 → 确认 → 「推流已启动」
# 「标注视频 2000000009 的人员聚集，描述：画面中多个人物聚集停留」→ 确认 → 标注 → 用中文名转述结果
```

---

## 9. 验收与隔离

- **单测**：全量 `py -m pytest tests/ -q` → **380 passed, 3 skipped**（streaming 转码/并发/续播、auto-anno 置信度/复核/版本/质量、assistant 工具流+泄露回归全绿）。
- **真环境烟测**（opt-in）：`STREAM_SMOKE=1` 推流（copy 源 + 不兼容源各推一次，起 2 流后第 3 流返 409 `STREAM_CONCURRENCY_LIMIT`）；`ANNO_SMOKE=1` 标注真 LLM 端到端。
- **隔离**：`benchmark.db`/`.env`/`logs/` 未被污染（既有隔离约定）；`ground_truth_versions/`、`auto_annotation_frames/` 为运行时产物，不纳入版本库。
- **既有文档零改动**：`git diff` 确认 rest-api / cli-unified-refactor / rest-api-streaming / rest-api-auto-annotation / rest-api-error-codes 五份 docs 正文零改动。
- **评测核心未触碰**：`app/routes/evaluation.py` 指标函数体未改；`compute_task_metrics` 不被标注质量端点调用（只读已存值）。
