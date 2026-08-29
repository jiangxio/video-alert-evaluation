# 接入指南

本文档说明如何将平台对接到 AI 产品常态化评测场景：注册算法版本、导入告警数据、导入真值（Ground Truth），完成算法版本迭代的评测闭环。所有接口、格式、ID 均基于代码实际实现，可供集成方直接参照对接。

> **术语**：**真值（GT，Ground Truth）**＝人工标注的视频中真实发生的事件（事件类型＋起止时间）。**告警**＝被测算法对水印视频运行后产出的检测结果图片。**水印**＝注入视频的“视频标识＋时间戳”标记，用于将告警对齐回视频中的真实时刻。

---

## 评测闭环总览

**① 算法版本注册 → ② 视频打水印 + 标注事件(GT) → ③ 告警数据导入 → ④ 评测任务 → ⑤ 指标计算 → ⑥ 报告生成**

**各环节职责**：

| 环节 | 操作对象 | 关键产出 | 对应路由/脚本 |
|------|---------|---------|-------------|
| ①算法版本注册 | 算法类型 + 版本 + 配置 | `algorithm_versions` 表 | `/algorithms/` |
| ②打水印 + GT | 原始视频 | 水印视频 + `ground_truth/{id}.json` + GT 帧 | `scripts/process_single.py` |
| ③告警导入 | 告警图片 | `alert_images` 表 + OCR 结果 | `/alerts/` |
| ④评测任务 | 数据集 + 评测视频集 | 命中/误检判定 + `eval_results` | `/evaluation/` |
| ⑤指标计算 | 评测结果 | 精确率/召回率/误检小时 | `eval_service.compute_task_metrics` |
| ⑥报告生成 | 评测指标 | 自包含 HTML/PDF | `/evaluation/<id>/report-config` |

---

## 一、算法版本注册

平台管理被测算法的版本，便于追溯与对比。算法版本通过 Web 界面或 REST API 注册，并关联到告警数据集参与评测。

### 1.1 Web 界面

访问「算法管理」页面（`/algorithms/`），新建算法版本，填写：

- **算法类型**：下拉选择，即事件类型 key（如 `personFallDown`）
- **版本名称**：如 `v2.3-20260814`
- **版本日期**：如 `2026-08-14`
- **描述**：选填
- **配置文件**：上传算法配置（JSON/YAML）
- **可执行文件**：上传算法包（ZIP 等）

同一页面可管理事件类型（`/algorithms/types`），支持新增/修改/删除。

### 1.2 REST API

#### 创建算法版本

```bash
curl -X POST http://localhost:8080/algorithms/api/versions \
  -F "algorithm_type=personFallDown" \
  -F "name=v2.3-20260814" \
  -F "version_date=2026-08-14" \
  -F "description=跌倒检测v2.3" \
  -F "config_file=@config.yaml" \
  -F "algorithm_file=@algorithm.zip"
```

**请求字段**（`multipart/form-data`）：

| 字段 | 必填 | 说明 |
|------|------|------|
| `algorithm_type` | 是 | 事件类型 key，必须在 `get_event_types()` 返回列表中 |
| `name` | 是 | 版本名称 |
| `version_date` | 是 | 版本日期 |
| `description` | 否 | 描述 |
| `config_file` | 否 | 算法配置文件（JSON/YAML） |
| `algorithm_file` | 否 | 算法可执行包 |

**响应**（`201 Created`）：

```json
{"id": 5}
```

#### 查询版本列表

```bash
curl http://localhost:8080/algorithms/api/versions
```

响应返回所有版本，按创建时间倒序，包含关联的数据集列表。

#### 查询版本详情（含配置解析）

```bash
curl http://localhost:8080/algorithms/api/versions/5/detail
```

响应包含版本信息、关联数据集、配置解析结果（由 `config_parser.parse_config` 解析）。

#### 更新/删除/批量下载

```bash
# 更新版本（PATCH，字段同创建）
curl -X PATCH http://localhost:8080/algorithms/api/versions/5 \
  -F "description=跌倒检测v2.3（修订）"

# 删除版本（有数据集引用时拒绝删除）
curl -X DELETE http://localhost:8080/algorithms/api/versions/5

# 批量下载算法文件（打包 ZIP）
curl -X POST http://localhost:8080/algorithms/api/download-batch \
  -H "Content-Type: application/json" \
  -d '{"ids": [1, 2, 3], "type": "all"}'
```

