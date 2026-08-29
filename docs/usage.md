# 使用指南

本平台是视频流AI算法评测工具，用于评测视频流算法的精确率、召回率、误检频率等核心指标。提供两种交互方式：**Web 可视化平台**（推荐）与**命令行脚本**。本指南面向测试与使用人员，说明如何启动平台、使用各项功能、跑通评测全流程。

> **术语**：**真值（GT，Ground Truth）**＝人工标注的视频中真实发生的事件（事件类型＋起止时间）。**告警**＝被测算法对水印视频运行后产出的检测结果图片。**水印**＝注入视频的“视频标识＋时间戳”标记，用于将告警对齐回视频中的真实时刻。

---

## 目录

- [一、快速开始](#一快速开始)
- [二、Web 平台](#二web-平台)
  - [2.1 视频管理（/videos/）](#21-视频管理videos)
  - [2.2 告警数据集（/alerts/）](#22-告警数据集alerts)
  - [2.3 评测（/evaluation/）](#23-评测evaluation)
  - [2.4 算法管理（/algorithms/）](#24-算法管理algorithms)
  - [2.5 AI 助手（/assistant/）](#25-ai-助手assistant)
  - [2.6 自动标注（/auto-annotation/）](#26-自动标注auto-annotation)
  - [2.7 推流（/streaming/）](#27-推流streaming)
  - [2.8 API 配置（/api-config/）](#28-api-配置api-config)
- [三、命令行工具](#三命令行工具)
  - [3.1 视频水印处理（process.py）](#31-视频水印处理processpy)
  - [3.2 OCR 识别（ocr_easy.py）](#32-ocr-识别ocr_easypy)
  - [3.3 告警验证（verify_alert.py）](#33-告警验证verify_alertpy)
  - [3.4 视频 ID 提取规则](#34-视频-id-提取规则)
- [四、OCR 识别规则](#四ocr-识别规则)
- [五、评测全流程操作](#五评测全流程操作)
- [六、REST API](#六rest-api)
- [七、配置文件参考](#七配置文件参考)

---

## 一、快速开始

推荐使用 Docker 部署，镜像内置全部依赖（Python、FFmpeg、EasyOCR、Chromium、字体），开箱即用：

```bash
# 1. 复制配置文件并填入 API key（唯一需修改的文件）
cp .env.example .env
# 编辑 .env，填入 OPENAI_API_KEY、ASSISTANT_ENCRYPTION_KEY 等

# 2. 一键启动
docker compose up -d --build

# 3. 浏览器访问 http://localhost:8080
```

启动后浏览器访问 `http://localhost:8080`。完整部署参数、数据持久化与端口配置见《安装指南》。

> **无 Docker 环境？** 需手动安装 Python + FFmpeg + EasyOCR 等依赖（`python process.py --install` 可装 Python 依赖），再 `source .venv/bin/activate && python run.py` 启动，监听 `0.0.0.0:8080`。详见《安装指南》「方式二：手动安装」。

---

## 二、Web 平台

### 2.1 视频管理（/videos/）

管理原始视频的完整生命周期：上传 → 设置视频 ID → 打水印 → 事件标注。

| 页面 | 路径 | 主要操作 |
|------|------|----------|
| 视频管理首页 | `/videos/` | 网格展示所有打水印视频，支持按评测集筛选、搜索（文件名/视频 ID），查看封面、分辨率、事件标签 |
| 视频上传 | `/videos/upload/` | 上传原始视频（mp4/avi/mov/mkv），自动从文件名提取视频 ID；可勾选"已打水印"直接入库 |
| 视频标注 | `/videos/<id>/annotate/` | 为打水印视频标注事件区间（事件类型+起止秒数），自动生成 Ground Truth JSON + GT 帧 |

**核心操作流程：**

1. **上传视频**：在 `/videos/upload/` 上传，系统自动提取视频 ID（需 10 位数字开头）、时长、文件大小
2. **设置视频 ID**：若自动提取的 ID 不对，可手动设置（必须恰好 10 位数字，且不与其他视频重复）
3. **确认视频 ID**：确认后视频 ID 不可更改，这是打水印的前提
4. **打水印**：调用 FFmpeg 在左上角叠加 `{视频ID} {时间戳}` 水印（异步队列处理，一次只运行一个），完成后自动提取封面并做 OCR 可读性校验
5. **标注事件**：在标注页添加事件区间，系统自动生成 `ground_truth/{视频ID}.json` 和 GT 帧（每秒 1 帧，最多 60 帧自动均匀采样）
6. **管理评测集**：将视频加入评测视频集（`eval_video_sets`），供评测任务使用

**其他功能：**
- 重命名 / 删除视频（级联删除水印版本、事件、GT 帧）
- 单个 / 批量下载（原始版 / 水印版，可选附带 GT JSON，打包为 ZIP）
- 视频拼接（最多 10 个，FFmpeg concat）与打包
- 视频裁剪（指定起止时间 + 新 10 位视频 ID，生成新视频）
- 导入外部 GT JSON 文件（校验 `id` 字段匹配后转为 DB 事件标注）
- GT 双向同步（DB 标注 ↔ JSON 文件）

### 2.2 告警数据集（/alerts/）

管理告警截图数据集，支持从压缩包批量导入、OCR 识别、标签管理。

| 页面 | 路径 | 主要操作 |
|------|------|----------|
| 数据集列表 | `/alerts/` | 展示所有告警数据集，支持创建（normal / realtime 模式）、关联算法版本 |
| 数据集详情 | `/alerts/<id>/` | 8 列网格浏览图片，分页（每页 80 张），按事件类型/视频 ID/标注状态筛选 |

**核心操作：**

- **创建数据集**：命名 + 备注 + 模式（`normal` 普通模式 / `realtime` 实时采集模式），可关联算法版本（每种算法类型只能选一个）
- **批量导入**：上传 ZIP / TAR / TAR.GZ 压缩包，自动解压并按文件名识别告警类型 ID（末尾 `_<数字>.png`），同数据集内防重名
- **单张/批量 OCR**：调用 EasyOCR 识别水印，提取视频 ID 和时间戳；批量 OCR 后台线程执行，支持进度查询、取消、失败即停（`stop_on_failure`）
- **手动保存 OCR**：OCR 识别失败时可手动输入视频 ID 和时间戳
- **标签管理**：为图片设置事件标签（`event_label`），支持批量删除（按视频 ID / 事件类型筛选）
- **告警评测集**：将多个告警数据集组合为告警评测集（`eval_alert_sets`），供评测任务使用
- **下载**：打包整个数据集图片为 ZIP

### 2.3 评测（/evaluation/）

评测核心模块，完成"告警图片 → OCR → 与真值匹配 → 指标计算 → 报告生成"全流程。

| 页面 | 路径 | 主要操作 |
|------|------|----------|
| 评测任务列表 | `/evaluation/` | 展示所有任务，查看状态/指标，创建/删除/复制任务 |
| 任务详情 | `/evaluation/<id>/` | 查看告警检测结果、GT 事件得分、调整参数、人工修正、确认锁定 |
| 报告配置 | `/evaluation/<id>/report-config` | 配置报告标题、项目背景、章节模块，生成 HTML/PDF 报告 |
| 多任务对比 | `/evaluation/compare` | 横向对比多个评测任务的指标 |
| 测前分析历史 | `/evaluation/pre-analysis` | 测前分析记录列表 |
| 测前分析详情 | `/evaluation/pre-analysis/<id>` | 查看事件区间预览、GT 覆盖率、GT/DB 一致性 |

**评测任务参数：**

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `merge_interval_sec` | 5.0 | 合并间隔（秒），相邻告警在此间隔内合并为一个事件 |
| `event_interval_sec` | 10.0 | 事件间隔（秒），用于理论告警数预估 |
| `trigger_rate` | 0.5 | 触发率，用于理论告警数预估 |
| `min_event_duration_sec` | 0 | 最小事件时长（秒），短于此值的事件被过滤 |
| `duration_hours` | — | 实时模式必填，评测时长（小时） |

**两种评测模式：**
- **普通模式**（`normal`）：需选择告警数据集 + 评测视频集，执行命中判定（时间窗口 ±5s 重叠），计算精确率/召回率/误检数每小时
- **实时模式**（`realtime`）：数据集 mode 为 `realtime`，需填写 `duration_hours`，跳过命中判定，所有告警待人工标注，仅计算精确率和误检数每小时

详见 [第五节：评测全流程操作](#五评测全流程操作)。

### 2.4 算法管理（/algorithms/）

注册和管理被测算法版本及其事件类型。

| 页面 | 路径 | 主要操作 |
|------|------|----------|
| 算法版本管理 | `/algorithms/` | 注册算法版本（名称、类型、日期、配置文件、可执行文件），版本对比与下载 |
| 事件类型管理 | `/algorithms/types` | 管理事件类型（算法类型），查看引用关系 |

**核心功能：**
- **算法版本 CRUD**：注册 / 编辑 / 删除算法版本，上传配置文件和可执行文件，支持单个 / 批量下载
- **事件类型管理**：创建 / 编辑 / 删除事件类型，查看每个类型被哪些算法版本和数据集引用
- **数据集关联**：在告警数据集页面关联算法版本（每种类型只能选一个），评测执行时快照到任务

### 2.5 AI 助手（/assistant/）

通过自然语言操作平台的 AI 助手，支持工具调用与待确认操作。

| 页面/接口 | 路径 | 说明 |
|-----------|------|------|
| 聊天组件 | `/assistant/widget` | 嵌入页面的聊天 UI（供 base.html include） |
| 设置页 | `/assistant/settings` | 配置 OpenAI API Key 等 |

**主要 API：**
- `POST /assistant/api/chat` — 发送消息，AI 解析意图并调用平台工具
- `POST /assistant/api/confirm` — 确认执行待确认操作
- `POST /assistant/api/cancel` — 取消待确认操作
- `POST /assistant/api/clear` — 清除对话历史
- `GET /assistant/api/history` — 获取对话历史
- `GET /assistant/api/tasks` — 查询 AI 助手后台任务列表

> 使用前需在 `/assistant/settings` 配置 OpenAI API Key。

### 2.6 自动标注（/auto-annotation/）

利用视觉大模型（VLM）辅助逐帧分析视频，自动生成事件标注，替代纯人工标注。

| 页面 | 路径 | 主要操作 |
|------|------|----------|
| 视频选择 | `/auto-annotation/` | 展示已打水印但尚无事件标注的视频 |
| 参数配置 | `/auto-annotation/config/<id>` | 配置抽帧间隔、合并间隔、事件类型、VLM API 凭据 |

**工作流程：** 视频抽帧（FFmpeg 按间隔截帧）→ 多模态逐帧分析（VLM 识别事件类型）→ 按类型合并事件区间（相邻帧间隔 ≤ 合并间隔则合并）→ 生成 Ground Truth JSON → 可一键转为 DB 事件标注（并串行生成 GT 帧）。

**特性：** 后台异步执行、任务排队（同时只运行一个）、中断支持、进度展示（extracting → analyzing → merging → saving）、429 限流自动重试、连续 5 帧失败自动停止。

**主要 API：**
- `POST /auto-annotation/api/start` — 启动标注任务
- `POST /auto-annotation/api/stop` — 中断当前任务
- `GET /auto-annotation/api/status` — 查询状态与队列
- `GET /auto-annotation/api/tasks` — 历史任务列表
- `GET /auto-annotation/api/json/<id>` — 查看生成的 JSON
- `POST /auto-annotation/api/convert-to-events/<id>` — 将 JSON 转为 DB 事件

### 2.7 推流（/streaming/）

通过 FFmpeg 将打水印视频以 RTSP 协议推流到 MediaMTX，模拟实时视频流供算法实时检测。

| 页面 | 路径 | 主要操作 |
|------|------|----------|
| 推流管理 | `/streaming/` | 创建/启动/停止推流任务，查看进度、RTSP 地址、日志 |

**前提条件：** 需手动启动 MediaMTX（`tools/mediamtx`，默认监听 `:8554`）。

**核心功能：**
- **推流来源**：单个打水印视频 或 评测视频集（逐个视频推流）
- **循环播放**：指定循环次数，逐视频串行推流，自动切换下一个
- **RTSP 地址**：`rtsp://<本机IP>:8554/<流名称>`，流名称仅允许字母、数字、连字符、下划线
- **断线重连**：网络错误（broken pipe 等）自动重试，最多 3 次，从当前视频开头续播
- **进度查询**：返回当前轮次、视频索引、已播放秒数、预计结束时间
- **日志查看**：返回 FFmpeg stderr 日志（限 100KB）
- **续播**：停止后可从断点续播（`resume=true`）

### 2.8 API 配置（/api-config/）

统一管理 OpenAI / Claude 等 API 凭据，供 AI 助手、自动标注、报告生成使用。

| 页面 | 路径 | 主要操作 |
|------|------|----------|
| API 配置 | `/api-config/` | 配置文本模型组（Claude）和视觉模型组（VLM）的 API Key、Base URL、模型名 |

**主要 API：**
- `GET /api-config/api/config` — 获取当前配置（Key 脱敏）
- `POST /api-config/api/save` — 保存配置
- `POST /api-config/api/test` — 测试连通性

---

## 三、命令行工具

> Docker 部署下，以下命令在 web 容器内执行，前缀 `docker compose exec web`；手动部署下在激活的虚拟环境中直接运行 `python ...`。

### 3.1 视频水印处理（process.py）

`process.py` 是跨平台 CLI 入口，调用 `scripts/process_single.py` 或 `scripts/batch_process.py` 执行 FFmpeg 水印处理。

```bash
# 处理单个视频（添加水印）
docker compose exec web python process.py --single video1/046-3.30-18:16.mp4

# 批量处理所有视频（默认行为，处理 video1/ 和 video2/ 目录）
docker compose exec web python process.py --batch
```

> Docker 镜像已内置全部 Python 依赖，无需 `python process.py --install`；仅手动部署时才需执行该命令安装依赖。

**直接调用底层脚本（更多参数）：**

```bash
# 指定输出目录、视频 ID、开头黑帧时长
docker compose exec web python scripts/process_single.py video.mp4 \
  --output-dir /path/to/output \
  --video-id 0514000003 \
  --tpad-duration 5    # 开头插入黑帧秒数，默认 5，设为 0 则不插入
```

**水印格式（FFmpeg drawtext）：**

| 参数 | 值 | 说明 |
|------|-----|------|
| 文本 | `{视频ID} %{pts:hms}` | 视频 ID + 空格 + 播放时间戳（HH:MM:SS.毫秒） |
| 字体 | DejaVuSans-Bold（Linux）/ Helvetica（macOS）/ Arial（Windows） | 32px 加粗 |
| 颜色 | 白字 + 黑底 | `fontcolor=white`, `boxcolor=black` |
| 位置 | 左上角 (20, 20) | `x=20, y=20` |
| 边框 | 12px | `boxborderw=12` |
| 视频编码 | libx264, CRF 23, preset medium | GOP 50 |

打水印完成后，脚本自动提取中间帧做 OCR 可读性校验，确认水印中的视频 ID 可被正确识别。

### 3.2 OCR 识别（ocr_easy.py）

使用 EasyOCR 识别告警截图中的水印文本。

```bash
# 识别单张图片
docker compose exec web python scripts/ocr_easy.py report/402_1774925112_103.png
```

**输出示例（JSON）：**

```json
{
  "image": "report/402_1774925112_103.png",
  "raw_ocr_text": "046 00:38:26.667",
  "video_id": "046",
  "timestamp": "00:38:26.667",
  "timestamp_seconds": 2306.667,
  "success": true
}
```

> Web 平台的 OCR 功能（`verification_service.run_ocr()`）直接调用此脚本的 `preprocess_and_ocr` 函数，复用 EasyOCR Reader 实例。

### 3.3 告警验证（verify_alert.py）

完整的验证流水线：从文件名提取告警类型 → OCR 识别水印 → 加载 Ground Truth → 判定命中。

```bash
# 验证单张告警（真实 OCR）
docker compose exec web python scripts/verify_alert.py report/402_1774925112_103.png

# 用 mock OCR 测试（无需 GPU/OCR 依赖，用于调试验证逻辑）
docker compose exec web python scripts/verify_alert.py report/402_1774925112_103.png \
  --mock-ocr '{"video_id": "046", "timestamp_seconds": 90}'

# 批量验证 report/ 目录下所有告警图片
docker compose exec web python scripts/verify_alert.py --batch

# 指定配置文件、容差、输出路径
docker compose exec web python scripts/verify_alert.py --batch \
  --config config/alert_types.json \
  --tolerance 5 \
  --output report/verification_results.json \
  --quiet    # 只输出 JSON，不打印人类可读信息
```

**验证流水线：**

```
告警图片文件名 → 提取告警类型 ID（末尾 _<数字>.png）
  → 查 config/alert_types.json 获取事件类型名
  → OCR 识别水印（视频 ID + 时间戳）
  → 加载 ground_truth/<视频ID>.json
  → 检查时间戳 ±5s 是否与匹配类型的 GT 事件区间重叠
  → 判定：correct（命中）/ incorrect（误检）/ unknown（无 GT 或 OCR 失败）
```

**判定规则：** 时间容差 ±5 秒。告警时间戳的 `[ts-5, ts+5]` 区间与同类型 GT 事件 `[start, end]` 区间有任何重叠即为 `correct`。

### 3.4 视频 ID 提取规则

CLI 与 Web 的提取规则不同：

| 场景 | 规则 | 示例 |
|------|------|------|
| CLI（process_single.py） | 文件名按 `-` 分割取首段 | `046-3.30-18:16.mp4` → `046`；不含 `-` 则整个文件名（去扩展名）作 ID |
| Web 上传（extract_video_id） | 正则匹配文件名开头的 10 位数字 `(\d{10})` | `0514000003-xxx.mp4` → `0514000003`；不足 10 位则返回 None |
| Web 手动设置（set_video_id） | 必须恰好 10 位数字 `\d{10}`，且不与其他视频重复 | `046` 会被拒绝（仅 3 位） |

> **注意：** Web 平台严格要求 10 位数字视频 ID，CLI 则更宽松。建议统一使用 10 位数字命名以便两端兼容。

---

## 四、OCR 识别规则

### 水印文本格式

水印文本格式为 `{视频ID} {时间戳}`，其中时间戳为 FFmpeg 的 `%{pts:hms}`，格式 `HH:MM:SS.毫秒`。

### 识别规则

| 字段 | 规则 | 说明 |
|------|------|------|
| 视频 ID | 恰好 10 位连续数字 `\b\d{10}\b` | 先提取视频 ID，避免其数字干扰时间戳匹配 |
| 时间戳 | 严格匹配 `HH:MM:SS.sss`（2位时:2位分:2位秒.3位毫秒） | OCR 常见误识（冒号变 `.`/`3`/空格等）会自动纠正 |
| 成功条件 | 视频 ID 和时间戳均识别成功 | `success = (video_id is not None and timestamp is not None)` |

### 预处理流程

Web 平台与 `verify_alert.py` 均调用 `ocr_easy.py`，预处理参数如下：

| 步骤 | 操作 | 参数 |
|------|------|------|
| 1. 裁剪 | 裁剪左上角水印区域 | `min(540, w) × min(50, h)` 像素 |
| 2. 灰度 | 转为灰度图 | `img.convert('L')` |
| 3. 增强对比度 | 对比度增强 | `2.5×` |
| 4. 反色 | 黑底白字 → 白底黑字 | `ImageOps.invert()` |

> `scripts/final_ocr.py`（PaddleOCR 引擎）使用更大的裁剪区域 `min(700, w) × min(120, h)`，预处理流程相同（灰度→2.5×对比度→反色），但 Web 平台和 CLI 验证脚本均使用 `ocr_easy.py`（EasyOCR）。

### OCR 常见纠错

- 字母 `O`/`o` → 数字 `0`（OCR 易将 0 误识为 O）
- 时间戳分隔符混用（`:`/`.`/`,`/`*`/`3`/`2`/空格）统一纠正为标准 `HH:MM:SS.sss`

---

## 五、评测全流程操作

### 步骤概览

```
创建任务 → 测前分析（可选） → 分析可合并事件 → 确认合并告警/GT
  → 执行评测 → 查看/修正结果 → 确认锁定 → 生成报告
```

### 步骤 1：创建评测任务

在 `/evaluation/` 点击"新建任务"，填写：

| 字段 | 必填 | 说明 |
|------|------|------|
| 任务名称 | 是 | — |
| 告警数据集 / 告警评测集 | 是（二选一） | 告警图片来源 |
| 评测视频集 | 普通模式必填 | Ground Truth 视频来源 |
| 评测时长（小时） | 实时模式必填 | `duration_hours` |
| 合并间隔 / 事件间隔 / 触发率 / 最小事件时长 | 否 | 有默认值，见 [2.3](#23-评测evaluation) |

### 步骤 2：测前分析（可选）

在测前分析页选择评测视频集，预览：
- 各事件类型的数量、时长分布（最小/最大/平均/中位数）
- GT 覆盖率（每视频、整体）
- 理论告警数预估
- GT 文件与 DB 标注的一致性对比

### 步骤 3：分析可合并事件

调用 `POST /evaluation/api/tasks/<id>/analyze`，系统根据合并间隔将同一视频、同一类型的相邻告警合并为事件，并匹配 GT 事件。

### 步骤 4：确认合并告警和 GT 事件

调用 `POST /evaluation/api/tasks/<id>/confirm`，保存用户确认的合并告警和 GT 事件列表。系统校验告警的 `video_id` 必须全部包含在评测视频集中（实时模式跳过此校验）。

### 步骤 5：执行评测

调用 `POST /evaluation/api/tasks/<id>/execute`，后台线程执行：

1. **普通模式：** 逐个判断合并告警是否命中 GT 事件（时间窗口 ±5s 重叠，不限次数），标记 `is_false_positive`，更新每个 GT 事件的 `actual_count`
2. **实时模式：** 跳过命中判定，所有告警 `is_false_positive=0`，待人工标注
3. 计算并保存指标：精确率、召回率、平均误检数/小时

进度查询：`GET /evaluation/api/tasks/<id>/status`

### 步骤 6：查看与修正结果

在 `/evaluation/<id>/` 查看告警检测结果和 GT 事件得分，可：
- 逐条 / 批量修改合并告警的人工状态（`auto`/`correct`/`false_positive`/`ignored`）
- 修改 GT 事件的 `confirmed_count` 和 `actual_count`
- 实时查看事件级指标（未锁定时实时计算）

### 步骤 7：确认锁定

调用 `POST /evaluation/api/tasks/<id>/finalize`，计算最终指标并锁定任务（`finalized=1`）。锁定后指标缓存，不可再改。如需修改可调用 `unconfirm` 取消确认后重新执行。

### 步骤 8：生成报告

在 `/evaluation/<id>/report-config` 配置报告参数（标题、项目背景、章节模块），可：
- 生成自包含 HTML 报告（`POST .../detailed-report`）
- 生成 PDF 报告（`POST .../detailed-report-pdf`，需 Playwright）
- AI 生成摘要和结论（`POST .../detailed-report-preview`，需 Claude API Key）
- Chat 迭代修改摘要/结论（`POST .../detailed-report-chat`）
- 下载报告图片（`GET .../report-image`）

### 指标计算逻辑

| 指标 | 公式 | 说明 |
|------|------|------|
| 精确率 | `correct_pred_count / alert_count` | `alert_count` = 有效状态 ≠ `ignored` 的告警总数；`correct_pred_count` = 有效状态为 `correct` 的告警数 |
| 召回率 | 各事件类型召回率的算术平均 | 每类型 `hit_count / gt_count`，其中 `hit_count = min(actual, confirmed)`（封顶），再取算术平均 |
| 平均误检数/小时 | 各事件类型误检/小时的算术平均 | 每类型 `fp_count / total_duration_hours`，再取算术平均 |

> 人工状态优先级：`manual_status`（`correct`/`false_positive`/`ignored`）高于 `is_false_positive`；未设置或 `auto` 时以 `is_false_positive` 为准。

---

## 六、REST API

除 Web 界面外，所有功能均提供 REST API。存在两套 API 体系：

### 旧版 API（各蓝图内嵌）

各功能模块路由下的 `/api/` 端点，路径形如 `/videos/api/...`、`/alerts/api/...`、`/evaluation/api/...`。

| 模块 | 端点前缀 | 示例 |
|------|----------|------|
| 评测 | `/evaluation/api/` | `GET /evaluation/api/tasks` 查任务列表 |
| 视频 | `/videos/api/` | `POST /videos/api/upload` 上传视频 |
| 告警 | `/alerts/api/` | `GET /alerts/api/datasets` 查数据集 |
| 算法 | `/algorithms/api/` | `GET /algorithms/api/versions` 查版本 |
| 推流 | `/streaming/api/` | `POST /streaming/api/tasks` 创建推流任务 |
| AI 助手 | `/assistant/api/` | `POST /assistant/api/chat` 聊天 |
| 自动标注 | `/auto-annotation/api/` | `POST /auto-annotation/api/start` 启动标注 |
| API 配置 | `/api-config/api/` | `GET /api-config/api/config` 查配置 |

### REST API v1（/api/v1/）

新版 RESTful API，统一前缀 `/api/v1`，采用标准 HTTP 状态码和统一响应格式。已落地的模块：

| 模块 | 端点前缀 | 主要端点 |
|------|----------|----------|
| 告警数据集 | `/api/v1/alerts/datasets` | CRUD、图片管理（列表/上传/导入/批量删除/日志/下载） |
| 告警图片 | `/api/v1/alerts/images/<id>` | 详情、文件、修改标签、删除 |
| 告警评测集 | `/api/v1/alerts/eval-sets` | CRUD、批量添加/移除数据集 |
| 告警 OCR | `/api/v1/alerts/...` | 单张 OCR、手动保存、批量 OCR、进度查询、取消 |
| 视频 | `/api/v1/videos` | 列表、上传、删除、下载 |
| 视频评测集 | `/api/v1/videos/eval-sets` | 列表、创建 |

**示例：**

```bash
# 查询告警数据集列表
curl http://localhost:8080/api/v1/alerts/datasets

# 对单张图片执行 OCR
curl -X POST http://localhost:8080/api/v1/alerts/images/42/ocr

# 批量 OCR（后台执行）
curl -X POST http://localhost:8080/api/v1/alerts/datasets/1/ocr:batch \
  -H "Content-Type: application/json" \
  -d '{"force_all": false, "stop_on_failure": false}'

# 查询批量 OCR 进度
curl http://localhost:8080/api/v1/alerts/datasets/1/ocr-status
```

> v1 API 使用 Google AIP 风格的 `:verb` 后缀（如 `ocr:batch`、`images:import`），统一响应格式为 `{"data": ..., "meta": ...}`。详细参数见 `app/api/v1/*.py`。

---

## 七、配置文件参考

| 文件 / 路径 | 用途 |
|-------------|------|
| `config/alert_types.json` | 告警类型 ID → 事件类型 key 映射（纯文本，每行 `"id key"`） |
| `ground_truth/<视频ID>.json` | Ground Truth 事件标注（`file`/`id`/`events[]`，每事件含 `type`/`start`/`end`） |
| `scripts/process_single.py` | FFmpeg 水印参数（字体、字号、位置、编码、CRF、GOP） |
| `app/config.py` | Flask 配置：上传路径、大小限制、允许的扩展名 |
| `app/auto_anno_config.json` | 自动标注 VLM API 配置（API Key、Base URL、模型名、请求间隔） |

**关键配置项（app/config.py）：**

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `UPLOAD_VIDEOS` | `uploads/videos` | 原始视频上传目录 |
| `OUTPUT_DIR` | `output` | 打水印视频输出目录 |
| `GROUND_TRUTH_DIR` | `ground_truth` | Ground Truth JSON 目录 |
| `GENERATED_VIDEOS_DIR` | `generated_videos` | 拼接/打包生成的视频目录 |
| `ALERT_TYPES_CONFIG` | `config/alert_types.json` | 告警类型配置文件 |
| `ALLOWED_VIDEO_EXTENSIONS` | `{mp4, avi, mov, mkv}` | 允许的视频格式 |
| `ALLOWED_IMAGE_EXTENSIONS` | `{png, jpg, jpeg, gif, bmp}` | 允许的图片格式 |

**数据库：** SQLite（`benchmark.db`），通过 `app/database.py` 初始化，`row_factory = sqlite3.Row`。核心表：`videos` → `watermarked_videos` → `events` / `gt_frames`；`datasets` → `alert_images` → `ocr_results`；`eval_tasks` → `eval_merged_events` / `eval_gt_events` / `eval_results`。
