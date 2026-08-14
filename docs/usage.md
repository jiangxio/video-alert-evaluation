# 使用指南

本平台提供两种交互方式：Web 可视化页面（推荐）与命令行脚本。

## 一、Web 平台

启动后访问 `http://localhost:8080`，主要功能入口：

### 视频管理（/videos/）
- 上传原始视频，设置 10 位数字视频 ID
- 打水印（FFmpeg drawtext，左上角 `{视频ID} | {时间戳}`）
- 为视频标注事件区间，自动生成 ground truth + GT 帧
- 搜索（按文件名、视频 ID、事件类型）

### 告警数据集（/alerts/）
- 创建数据集，支持从 ZIP 批量导入告警图片
- 8 列网格浏览，从文件名自动识别事件类型
- 单张/批量 OCR（EasyOCR），提取视频 ID 和时间戳
- 查看图片详情（尺寸、OCR 结果等）

### 评测（/evaluation/）
- 创建评测任务，配置参数（合并间隔、事件起止、触发率）
- 执行评测：OCR 告警图片 → 与真值时间窗口匹配 → 命中/误检判定
- 确认结果（调整 `confirmed_count` 等参数）
- 计算指标：精确率、召回率、平均误检数/小时
- 生成算法验证报告（HTML/PDF）

### 算法管理（/algorithms/）
- 注册算法版本（配置文件、可执行文件）
- 事件类型（算法类型）管理
- 版本对比与下载

### 其他功能
- **AI 助手**（/assistant/）：自然语言操作平台
- **自动标注**（/auto-annotation/）：VLM 辅助逐帧分析，自动生成事件标注
- **推流**（/streaming/）：RTSP 推流到 MediaMTX 模拟实时视频流
- **API 配置**（/api-config/）：统一管理 OpenAI/Claude API 凭据
- **测前分析**（/evaluation/pre-analysis）：评测前预览事件区间

## 二、命令行脚本

### 视频水印处理

```bash
# 安装依赖
python process.py --install

# 处理单个视频（添加水印）
python process.py --single video1/0514000003.mp4

# 批量处理所有视频
python process.py --batch

# 指定输出目录
python scripts/process_single.py video.mp4 --output-dir /path/to/output
```

视频 ID 提取规则：文件名 `{视频ID}-{其他}.mp4`，如 `046-3.30-18:16.mp4` 提取 `046`。不含 `-` 则整个文件名（去扩展名）作视频 ID。

### OCR 与验证

```bash
# 单张告警图片 OCR
python scripts/ocr_easy.py report/402_1774925112_103.png

# 验证单张告警（真实 OCR）
python scripts/verify_alert.py report/402_1774925112_103.png

# 用 mock OCR 测试（无需 GPU/OCR 依赖）
python scripts/verify_alert.py report/402_1774925112_103.png \
  --mock-ocr '{"video_id": "046", "timestamp_seconds": 90}'

# 批量验证所有告警图片
python scripts/verify_alert.py --batch
```

### 验证流水线

```
告警图片文件名 → 提取告警类型 ID → 查 config.json 事件类型 → OCR 水印 →
加载 ground_truth/{video_id}.json → 检查时间戳 ±5s 是否与匹配事件重叠 →
判定：correct / incorrect / unknown
```

## 三、OCR 识别规则

- **视频 ID**：10 位连续数字
- **时间戳**：`HH:MM:SS` 或带毫秒 `HH:MM:SS.xxx`
- **裁剪区域**：左上角 700×120px（水印位置）
- **预处理**：转灰度 → 增强对比度（2.5×）→ 反色（黑底白字→白底黑字）

## 四、REST API

除 Web 界面外，所有功能均提供 REST API，便于集成与自动化。主要端点：

| 模块 | 端点前缀 | 示例 |
|------|----------|------|
| 评测 | /evaluation/api/ | `GET /evaluation/api/tasks` 查任务列表 |
| 视频 | /videos/api/ | 上传、打水印、标注 |
| 告警 | /alerts/api/ | 数据集、OCR、图片文件 |
| 算法 | /algorithms/api/ | 版本管理、事件类型 |
| 推流 | /streaming/api/ | 创建/停止推流任务 |

API 返回 JSON，详细参数见各路由代码 `app/routes/*.py`。