### 1.3 算法类型与事件类型

**算法类型即事件类型**。事件类型注册表定义于 `app/event_types.py`（硬编码降级备份）与数据库 `event_types` 表（运行时主数据源），配置文件 `config/alert_types.json` 为数据库不可用时的种子来源。

当前已注册的 18 种事件类型（ID 100–117）：

| ID | key | 中文名 |
|----|-----|--------|
| 100 | rat | 老鼠检测 |
| 101 | smoke | 抽烟检测 |
| 102 | use_phone | 玩手机检测 |
| 103 | call_phone | 打电话检测 |
| 104 | chef | 厨师服/厨师帽检测 |
| 105 | trash | 垃圾桶未关检测 |
| 106 | mask | 未戴口罩检测 |
| 107 | flame | 火焰检测 |
| 108 | fireEscapeOccupy | 消防通道占用识别 |
| 109 | safetyOfficerOnDuty | 人员离岗检测 |
| 110 | personSleep | 睡岗检测 |
| 111 | personLadderHigh | 登高检测 |
| 112 | withoutHelmetOnSite | 工地人员不戴安全帽检测 |
| 113 | withoutRefClothes | 反光衣识别 |
| 114 | personFallDown | 人员跌倒检测 |
| 115 | personAction | 人员动作检测 |
| 116 | inHandDangerTool | 手持危险工具检测 |
| 117 | fight | 打架检测 |

每个事件类型包含以下字段：`id`（数字）、`key`（英文标识，唯一，创建后不可修改）、`name`（中文名）、`description`（行为描述）、`bg_color`/`fg_color`（标签颜色）、`tags`（标签数组）、`sort_order`（排序）。

#### 事件类型 CRUD API

```bash
# 获取所有事件类型详情
curl http://localhost:8080/algorithms/api/event-types

# 新增事件类型
curl -X POST http://localhost:8080/algorithms/api/event-types \
  -H "Content-Type: application/json" \
  -d '{
    "id": 118,
    "key": "newEventType",
    "name": "新事件类型",
    "description": "描述",
    "bg_color": "#e0e0e0",
    "fg_color": "#333333",
    "tags": ["安全", "人员"]
  }'

# 修改事件类型（不允许修改 key）
curl -X PATCH http://localhost:8080/algorithms/api/event-types/118 \
  -H "Content-Type: application/json" \
  -d '{"name": "新事件类型（修订）"}'

# 查询引用情况（被哪些表引用）
curl http://localhost:8080/algorithms/api/event-types/118/references

# 删除事件类型（有引用时拒绝）
curl -X DELETE http://localhost:8080/algorithms/api/event-types/118
```

**约束**：

- `key` 只能包含字母、数字、下划线，创建后不可修改
- 删除前检查引用表：`algorithm_versions`、`events`、`auto_annotation_tasks`、`eval_merged_events`、`eval_gt_events`，有引用时返回 400 拒绝
- 新增/修改/删除事件类型后，自动调用 `_sync_alert_types_json()` 将当前数据库内容按 id 排序写回 `config/alert_types.json`，保持文件与 DB 一致

---

## 二、视频打水印与真值标注

评测依赖「水印」实现告警与真值的自动对齐：给视频加水印（含视频 ID + 时间戳），OCR 提取水印即可将告警图片对齐到视频的精确时刻。

### 2.1 水印格式与 FFmpeg 设置

水印由 FFmpeg `drawtext` 滤镜添加，设置见 `scripts/process_single.py` 的 `DEFAULT_CONFIG`：

| 参数 | 值 | 说明 |
|------|----|------|
| 位置 | `(20, 20)` | 左上角，`watermark_x=20`, `watermark_y=20` |
| 字号 | 32px | `font_size=32` |
| 字体颜色 | 白色 | `font_color=white` |
| 字体 | DejaVuSans-Bold | 跨平台候选字体列表，Linux 优先 `/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf` |
| 背景框 | 黑色 | `box=1`, `box_color=black` |
| 框边框 | 12px | `boxborderw=12` |
| 文本 | `{视频ID} {HH:MM:SS}` | 视频ID + 空格 + FFmpeg 时间戳展开 `%{pts:hms}` |
| 视频编码 | libx264 | CRF=23, preset=medium, GOP=50 |

