# 安装指南

本平台提供两种安装方式：Docker 部署（推荐）和手动安装。

## 方式一：Docker 部署（推荐）

仅需修改 `.env` 一个配置文件即可运行，镜像内置全部依赖（FFmpeg、字体、EasyOCR 模型、Chromium）。

### 前置条件
- 已安装 Docker（20.10+）与 Docker Compose（v2+）
- 端口 8080 可用

### 步骤

```bash
# 1. 克隆仓库
git clone <仓库地址>
cd video-alert-evaluation

# 2. 复制配置文件并填入 API key（唯一需修改的配置）
cp .env.example .env
# 编辑 .env，填入 OPENAI_API_KEY、ANTHROPIC_AUTH_TOKEN 等

# 3. 一键构建并启动
docker compose up -d

# 4. 访问 http://localhost:8080
```

### 常用命令

```bash
docker compose up -d        # 启动（后台）
docker compose logs -f      # 查看实时日志
docker compose down         # 停止（数据保留在命名卷）
docker compose up -d --build # 代码变更后重新构建启动
```

### 数据持久化

数据库与各数据目录通过 Docker 命名卷保存，重建容器不丢数据：

| 卷 | 挂载路径 | 用途 |
|----|----------|------|
| db_data | /app/data | benchmark.db 数据库 |
| uploads_data | /app/uploads | 上传的视频/告警图片 |
| ground_truth_data | /app/ground_truth | 真值标注文件 |
| output_data | /app/output | 打水印后的视频 |
| 其他 | thumbnails/logs/report 等 | 缩略图、日志、报告 |

### 自定义端口

修改 `docker-compose.yml` 的端口映射：`"9090:8080"` 即改用 9090 端口。

### 覆盖默认告警类型配置

取消 `docker-compose.yml` 中 `./config:/app/config:ro` 的注释，用宿主 `config/` 覆盖镜像内默认告警类型。

---

## 方式二：手动安装

适用于无 Docker 环境或需要本地开发调试的场景。

### 前置条件

| 依赖 | 版本 | 说明 |
|------|------|------|
| Python | 3.10+ | 推荐 3.10/3.11 |
| FFmpeg | 任意稳定版 | 必须含 `drawtext` 的 `libfreetype` 支持 |
| 字体 | DejaVuSans-Bold | 打水印用，Linux 通常预装 |

### 1. 安装 FFmpeg

```bash
# Linux (Debian/Ubuntu)
sudo apt-get install -y ffmpeg fonts-dejavu

# macOS（需 conda-forge 完整版，Homebrew 版缺 libfreetype）
conda install -c conda-forge ffmpeg
which ffmpeg  # 应指向 conda 路径
```

验证：
```bash
ffmpeg -version
ffprobe -version
```

### 2. 创建虚拟环境并安装依赖

```bash
python -m venv .venv
source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

> **CPU 版 PyTorch**：EasyOCR 纯 CPU 即可运行，无需 GPU。如需节省空间，单独安装 CPU 版 torch：
> ```bash
> pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
> ```

### 3. 预装 Playwright 浏览器（PDF 报告）

```bash
playwright install chromium
```

### 4. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 填入 API key
```

### 5. 启动

```bash
python run.py
# 访问 http://localhost:8080
```

### 字体路径说明

打水印脚本 `scripts/process_single.py` 会按操作系统自动查找 `DejaVuSans-Bold.ttf`：
- Linux: `/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf`
- macOS: `/System/Library/Fonts/Helvetica.ttc`
- Windows: `C:/Windows/Fonts/arial.ttf`

若找不到，修改 `scripts/process_single.py` 的 `_FONT_CANDIDATES` 列表添加自定义路径。
