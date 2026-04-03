# 视频数据预处理与告警评估平台

本项目是一个视频数据预处理、告警事件解析与评估的完整工作流平台：
- 为视频添加可识别水印（含视频 ID 和时间戳）
- 管理视频并标注事件，自动生成 ground truth
- 创建告警图片数据集，支持从 ZIP 批量导入
- 通过 OCR 从告警图片中提取视频水印信息（视频 ID、时间戳）
- 支持评估告警与 ground truth 的匹配

---

## 功能特性

### 视频管理
- 上传原始视频，可重命名、下载
- 设置 **10 位数字视频 ID**（用于水印）
- 对视频添加文字水印（左上角显示 `{视频ID} | {时间戳}`）
- 为视频标注事件，自动生成 `ground_truth/{视频ID}.json`
- 搜索功能（按文件名、视频 ID 或事件类型）

### 告警数据集管理
- 创建数据集，支持从 ZIP 批量导入告警图片
- 数据集内图片以 8 列网格展示
- 从文件名自动识别事件类型（支持横线/下划线分隔）
- 支持单张/批量 OCR（EasyOCR 或 PaddleOCR），提取视频 ID 和时间戳
- 可查看图片详情（尺寸、文件大小、OCR 结果等）

---

## 目录说明

```
├── app/                  # Flask Web 应用
│   ├── __init__.py
│   ├── config.py
│   ├── database.py
│   ├── routes/           # API 路由
│   │   ├── videos.py      # 视频管理接口
│   │   ├── alerts.py      # 告警数据集接口
│   │   └── verification.py # OCR/验证接口
│   ├── services/         # 业务逻辑
│   ├── templates/        # 前端模板
│   └── static/
├── scripts/              # 命令行脚本
│   ├── process_single.sh    # 单个视频打水印
│   ├── batch_process.sh     # 批量视频打水印
│   ├── ocr_easy.py          # EasyOCR 水印识别
│   ├── final_ocr.py         # PaddleOCR 水印识别
│   └── verify_alert.py      # 告警验证脚本
├── config.sh             # 水印样式配置
├── process.sh            # 视频处理入口脚本
├── run.py                # Flask 服务启动
├── requirements.txt      # 基础依赖
├── requirements-flask.txt # Web 平台依赖
└── requirements-ocr.txt  # OCR 引擎依赖
```

---

## 上传到 GitHub

### 要上传的文件夹/文件：
- `app/`
- `scripts/`
- `config.sh`
- `process.sh`
- `run.py`
- `requirements*.txt`
- `README.md`
- `CLAUDE.md`

### 不要上传的文件夹（数据相关）：
- `video1/`、`video2/`（原始视频）
- `output/`（打水印后的视频）
- `uploads/`（用户上传的视频/图片）
- `report/`（告警图片）
- `ground_truth/`（标注文件）
- `benchmark.db`（SQLite 数据库）
- `__pycache__/`（Python 缓存）
- `*.pyc`、`*.pyo`
- `.DS_Store`（macOS）

---

## .gitignore 示例

```gitignore
# 数据和媒体文件
video1/
video2/
output/
uploads/
report/
ground_truth/

# 数据库
*.db
*.sqlite
*.sqlite3

# Python 缓存
__pycache__/
*.pyc
*.pyo

# macOS
.DS_Store

# IDE
.vscode/
.idea/
```

---

## 快速开始

### 安装依赖
```bash
# 基础依赖（视频处理、QR码）
pip install -r requirements.txt

# Web 平台依赖
pip install -r requirements-flask.txt

# OCR 引擎（选择其一安装即可）
pip install -r requirements-ocr.txt
```

### 启动 Web 平台
```bash
python run.py
```
访问 http://localhost:8080

### 命令行使用
```bash
# 安装系统依赖
./process.sh --install

# 处理单个视频（添加水印）
./process.sh --single video1/046-3.30-18:16.mp4

# 批量处理所有视频
./process.sh --batch
```

---

## OCR 水印识别规则

- **视频 ID**：10 位连续数字
- **时间戳**：`HH:MM:SS` 或带毫秒的 `HH:MM:SS.xxx`
- **裁剪区域**：左上角 700×120px
- **预处理**：转灰度 → 增强对比度（2.5×）→ 反色