水印文本中视频 ID 为 **10 位数字**，时间戳为 `HH:MM:SS` 格式。OCR 识别时会将 `O`/`o` 纠正为 `0`，并自动修复冒号被误识为 `.`/`3`/`2`/空格的情况。

### 2.2 CLI 打水印

> Docker 部署下，命令在 web 容器内执行，前缀 `docker compose exec web`；镜像已内置依赖，无需 `--install`。

```bash
# 单视频打水印
docker compose exec web python process.py --single video1/0514000003.mp4

# 批量打水印（video1/ 和 video2/ 目录下所有视频）
docker compose exec web python process.py --batch
```

### 2.3 视频管理（Web）

通过「视频管理」页面上传视频，设置 **10 位数字视频 ID**（如 `0514000003`），点击打水印。Web 平台通过 `watermark_service.add_watermark()` 调用 `scripts/process_single.py`。

### 2.4 标注事件，生成 Ground Truth

在「视频管理」页面为视频标注事件区间（事件类型 + 起止秒数），系统自动：

1. 生成 `ground_truth/{视频ID}.json` 真值文件
2. 将标注写入数据库 `events` 表
3. 每秒截取一帧作为 GT 帧，存入 `gt_frames` 表，用于核对与报告

#### Ground Truth JSON 格式

