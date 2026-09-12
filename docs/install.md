# 安装指南

视频流AI算法评测工具提供两种安装方式：**Docker 部署（推荐）** 与 **手动安装**。前者镜像内置全部依赖（FFmpeg、字体、EasyOCR 模型、Chromium），开箱即用；后者适用于无 Docker 环境或需要本地开发调试的场景。

## 方式选择

| 维度 | Docker 部署 | 手动安装 |
|------|-------------|----------|
| 耗时 | 首次构建约 10–20 分钟，后续秒级启动 | 视网络情况，约 10–30 分钟 |
| 依赖 | 仅需 Docker + Docker Compose | 需自行安装 Python、FFmpeg、字体、Playwright |
| 配置 | 仅改 `.env` 一个文件 | 需逐步配置虚拟环境与系统依赖 |
| 适用 | 生产/参赛现场/快速部署 | 本地开发/调试/无 Docker 环境 |

---

## 方式一：Docker 部署（推荐）

镜像内置 CPU 版 PyTorch、EasyOCR（含预下载英文模型）、Playwright Chromium、FFmpeg（含 `libfreetype`）、DejaVuSans-Bold 与 Noto CJK 字体，参赛现场无外网也能正常 OCR。

### 前置条件

| 依赖 | 版本 | 说明 |
|------|------|------|
| Docker | 20.10+ | 引擎 |
| Docker Compose | v2+ | 随 Docker Desktop 或插件安装 |
| 端口 | 8080、5000 可用 | 8080 主平台，5000 目标检测评测服务 |

### 部署步骤

```bash
# 1. 克隆仓库
git clone <仓库地址>
cd video-alert-evaluation

# 2. 复制配置文件并填入 API key（唯一需修改的配置文件）
cp .env.example .env
# 编辑 .env，填入 OPENAI_API_KEY、VISION_API_KEY、ASSISTANT_ENCRYPTION_KEY 等

# 3. 一键构建并启动（后台）
docker compose up -d --build

# 4. 访问 http://localhost:8080
```

`.env` 需填写的变量见下文「环境变量说明」。首次构建会下载 PyTorch CPU 版（约 200MB）、EasyOCR 模型（约 100MB）与 Chromium（约 300MB），请耐心等待。

### 常用命令

```bash
docker compose up -d            # 启动（后台）
docker compose logs -f          # 查看实时日志
docker compose ps               # 查看容器状态与健康检查
docker compose down             # 停止并移除容器（数据保留在命名卷）
docker compose up -d --build    # 代码变更后重新构建并启动
docker compose restart web      # 仅重启主平台
```

### 数据持久化

数据库与各数据目录通过 Docker 命名卷保存，`docker compose down` 不会删除数据，重建容器后数据完整保留。

**主平台（web 服务，端口 8080）**

| 命名卷 | 容器挂载路径 | 用途 |
|--------|-------------|------|
| `db_data` | `/app/data` | `benchmark.db` 数据库（`DATABASE_PATH=/app/data/benchmark.db`） |
| `uploads_data` | `/app/uploads` | 上传的视频与告警图片 |
| `ground_truth_data` | `/app/ground_truth` | 真值标注 JSON 文件 |
| `output_data` | `/app/output` | 打水印后的视频 |
| `thumbnails_data` | `/app/thumbnails` | 视频缩略图 |
| `generated_videos_data` | `/app/generated_videos` | 评测流程生成的视频 |
| `extracted_frames_data` | `/app/extracted_frames` | 抽帧结果 |
| `auto_annotation_frames_data` | `/app/auto_annotation_frames` | 自动标注用帧 |
| `logs_data` | `/app/logs` | 运行日志 |
| `report_data` | `/app/report` | 评测报告（含 PDF） |

**目标检测评测服务（od 服务，端口 5000）**

| 命名卷 | 容器挂载路径 | 用途 |
|--------|-------------|------|
| `od_db` | `/app/data` | od-dataset-manager 数据库（`OD_DB_DIR=/app/data`） |
| `od_datasets` | `/app/datasets` | 目标检测数据集 |

> 如需彻底清除数据：`docker compose down -v` 会一并删除上述命名卷，请谨慎使用。

### 自定义端口

