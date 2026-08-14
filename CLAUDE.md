# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Developer Workflow

### 提交代码前必须征得用户同意

**规则**：任何 `git commit` 或 `git push` 操作前，必须先向用户展示变更摘要并征得明确同意（如用户说"上库"、"提交"、"push"等）。

**禁止**：
- 未经用户确认就自动创建 commit
- 未经用户确认就 push 到远程仓库
- 把代码改动偷偷混在其他操作里提交

**正确流程**：
1. 完成代码修改后，展示 `git diff --stat` 或变更摘要
2. 等待用户明确同意（"上库"、"提交吧"、"push"等）
3. 征得同意后再执行 `git add` → `git commit` → `git push`

## Project Overview

A video watermark benchmarking tool that tests OCR capabilities to extract video IDs and timestamps from video watermarks. The project has two interfaces: a CLI pipeline and a Flask web platform.

## Common Commands

### Video Watermarking (CLI)
```bash
python process.py --install          # Install dependencies
python process.py --single video1/046-3.30-18:16.mp4  # Watermark single video
python process.py --batch            # Watermark all videos in video1/ and video2/
```

### OCR and Verification (CLI)
```bash
# Verify single alert image (with real OCR)
python scripts/verify_alert.py report/402_1774925112_103.png

# Verify with mock OCR (for testing without GPU/OCR dependencies)
python scripts/verify_alert.py report/402_1774925112_103.png --mock-ocr '{"video_id": "046", "timestamp_seconds": 90}'

# Batch verify all alert images
python scripts/verify_alert.py --batch

# Run EasyOCR directly on an image
python scripts/ocr_easy.py report/402_1774925112_103.png
```

### Web Platform
```bash
source .venv/bin/activate            # 进入虚拟环境
python run.py                        # Starts Flask on 0.0.0.0:8080
```

## Architecture

### Two Interfaces, Shared Scripts

The CLI and web platform both call the same underlying scripts via subprocess. The Flask services (`app/services/`) are thin wrappers that exec the CLI scripts:
- `watermark_service.add_watermark()` → execs `scripts/process_single.py`
- `verification_service.run_ocr()` → execs `scripts/ocr_easy.py`
- `verification_service.verify_alert()` → execs `scripts/verify_alert.py`

### Verification Pipeline

Alert image filename → extract alert type ID → look up event type in `report/config.json` → run OCR on watermark → load `ground_truth/{video_id}.json` → check if OCR timestamp ±5s overlaps any matching event → verdict: `correct` / `incorrect` / `unknown`

The timestamp tolerance is 5 seconds. Alert filenames follow `{prefix}_{unix_ts}_{alert_type_id}.png`.

### OCR Image Preprocessing

Both `ocr_easy.py` and `final_ocr.py` apply the same preprocessing before OCR:
1. Crop top-left 380×100px (watermark location)
2. Convert to grayscale
3. Enhance contrast (2.5×)
4. Invert colors (white text on black → black on white)

### Flask Web Platform (`app/`)

App factory pattern in `app/__init__.py`. SQLite database (`benchmark.db`) initialized via `app/database.py`. Three route blueprints:
- `app/routes/videos.py` — upload, list, watermark videos
- `app/routes/alerts.py` — upload, list alert images
- `app/routes/verification.py` — run OCR, verify, batch verify

The DB schema tracks the full lifecycle: `videos` → `watermarked_videos`, `alert_images` → `ocr_results` → `verification_results`. Ground truth is also imported into the DB from `ground_truth/*.json`.

### Watermark Format

FFmpeg `drawtext` filter adds `{VIDEO_ID} | {HH:MM:SS}` at position (20, 20) in 32px DejaVuSans-Bold white text with a semi-transparent black background. Settings are in `scripts/process_single.py`.

## Configuration Files

| File | Purpose |
|------|---------|
| `scripts/process_single.py` | FFmpeg/font settings for watermarking (font candidates, size, position, codec) |
| `report/config.json` | Alert type ID → event type name mapping (format: `"id name"` per line) |
| `ground_truth/{video_id}.json` | Ground truth events with type, start, end timestamps |
| `app/config.py` | Flask config: upload paths, size limits, allowed extensions |

## Dependencies

All Python deps consolidated in a single file:
- `requirements.txt` — Flask, Werkzeug, waitress (web), easyocr (OCR), Pillow (image processing)

External: FFmpeg must be installed for watermarking. `python process.py --install` handles Python deps.

## Important Notes

### sqlite3.Row 对象没有 .get() 方法

**问题**：代码中多次出现 `'sqlite3.Row' object has no attribute 'get'` 错误。