```json
{
  "file": "0514000003.mp4",
  "id": "0514000003",
  "events": [
    {"type": "rat", "start": 5.0, "end": 8.0},
    {"type": "rat", "start": 8.0, "end": 11.0},
    {"type": "rat", "start": 11.0, "end": 14.0}
  ]
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `file` | string | 视频文件名 |
| `id` | string | 视频 ID（10 位数字） |
| `events[].type` | string | 事件类型 key（必须在 `event_types` 注册表中） |
| `events[].start` | float | 事件起始秒数 |
| `events[].end` | float | 事件结束秒数 |

> 启动时 `app/database.py` 的 `import_ground_truth()` 自动将 `ground_truth/*.json` 导入数据库。

#### GT 与 DB 双向同步

通过 `/evaluation/api/sync-gt` 接口可实现 JSON 文件与数据库标注互相同步：

```bash
# 以 DB 标注为准，生成/覆盖 GT JSON 文件
curl -X POST http://localhost:8080/evaluation/api/sync-gt \
  -H "Content-Type: application/json" \
  -d '{"video_db_id": 3, "direction": "db_to_gt"}'

# 以 GT JSON 文件为准，同步到 DB 标注
curl -X POST http://localhost:8080/evaluation/api/sync-gt \
  -H "Content-Type: application/json" \
  -d '{"video_db_id": 3, "direction": "gt_to_db"}'
```

---

## 三、告警数据导入

被测算法对水印视频运行后产出告警图片，导入平台做评测。导入时系统从文件名自动识别告警类型，并支持批量上传。

### 3.1 告警文件名规范

文件名必须编码「告警类型 ID」与「触发时间戳」。**标准格式**：

```
{video_id}_{unix时间戳}_{告警类型ID}.png
```

示例：`402_1774925112_103.png`

- `402` — 来源视频 ID（可为任意长度数字）
- `1774925112` — Unix 时间戳
- `103` — 告警类型 ID（`call_phone`）

**格式约束**：

- 三段均为纯数字，以下划线 `_` 分隔
- 告警类型 ID 必须在 `config/alert_types.json` 中登记，否则导入时标记为无效类型（不阻断导入，响应返回 `invalid_type_ids` 警告）
- 扩展名支持 `png`/`jpg`/`jpeg`/`gif`/`bmp`

### 3.2 文件名解析规则

解析逻辑见 `app/services/verification_service.py` 的 `extract_alert_type_id`，采用三级匹配策略：

| 优先级 | 正则 | 适用场景 | 取值 |
|--------|------|---------|------|
| 标准（优先） | `^\d+_\d+_(\d+)\.[^.]+$` | 三段式 `video_id_unix_ts_alert_type_id.ext` | 第三段 |
| 兜底 1 | `[_\-](\d+)\.[^.]+$` | 末尾带分隔符的数字 `xxx-105.png` | 分隔符后数字 |
| 兜底 2 | `(\d+)\.[^.]+$` | 裸数字 `105.png` | 扩展名前数字 |

> 建议统一使用标准三段式命名，兜底规则仅为兼容历史数据。返回的 ID 未必在 `alert_types.json` 中登记，调用方应自行校验有效性。

### 3.3 alert_types.json 格式

每行一条记录，`id key` 格式（空格分隔），实际内容如下：

```
100 rat
101 smoke
102 use_phone
103 call_phone
104 chef
105 trash
106 mask
107 flame
108 fireEscapeOccupy
109 safetyOfficerOnDuty
110 personSleep
111 personLadderHigh
112 withoutHelmetOnSite
113 withoutRefClothes
114 personFallDown
115 personAction
116 inHandDangerTool
117 fight
```

**约束**：

- `id`：数字，唯一（示例中为 3 位，实际不限位数）
- `key`：英文标识，与 `event_types` 表 key 一致，唯一
- 顺序无关，启动时按 id 排序播种到数据库 `event_types` 表
- 新增类型：编辑此文件重启服务自动播种；或通过「事件类型」页面操作（自动回写此文件）

### 3.4 创建数据集并关联算法版本

```bash
# 创建告警数据集（normal 或 realtime 模式）
curl -X POST http://localhost:8080/alerts/api/datasets \
  -H "Content-Type: application/json" \
  -d '{
    "name": "2026-08 告警数据集",
    "notes": "v2.3 算法产出",
    "mode": "normal",
    "algorithm_version_ids": [5]
  }'

# 关联算法版本（每种类型只能选一个）
curl -X POST http://localhost:8080/alerts/api/datasets/3/algorithm-versions \
  -H "Content-Type: application/json" \
  -d '{"algorithm_version_ids": [5]}'
```

**校验规则**：同一数据集下，每种算法类型只能关联一个版本，否则返回 400。

### 3.5 批量导入告警图片

从压缩包（`zip`/`tar`/`tar.gz`/`tgz`）批量导入告警图片：

```bash
curl -X POST http://localhost:8080/alerts/api/datasets/3/import \
  -F "file=@alerts.zip"
```

**响应**：

```json
{
  "success": true,
  "imported": 156,
  "skipped": 3,
  "skipped_files": ["402_1774925112_103.png"],
  "invalid_type_ids": ["999"]
}
```

- `imported`：成功导入数
- `skipped`：同数据集内文件名重复跳过数
- `invalid_type_ids`：解析到 ID 但未在 `alert_types.json` 登记的类型 ID 排序列表（不阻断导入）

#### 单张上传

```bash
curl -X POST http://localhost:8080/alerts/api/datasets/3/upload \
  -F "image=@402_1774925112_103.png"
```

### 3.6 OCR 识别

导入后需对告警图片执行 OCR，提取水印中的视频 ID 和时间戳。

#### 单张 OCR

```bash
curl -X POST http://localhost:8080/alerts/api/images/42/ocr
```

**响应**：

```json
{
  "success": true,
  "ocr": {
    "image": "/app/uploads/alerts/3/402_1774925112_103.png",
    "raw_ocr_text": "0514000003 00:01:30.000",
    "video_id": "0514000003",
    "timestamp": "00:01:30.000",
    "timestamp_seconds": 90.0,
    "success": true
  }
}
```

#### 批量 OCR（后台线程）

```bash
# 启动批量 OCR（默认只处理未成功识别的图片）
curl -X POST http://localhost:8080/alerts/api/datasets/3/ocr/batch \
  -H "Content-Type: application/json" \
  -d '{"force_all": false, "stop_on_failure": false}'

# 查询进度
curl http://localhost:8080/alerts/api/datasets/3/ocr/status

# 取消
curl -X POST http://localhost:8080/alerts/api/datasets/3/ocr/cancel
```

#### 手动保存 OCR 结果

当 OCR 识别失败时，可手动输入水印内容：

```bash
curl -X POST http://localhost:8080/alerts/api/images/42/ocr/manual \
  -H "Content-Type: application/json" \
  -d '{"video_id": "0514000003", "timestamp": "00:01:30.000", "timestamp_seconds": 90.0, "success": true}'
```

### 3.7 告警评测集

评测集是多个数据集的集合，用于评测任务选择告警来源：

```bash
# 创建告警评测集
curl -X POST http://localhost:8080/alerts/api/eval-sets \
  -H "Content-Type: application/json" \
  -d '{"name": "8月评测集", "dataset_ids": [3, 4]}

# 批量添加数据集到评测集
curl -X POST http://localhost:8080/alerts/api/eval-sets/batch-add \
  -H "Content-Type: application/json" \
  -d '{"set_id": 1, "dataset_ids": [5, 6]}'
```

### 3.8 数据集模式

数据集支持两种模式，影响评测判定逻辑：

| 模式 | 说明 | 评测影响 |
|------|------|---------|
| `normal` | 普通告警图片数据集 | 需要 GT 事件，执行命中判定（±5s 容差） |
| `realtime` | 实时采集模式 | 无 GT，所有告警待人工标注，使用 `duration_hours` 计算误检/小时 |

```bash
curl -X PUT http://localhost:8080/alerts/api/datasets/3/mode \
  -H "Content-Type: application/json" \
  -d '{"mode": "realtime"}'
```

---

## 四、评测任务

在「评测」页面（`/evaluation/`）创建评测任务，将告警数据集与评测视频集关联，完成命中判定与指标计算。

### 4.1 创建任务

#### 普通模式

```bash
curl -X POST http://localhost:8080/evaluation/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "name": "跌倒检测 v2.3 评测",
    "notes": "测试场景",
    "dataset_id": 3,
    "alert_eval_set_id": 1,
    "eval_set_id": 2,
    "merge_interval_sec": 5.0,
    "event_interval_sec": 10.0,
    "trigger_rate": 0.5,
    "min_event_duration_sec": 0
  }'
```

#### 实时模式

实时模式下 `eval_set_id` 可选，`duration_hours` 必填：

```bash
curl -X POST http://localhost:8080/evaluation/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "name": "实时采集评测",
    "dataset_id": 5,
    "duration_hours": 24.0,
    "merge_interval_sec": 5.0
  }'
```

**任务参数说明**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `dataset_id` | — | 告警数据集 ID（与 `alert_eval_set_id` 至少填一个） |
| `alert_eval_set_id` | — | 告警评测集 ID |
| `eval_set_id` | — | 评测视频集 ID（普通模式必填） |
| `merge_interval_sec` | 5.0 | 合并间隔（秒），相邻告警在此间隔内合并为一个事件 |
| `event_interval_sec` | 10.0 | 事件间隔（秒），用于理论告警数预估 |
| `trigger_rate` | 0.5 | 触发率，用于理论告警数预估 |
| `min_event_duration_sec` | 0 | 最小事件时长（秒），短于此的事件被过滤 |
| `duration_hours` | — | 评测时长（小时），实时模式必填 |

#### 复制任务

复制配置（评测视频集 + 所有参数），不复制告警数据集和结果：

```bash
curl -X POST http://localhost:8080/evaluation/api/tasks/5/clone \
  -H "Content-Type: application/json" \
  -d '{"name": "v2.4 评测（复用配置）"}'
```

### 4.2 执行评测

#### 分析可合并事件

执行评测前，先分析可合并的告警事件：

```bash
curl -X POST http://localhost:8080/evaluation/api/tasks/5/analyze
```

#### 确认合并结果

用户确认/调整合并告警和 GT 事件后保存：

```bash
curl -X POST http://localhost:8080/evaluation/api/tasks/5/confirm \
  -H "Content-Type: application/json" \
  -d '{
    "merged_alerts": [
      {
        "video_id": "0514000003",
        "event_type": "rat",
        "image_ids": [42, 43],
        "representative_image_id": 42,
        "ts_start": 90.0,
        "ts_end": 95.0
      }
    ],
    "gt_events": [
      {
        "gt_event_id": "gt_001",
        "video_id": "0514000003",
        "event_type": "rat",
        "start_sec": 5.0,
        "end_sec": 8.0,
        "expected_count": 1,
        "confirmed_count": 1,
        "mid_frame_id": 10
      }
    ]
  }'
```

**校验**：普通模式下，告警的 `video_id` 必须全部包含在评测视频集中，否则返回 400 及 `missing_video_ids`。实时模式跳过此校验。

#### 执行评测（后台线程）

```bash
curl -X POST http://localhost:8080/evaluation/api/tasks/5/execute
```

执行逻辑（见 `evaluation.py` 的 `execute_task`）：

**普通模式 — 命中判定（±5s 容差）**：

对每个合并告警，与 GT 事件按以下条件匹配（须同时满足）：
- `video_id` 相同
- `event_type` 相同
- 时间窗口重叠（容差 5 秒）

重叠判定（三种情况满足其一即可）：

1. 告警 `ts_start` 落在 GT 容差区间 `[start_sec-5, end_sec+5]`
2. 告警 `ts_end` 落在 GT 容差区间
3. 告警窗口完全覆盖 GT 容差区间（`ts_start <= g_start` 且 `ts_end >= g_end`）

匹配成功 → `is_false_positive=False`（命中）；否则 → `is_false_positive=True`（误检）。

> **关键约束**：命中判定只看时间重叠，不限次数。同一 GT 事件区间内 N 张告警均可独立命中。`gt_hit_counts` 仅用于统计每个 GT 事件被命中次数（写入 `actual_count`），不影响单张告警的命中判定。

**实时模式**：跳过命中判定，所有告警 `is_false_positive=0`，待人工标注。

**查询进度**：

```bash
curl http://localhost:8080/evaluation/api/tasks/5/status
```

```json
{"total": 200, "done": 156, "running": true}
```

### 4.3 确认结果与人工修正

#### 修改合并告警的人工状态

```bash
# 单条修改（manual_status: auto/correct/false_positive/ignored）
curl -X PUT http://localhost:8080/evaluation/api/tasks/5/merged-events/12/status \
  -H "Content-Type: application/json" \
  -d '{"manual_status": "correct"}'

# 批量修改
curl -X PUT http://localhost:8080/evaluation/api/tasks/5/merged-events/batch-status \
  -H "Content-Type: application/json" \
  -d '{"merged_ids": [12, 13, 14], "manual_status": "false_positive"}'
```

**有效状态优先级**（`get_effective_status`）：`manual_status` 优先于 `is_false_positive`。

| `manual_status` | 生效逻辑 |
|----------------|---------|
| `correct` | 直接判为命中 |
| `false_positive` | 直接判为误检 |
| `ignored` | 排除出统计 |
| `auto` 或未设置 | 以 `is_false_positive` 为准 |

#### 修改 GT 事件的预期/实际触发数

```bash
curl -X PUT http://localhost:8080/evaluation/api/tasks/5/gt-events/8 \
  -H "Content-Type: application/json" \
  -d '{"confirmed_count": 3, "actual_count": 2}'
```

#### 完成评测（锁定并计算指标）

```bash
curl -X POST http://localhost:8080/evaluation/api/tasks/5/finalize
```

只有 `status='done'` 的任务才能确认。确认后任务锁定（`finalized=1`），可取消确认重新评测：

```bash
curl -X POST http://localhost:8080/evaluation/api/tasks/5/unconfirm
```

### 4.4 核心指标

| 指标 | 计算口径 |
|------|----------|
| 精确率（Precision） | `有效状态=correct 的告警数 / 有效状态≠ignored 的告警总数` |
| 召回率（Recall） | 各事件类型召回率的**算术平均**（不加权）：`event_recall = hit_count / gt_count` |
| 平均误检数/小时 | 各事件类型 `误检数 / 评测视频总时长(小时)` 的**算术平均** |

**召回率各类型 gt_count / hit_count 计算规则**：

| confirmed_count | actual_count | gt_count | hit_count | 语义 |
|-----------------|-------------|----------|-----------|------|
| 0 | 0 | 忽略（不计入） | — | 不预期且未触发，跳过 |
| 0 | >0 | 1 | 1 | 不预期但触发了，按 1 次算 |
| >0 | 任意 | confirmed | min(actual, confirmed) | 预期 N 次，封顶 N 次 |

> **关键约束**：`hit_count = min(actual, confirmed)` 的封顶逻辑只在召回率计算时生效，不影响单张告警的命中/误检判定。修改 `confirmed_count` 只影响召回率，不影响精确率。

**实时模式指标**：无 GT 事件，召回率 `None`。精确率 = `(总数 - 误检数) / 总数`，误检/小时按各事件类型分别计算后算术平均，使用 `duration_hours` 而非视频总时长。

详细计算逻辑见 `CLAUDE.md` 的「评测核心指标计算逻辑」章节与 `eval_service.py` 的 `compute_task_metrics`。

#### 查询事件级指标

```bash
curl http://localhost:8080/evaluation/api/tasks/5/event-metrics
```

```json
{
  "success": true,
  "event_metrics": [
    {
      "event_type": "rat",
      "alert_count": 50,
      "correct_pred_count": 45,
      "false_positive_count": 5,
      "gt_count": 13,
      "hit_count": 12,
      "missed_gt_count": 1,
      "precision": 0.9,
      "recall": 0.923
    }
  ],
  "overall": {
    "accuracy": 0.9,
    "recall": 0.923,
    "avg_fp_per_hour": 2.5,
    "alert_count": 50,
    "gt_count": 13
  }
}
```

### 4.5 报告生成

#### 报告图片

```bash
curl http://localhost:8080/evaluation/api/tasks/5/report-image -o report.png
```

#### 详细算法验证报告（自包含 HTML）

```bash
curl -X POST http://localhost:8080/evaluation/api/tasks/5/detailed-report \
  -H "Content-Type: application/json" \
  -d '{
    "project_name": "跌倒检测系统",
    "report_title": "算法验证报告",
    "project_background": "针对视频流跌倒检测算法的验证",
    "modules": ["cover", "summary", "env", "overview", "events", "video", "time", "conclusion"],
    "summary_text": "",
    "conclusion_text": ""
  }' -o report.html
```

#### PDF 报告（Playwright 渲染）

```bash
curl -X POST http://localhost:8080/evaluation/api/tasks/5/detailed-report-pdf \
  -H "Content-Type: application/json" \
  -d '{"project_name": "跌倒检测系统", "report_title": "算法验证报告"}' \
  -o eval_report.pdf
```

#### AI 摘要与结论生成

```bash
# 生成 AI 摘要和结论初版（需配置 API Key）
curl -X POST http://localhost:8080/evaluation/api/tasks/5/detailed-report-preview \
  -H "Content-Type: application/json" \
  -d '{"project_name": "跌倒检测系统", "project_background": "验证场景"}'

# Chat 迭代修改
curl -X POST http://localhost:8080/evaluation/api/tasks/5/detailed-report-chat \
  -H "Content-Type: application/json" \
  -d '{
    "messages": [{"role": "user", "content": "补充误检分析"}],
    "current_summary": "...",
    "current_conclusion": "..."
  }'
```

### 4.6 测前分析

评测前可对评测视频集执行测前分析，预估理论告警数、GT 覆盖率、事件时长分布等：

```bash
curl -X POST http://localhost:8080/evaluation/api/pre-analysis \
  -H "Content-Type: application/json" \
  -d '{
    "eval_video_set_id": 2,
    "merge_interval_sec": 5.0,
    "event_interval_sec": 10.0,
    "trigger_rate": 0.5,
    "min_event_duration_sec": 0
  }'
```

返回结果包含：事件类型统计（总数/时长分布/理论告警数/被过滤数）、GT 覆盖率、GT 与 DB 一致性对比等。

---

## 五、目标检测评测（od 模块，独立服务）

平台另集成一个**独立的目标检测评测服务** `od-dataset-manager`，与视频流评测完全解耦（独立 Flask app / 独立数据库 `annotations.db` / 独立端口 5000），补足图像级目标检测评测能力。

### 5.1 访问与部署

- **访问**：`http://<主机>:5000/`，或从主平台导航「目标检测」入口跳转
- **部署**：`docker compose up -d` 同时启动 web（8080）+ od（5000）两个服务
- **解耦**：停 od 不影响视频评测，反之亦然；数据卷独立（`od_db` / `od_datasets`）
- **独立运行**：`cd od-dataset-manager && python app.py`（监听 `0.0.0.0:5000`）

`docker-compose.yml` 中 od 服务配置：

```yaml
od:
  build: ./od-dataset-manager
  ports:
    - "5000:5000"
  environment:
    - OD_DB_DIR=/app/data
  volumes:
    - od_db:/app/data
    - od_datasets:/app/datasets
```

### 5.2 数据结构

od 模块采用 **项目（Project）→ 版本（Version）→ 图片（Image）** 三级结构：

| 层级 | 说明 | 关键字段 |
|------|------|---------|
| 项目 | 定义类别列表 | `name`, `classes`（如 `["call_phone", "not_call_phone", "other"]`） |
| 版本 | 一个数据集快照 | `name`, `images_dir`, `labels_dir`, `classes` |
| 图片 | 单张图片 + 标注 | `name`, `filename`, `image_width/height` |

### 5.3 评测流程

1. **创建项目**：配置类别列表（如 `call_phone, not_call_phone, other`）与图片目录
2. **创建版本**：导入图片和标注，支持 COCO/YOLO/内部 JSON 格式
3. **标注真值**：在标注页面（`/version/<version_id>`）用 SVG 拖拽画矩形框，标注真实目标
4. **导入预测**：被测算法输出 YOLO 格式预测文件（`.txt`），指定预测目录
5. **评测计算**：按 IoU 阈值匹配预测框与真值框，按类统计 tp/fp/fn

#### 评测 API

```bash
curl -X POST http://localhost:5000/api/evaluate \
  -H "Content-Type: application/json" \
  -d '{
    "version_id": "v1",
    "pred_dir": "/path/to/predictions",
    "conf_threshold": 0.25,
    "iou_threshold": 0.5
  }'
```

**参数**：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `version_id` | — | 必填，版本 ID |
| `pred_dir` | — | 必填，预测文件目录路径（须存在） |
| `conf_threshold` | 0.25 | 置信度阈值，低于此值的预测框忽略 |
| `iou_threshold` | 0.5 | IoU 匹配阈值 |

**响应**：

```json
{
  "images": [
    {
      "name": "0040",
      "filename": "0040.png",
      "gt_boxes": [{"label": "call_phone", "points": [[97.7, 61.1], [129.1, 97.2]], "matched": true}],
      "pred_boxes": [{"label": "call_phone", "points": [[95.0, 60.0], [130.0, 98.0]], "conf": 0.92, "matched": true}]
    }
  ],
  "metrics": {
    "call_phone": {"precision": 0.92, "recall": 0.88, "tp": 45, "fp": 4, "fn": 6},
    "not_call_phone": {"precision": 0.85, "recall": 0.90, "tp": 38, "fp": 7, "fn": 4},
    "_overall": {"precision": 0.89, "recall": 0.89, "tp": 83, "fp": 11, "fn": 10}
  }
}
```

**匹配逻辑**：按类分组，预测框按置信度降序排列，贪心匹配 IoU 最高的未匹配 GT 框。`IoU >= iou_threshold` 视为匹配。无 GT 的图片也纳入统计（全为 FP）。

### 5.4 支持格式

| 格式 | 用途 | 坐标系 |
|------|------|--------|
| 内部 JSON | 标注存储（`shapes[].points` 矩形两点） | 绝对像素 |
| COCO | 导入/导出数据集 | 绝对像素（`bbox: [x, y, w, h]`） |
| YOLO | 预测文件格式 + 导入/导出 | 归一化坐标（`cx cy w h`，0–1） |

**YOLO 预测文件格式**（`.txt`，每行一条）：

```
class_id cx cy w h conf
```

- `class_id`：类别索引（从 0 开始，对应版本 `classes` 列表）
- `cx cy w h`：归一化中心坐标 + 宽高（0–1）
- `conf`：置信度（可选，默认 1.0）

**内部标注格式**（JSON）：

```json
{
  "imagePath": "0040.png",
  "imageHeight": 480,
  "imageWidth": 640,
  "shapes": [
    {
      "label": "call_phone",
      "class_idx": 0,
      "points": [[97.7, 61.1], [129.1, 97.2]],
      "group_id": null,
      "shape_type": "rectangle",
      "flags": {}
    }
  ]
}
```

### 5.5 与视频评测的区别

| 维度 | 视频流评测（web 8080） | 目标检测评测（od 5000） |
|------|----------------------|------------------------|
| 输入 | 水印视频的告警截图 | 任意图片 |
| 真值 | 时间区间事件（GT JSON） | 矩形框坐标（DB 标注） |
| 对齐方式 | OCR 时间戳 ↔ GT 时间区间 | IoU 框匹配 |
| 数据库 | `benchmark.db` | `annotations.db` |
| 指标 | 精确率/召回率/误检数每小时 | 精确率/召回率（按类 + 整体） |
| 部署 | `docker compose` web 服务 | `docker compose` od 服务 / 独立运行 |
| 框架 | Flask + SQLite + EasyOCR | Flask + SQLite + PIL |

> od 模块代码位于 `od-dataset-manager/`，配置见 `od-dataset-manager/config.py`（`CLASSES`、`IMAGES_DIR`、`LABELS_DIR`、`DB_DIR`）。