修改 `docker-compose.yml` 中 `web` 服务的端口映射，将 `"8080:8080"` 改为 `"9090:8080"` 即可改用 9090 端口访问。格式为 `宿主端口:容器端口`，容器内端口固定为 8080（`run.py` 通过 waitress 监听 `0.0.0.0:8080`）。od 服务同理，改 `"5000:5000"`。

### 覆盖告警类型配置

镜像内置默认告警类型配置位于 `/app/config/alert_types.json`（对应 `app/config.py` 的 `ALERT_TYPES_CONFIG`）。如需用自定义配置覆盖，取消 `docker-compose.yml` 中 `web` 服务下该行的注释：

```yaml
volumes:
  # ...
  - ./config:/app/config:ro   # 取消注释，用宿主 ./config 覆盖镜像内默认配置
```

取消注释后，在宿主仓库根目录创建 `config/alert_types.json`，重启容器即可生效。

### 环境变量说明（`.env`）

| 变量 | 必填 | 说明 |
|------|------|------|
| `OPENAI_API_KEY` | 是 | 文本逻辑组 API key（OpenAI 兼容协议），供 AI 助手、报告生成使用 |
| `OPENAI_BASE_URL` | 否 | 默认 `https://api.openai.com/v1`，可改为兼容服务地址 |
| `OPENAI_MODEL` | 否 | 默认 `gpt-4o-mini` |
| `VISION_API_KEY` | 否 | 多模态审查组 API key，需支持图片输入的 VL 模型；未填时回退文本逻辑组 |
| `VISION_BASE_URL` | 否 | 默认 `https://api.openai.com/v1` |
| `VISION_MODEL` | 否 | 默认 `Qwen3-VL-8B-Instruct` |
| `ASSISTANT_ENCRYPTION_KEY` | 是 | 加密存储数据库中 API Key 的密钥，32 字节 base64 |
| `ASSISTANT_MAX_MESSAGES_PER_SESSION` | 否 | 默认 50 |
| `ASSISTANT_MAX_WRITE_ACTIONS_PER_SESSION` | 否 | 默认 30 |
| `ASSISTANT_CONFIRMATION_TTL_SECONDS` | 否 | 默认 300 |

`ASSISTANT_ENCRYPTION_KEY` 生成方式：

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

---

## 方式二：手动安装

适用于无 Docker 环境或需要本地开发调试的场景。

### 前置条件

| 依赖 | 版本 | 说明 |
|------|------|------|
| Python | 3.10+ | 推荐 3.10 / 3.11，镜像基于 3.10-slim-bullseye |
| FFmpeg | 任意稳定版 | 必须含 `drawtext` 滤镜（依赖 `libfreetype`） |
| 字体 | DejaVuSans-Bold | 打水印用，Linux 通常随 `fonts-dejavu` 安装 |
| 中文字体 | Noto CJK | 报告/页面渲染中文，建议安装 `fonts-noto-cjk` |

### 1. 安装 FFmpeg（含 drawtext / libfreetype 支持）

```bash
# Linux (Debian/Ubuntu) —— 一并安装字体
sudo apt-get update
sudo apt-get install -y ffmpeg fonts-dejavu fonts-noto-cjk

# macOS —— 需 conda-forge 完整版（Homebrew 版缺 libfreetype，drawtext 不可用）
conda install -c conda-forge ffmpeg
which ffmpeg          # 应指向 conda 路径
```

验证 FFmpeg 可用且含 `drawtext`：

```bash
ffmpeg -version
ffprobe -version
ffmpeg -filters | grep drawtext    # 应输出 drawtext 滤镜
```

### 2. 创建虚拟环境并安装依赖

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
```

EasyOCR 依赖 PyTorch。为避免 `pip install easyocr` 拉取庞大的 CUDA 版 torch，**先单独安装 CPU 版 torch**（与 Dockerfile 一致的做法），再装其余依赖：

```bash
# 1) 先装 CPU 版 torch/torchvision（评测现场无需 GPU）
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu

