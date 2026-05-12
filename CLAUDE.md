# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A video watermark benchmarking tool that tests OCR capabilities to extract video IDs and timestamps from video watermarks. The project has two interfaces: a CLI pipeline and a Flask web platform.

## Common Commands

### Video Watermarking (CLI)
```bash
./process.sh --install          # Install dependencies
./process.sh --single video1/046-3.30-18:16.mp4  # Watermark single video
./process.sh --batch            # Watermark all videos in video1/ and video2/
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
pip install -r requirements-flask.txt
python run.py    # Starts Flask on 0.0.0.0:8080
```

## Architecture

### Two Interfaces, Shared Scripts

The CLI and web platform both call the same underlying scripts via subprocess. The Flask services (`app/services/`) are thin wrappers that exec the CLI scripts:
- `watermark_service.add_watermark()` → execs `scripts/process_single.sh`
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

FFmpeg `drawtext` filter adds `{VIDEO_ID} | {HH:MM:SS}` at position (20, 20) in 32px DejaVuSans-Bold white text with a semi-transparent black background. Settings are in `config.sh`.

## Configuration Files

| File | Purpose |
|------|---------|
| `config.sh` | FFmpeg/font settings for watermarking (font, size, position, codec) |
| `report/config.json` | Alert type ID → event type name mapping (format: `"id name"` per line) |
| `ground_truth/{video_id}.json` | Ground truth events with type, start, end timestamps |
| `app/config.py` | Flask config: upload paths, size limits, allowed extensions |

## Dependencies

Three separate requirements files — install only what you need:
- `requirements.txt` — core (qrcode, Pillow)
- `requirements-flask.txt` — web platform (Flask, Werkzeug)
- `requirements-ocr.txt` — OCR backend (choose PaddleOCR **or** EasyOCR **or** Tesseract)

External: FFmpeg must be installed for watermarking. `process.sh --install` handles Python deps.

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
