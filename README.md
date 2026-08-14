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

## 文档导航

完整文档见 `docs/` 目录：

| 文档 | 内容 |
|------|------|
| [安装指南](docs/install.md) | Docker 部署与手动安装 |
| [接入指南](docs/integration.md) | 算法版本注册、数据格式、评测闭环 |
| [使用指南](docs/usage.md) | Web 平台与命令行操作手册 |
| [故障排查](docs/troubleshooting.md) | 常见问题与解决方案 |

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
├── scripts/              # 命令行脚本（跨平台 Python）
│   ├── process_single.py    # 单个视频打水印
│   ├── batch_process.py     # 批量视频打水印
│   ├── ocr_easy.py          # EasyOCR 水印识别
│   └── verify_alert.py      # 告警验证脚本
├── process.py            # 视频处理入口脚本（跨平台）
├── run.py                # Flask 服务启动
└── requirements.txt      # 所有 Python 依赖
```

---

## 上传到 GitHub

### 要上传的文件夹/文件：
- `app/`
- `scripts/`
- `process.py`
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

### 方式一：Docker 部署（推荐）

仅需修改 `.env` 一个配置文件即可运行：

```bash
# 1. 复制配置文件并填入 API key（唯一需修改的配置）
cp .env.example .env

# 2. 一键构建并启动
docker compose up -d

# 3. 访问 http://localhost:8080（视频评测平台）
#    目标检测评测服务：http://localhost:5000（或从导航「目标检测」跳转）
```

`docker compose up -d` 同时启动两个服务：
- **web**（8080）：视频水印评测平台
- **od**（5000）：目标检测评测服务（独立 Flask app，与 web 完全解耦）

常用命令：

```bash
docker compose logs -f   # 查看实时日志
docker compose down      # 停止（数据保留在命名卷中）
docker compose up -d     # 重新启动（数据不丢失）
docker compose stop od   # 仅停目标检测服务（不影响视频评测）
```

> 镜像已内置 FFmpeg、字体、EasyOCR 模型（离线可用）、Chromium（PDF 报告）。
> 数据持久化：数据库与各数据目录通过 Docker 命名卷保存，重建容器不丢数据。
> 可选：取消 `docker-compose.yml` 中 `./config:/app/config:ro` 注释，用宿主配置覆盖默认告警类型。

### 方式二：手动安装

#### 安装依赖
```bash
pip install -r requirements.txt
```

> **macOS 用户注意**：Homebrew 默认安装的 FFmpeg **不包含** `drawtext` 滤镜所需的 `libfreetype` 支持，运行打水印时会报错。建议从 conda-forge 安装完整版 FFmpeg：
> ```bash
> conda install -c conda-forge ffmpeg
> ```
> 并确保 conda 环境的 `ffmpeg` 优先于 Homebrew 版本（`which ffmpeg` 应指向 conda 路径）。

### 启动 Web 平台
```bash
python run.py
```
访问 http://localhost:8080

### 命令行使用（Linux/macOS/Windows 通用）
```bash
# 安装依赖
python process.py --install

# 处理单个视频（添加水印）
python process.py --single video1/046-3.30-18:16.mp4

# 批量处理所有视频
python process.py --batch
```

---

## 部署注意事项

### 1. FFmpeg 必须安装并加入 PATH

打水印和抽帧都依赖 FFmpeg，安装后确保命令行能直接调用：

```bash
ffmpeg -version
ffprobe -version
```

> **macOS 用户注意**：Homebrew 默认安装的 FFmpeg **不包含** `drawtext` 滤镜所需的 `libfreetype` 支持，运行打水印时会报错。建议从 conda-forge 安装完整版 FFmpeg：
> ```bash
> conda install -c conda-forge ffmpeg
> ```
> 并确保 conda 环境的 `ffmpeg` 优先于 Homebrew 版本（`which ffmpeg` 应指向 conda 路径）。

### 2. 字体自动查找

打水印需要字体文件，`scripts/process_single.py` 会按操作系统自动查找常见字体路径：

| 系统 | 查找路径 |
|------|---------|
| Linux | `/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf` |
| macOS | `/System/Library/Fonts/Helvetica.ttc` 等 |
| Windows | `C:/Windows/Fonts/arial.ttf` 等 |

如果找不到字体，会报错。你可以修改 `scripts/process_single.py` 中的 `_FONT_CANDIDATES` 列表添加自己的字体路径。

### 3. 视频源目录

批量处理（`python process.py --batch`）默认递归查找项目根目录下所有 `.mp4` 文件，排除 `output/` 目录。如需修改源目录，直接编辑 `scripts/batch_process.py` 中的 `find_videos()` 函数。

### 4. 输出目录

默认输出到项目根目录的 `output/` 文件夹。如需修改，可通过 `--output-dir` 参数指定：

```bash
python scripts/process_single.py video.mp4 --output-dir /path/to/output
```

### 5. 视频 ID 规则

文件名格式为 `{视频ID}-{其他信息}.mp4`，如 `046-3.30-18:16.mp4`，系统会自动提取 `046` 作为视频 ID。如果文件名不含 `-`，则整个文件名（不含扩展名）会被当作视频 ID。

---

## OCR 水印识别规则

- **视频 ID**：10 位连续数字
- **时间戳**：`HH:MM:SS` 或带毫秒的 `HH:MM:SS.xxx`
- **裁剪区域**：左上角 700×120px
- **预处理**：转灰度 → 增强对比度（2.5×）→ 反色