**原因**：数据库连接使用 `row_factory = sqlite3.Row`（见 [app/database.py:16](app/database.py#L16)），`sqlite3.Row` 对象支持字典式索引访问（`row['key']`），但**不支持** `.get()` 方法。

**正确写法**：
```python
# 错误 ❌
value = row.get('key', default)

# 正确 ✅
value = row['key'] if row['key'] is not None else default

# 或者先转换为字典 ✅
row_dict = dict(row)
value = row_dict.get('key', default)
```

**常见场景**：
- 从 `eval_tasks` 表读取任务参数时
- 从 `events`、`videos` 等表读取数据时
- 任何使用 `cursor.fetchone()` 或 `cursor.fetchall()` 获取 `sqlite3.Row` 对象的地方

### 跨平台兼容与路径规范

**原则**：代码需兼容 Linux / macOS / Windows，避免硬编码与运行设备强相关的绝对路径。

**要求**：
- 使用 `pathlib.Path` 处理路径，避免字符串拼接路径分隔符。
- 配置文件中的路径优先使用相对路径或可通过环境变量覆盖。
- 如果存在与特定设备/环境强相关的路径（如 `/userdata/nvr_warn_assets/`），必须在 `README.md` 中说明该路径的用途及如何修改为适配当前环境的值。

**示例**：
```python
# 推荐 ✅
from pathlib import Path
base_dir = Path(__file__).resolve().parent
assets_path = Path(os.environ.get("WARN_ASSETS_DIR", "./assets"))

# 不推荐 ❌
assets_path = "/userdata/nvr_warn_assets/"
```

---

## 评测核心指标计算逻辑（evaluation.py）

这是项目的核心统计逻辑，涉及后端 `app/routes/evaluation.py` 和前端 `app/templates/eval_task.html`，修改时极易牵一发而动全身。以下记录各环节的精确语义和常见陷阱。

### 1. 执行评测阶段（execute_task）

**命中判定（单张告警 → 命中/误检）**

- 条件：`video_id` 相同 AND `event_type` 相同 AND 时间窗口与 GT 事件重叠（±5s 容差）
- 重叠判定（三种情况满足其一即可）：
  1. `ts_start` 落在 GT 容差区间 `[start_sec-5, end_sec+5]`
  2. `ts_end` 落在 GT 容差区间
  3. 告警窗口完全覆盖 GT 容差区间（`ts_start <= g_start` 且 `ts_end >= g_end`）
- **关键约束**：命中判定只看时间重叠，**不限次数**。同个 GT 事件区间内可以有 N 张告警图命中，每张都独立标记为命中（`is_fp=False`）。
- `gt_hit_counts` 只用于统计每个 GT 事件被命中了多少次，写入 `actual_count`，**不影响**单张告警的命中判定。

**实际触发次数（actual_count）**

- 评测结束后，每个 GT 事件的 `actual_count = gt_hit_counts[gt_id]`
- 这是**真实命中次数**，不受 `confirmed_count` 限制。

### 2. 确认阶段（finalize_task）—— 指标计算

**有效状态（`_get_effective_status`）**

- `manual_status` 优先级高于 `is_false_positive`：
  - `'correct'` / `'false_positive'` / `'ignored'` → 直接生效
  - `'auto'` 或未设置 → 以 `is_false_positive` 为准
- **所有统计指标都必须通过 `_get_effective_status` 获取最终状态**，不能直接读 `is_false_positive`。

**精确率（Precision）**

```
precision = correct_pred_count / alert_count
```

- `alert_count`：有效状态 ≠ `ignored` 的告警总数
- `correct_pred_count`：有效状态 = `correct` 的告警数
- 只和告警的有效状态有关，和 `confirmed_count` **无关**。

**召回率（Recall）**

按事件类型分组，各类型召回率的**算术平均**（不是加权平均）：

```
event_recall = hit_count / gt_count        # 每个事件类型
recall = average(event_recall for all types with gt_count > 0)
```

每个事件类型的 `gt_count` 和 `hit_count` 计算规则：

| confirmed | actual | gt_count | hit_count | 语义 |
|-----------|--------|----------|-----------|------|
| 0 | 0 | 忽略（不计入） | — | 不预期且未触发，跳过 |
| 0 | >0 | 1 | 1 | 不预期但触发了，按1次算 |
| >0 | 任意 | confirmed | min(actual, confirmed) | 预期N次，封顶N次 |

**关键约束**：`hit_count = min(actual, confirmed)` 只在召回率计算时做封顶，**不影响**单张告警的命中/误检判定。

**平均误检数/小时**

```
avg_fp_per_hour = average(各事件类型的 avg_fp_per_hour)   # 算术平均（宏平均）
各事件类型 avg_fp_per_hour = 该类型误检数 / total_duration_hours
```

- `fp_count`：有效状态 = `false_positive` 的告警总数（所有事件类型合计，仅用于整体精确率分母）
- `total_duration_hours`：评测涉及的视频总时长（小时）
- **口径与召回率一致**：各事件类型分别算误检/小时，再算术平均（不是合计/总时长）。这与 realtime 模式、验收报告口径统一。
- 代码实现见 `eval_service.py` 的 `compute_overall_avg_fp()`，普通模式与 realtime 模式均走此逻辑。

### 3. 前端重算（eval_task.html）

- `recalcMetrics()` 函数与后端 `finalize_task` 逻辑保持一致
- 修改 `confirmed_count` 后前端实时重算召回率，但不重算精确率
- 前端过滤逻辑（miss/hit）也使用相同的 confirmed/actual 规则

### 4. 常见陷阱（修改前必读）

1. **不要把召回率的封顶逻辑放到命中判定里**。命中判定只看时间重叠，召回率才用 `min(actual, confirmed)` 封顶。`cc16cc5` 曾错误地把超出 confirmed_count 的告警改判为误检，已修复（见 `evaluation.py:613-617`）。
2. **`confirmed_count == 0` 不是"预期0次触发"**。它的语义是"不主动预期，但如果实际触发了，`gt_count` 和 `hit_count` 都按1算"。
3. **整体召回率是算术平均**。各事件类型的召回率简单相加后除以类型数，不按GT事件数量加权。
4. **所有指标统计必须走 `_get_effective_status`**。直接读 `is_false_positive` 会漏掉用户手动覆盖的状态。
5. **修改 `confirmed_count` 只影响召回率，不影响精确率**。精确率取决于单张告警的有效状态（命中/误检），而这个状态在评测执行阶段就已确定。