# 2) 再装其余依赖（显式指定 PyPI 索引，避免回退到 torch 索引找不到 Flask）
pip install -r requirements.txt --index-url https://pypi.org/simple/
```

> `requirements.txt` 中 `playwright==1.49.1` 为固定版本：1.62 起不再支持 Debian 11 (bullseye)，固定 1.49 以兼容镜像基础环境。

### 3. 预装 Playwright 浏览器（PDF 报告生成依赖）

```bash
playwright install chromium
```

> 若在无图形界面的 Linux 服务器运行，需额外装系统依赖：`playwright install --with-deps chromium`。

### 4. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`，填入 `OPENAI_API_KEY`、`VISION_API_KEY`（可选）与 `ASSISTANT_ENCRYPTION_KEY`（必填，生成方式见上文「环境变量说明」）。完整变量列表与默认值见 `.env.example` 注释。

### 5. 启动

```bash
python run.py
```

启动后终端会打印平台 Banner，随后通过 waitress 监听 `0.0.0.0:8080`（16 线程）。访问：

| 入口 | 地址 |
|------|------|
| 首页 | http://localhost:8080/ |
| 视频管理 | http://localhost:8080/videos/ |
| 告警图片 | http://localhost:8080/alerts/ |

### 字体路径说明

打水印脚本 `scripts/process_single.py` 的 `find_font()` 会按操作系统依次查找 `_FONT_CANDIDATES` 中列出的字体，返回第一个存在的路径。默认水印为 32px 白色 `DejaVuSans-Bold` 文字、黑底，位于左上角 (20, 20)（见 `scripts/process_single.py` 的 `DEFAULT_CONFIG`）。

| 操作系统 | 候选路径（按查找顺序） | 默认命中 |
|----------|----------------------|----------|
| Linux | `/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf`<br>`/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf` | 第二条（`fonts-dejavu` 包安装位置） |
| macOS (Darwin) | `/System/Library/Fonts/Helvetica.ttc`<br>`/Library/Fonts/Arial.ttf`<br>`/System/Library/Fonts/Supplemental/Arial.ttf` | 第一条 |
| Windows | `C:/Windows/Fonts/arial.ttf`<br>`C:/Windows/Fonts/segoeui.ttf`<br>`C:/Windows/Fonts/calibrib.ttf` | 第一条 |

若上述路径均不存在，`find_font()` 返回 `None`，打水印会报错 "找不到合适的字体文件"。此时可：

- 安装对应字体包（Linux：`fonts-dejavu`；macOS：系统自带 Helvetica；Windows：自带 Arial）；或
- 在 `scripts/process_single.py` 的 `_FONT_CANDIDATES` 列表中添加自定义字体路径。

---

## 安装验证

### Docker 部署验证

```bash
# 1. 容器状态与健康检查（STATUS 列应为 Up，并显示 healthy）
docker compose ps

# 2. 访问健康检查端点（compose 内置 healthcheck 即调用此地址）
curl -fsS http://localhost:8080/ && echo "OK"

# 3. 查看启动日志，确认 waitress 监听
docker compose logs web | grep -i "serving\|8080"
```

预期：`video-alert-eval` 与 `video-alert-od` 两个容器均为 `Up` 状态，主平台健康检查通过（`start_period: 40s` 内转为 healthy）。浏览器访问 http://localhost:8080/ 可看到平台首页。

### 手动安装验证

```bash
# 1. FFmpeg 可用且含 drawtext
ffmpeg -filters | grep drawtext

# 2. EasyOCR 模型可加载（首次会下载英文模型，约 100MB）
python -c "import easyocr; r=easyocr.Reader(['en'], gpu=False); print('EasyOCR 就绪')"

# 3. 启动平台
python run.py
# 终端打印 Banner 后，访问 http://localhost:8080/
```

### 常见问题

| 现象 | 原因与处理 |
|------|-----------|
| `drawtext` 滤镜不存在 | FFmpeg 编译未含 `libfreetype`，macOS 用 conda-forge 版，Linux 用包管理器版 |
| 打水印报 "找不到合适的字体文件" | 未安装 `fonts-dejavu`，或字体不在 `_FONT_CANDIDATES` 路径，按上文「字体路径说明」处理 |
| OCR 首次很慢 | EasyOCR 正在下载英文模型（约 100MB），Docker 镜像已预下载，手动安装需联网 |
| 容器健康检查一直 `unhealthy` | 查 `docker compose logs web`，常见为 `.env` 未填必填项或端口被占用 |
| `pip install` 找不到 Flask | 装 torch 时 `--index-url` 持久化到 pip 配置，装依赖时需显式加 `--index-url https://pypi.org/simple/` |
