# ── 阶段一：构建 Python 依赖（CPU 版 torch）────────────────────────────────
FROM python:3.10-slim-bullseye AS builder

# 构建期装编译工具（easyocr/numpy 等部分 wheel 需要构建）
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# 指定 CPU 版 torch/torchvision，避免拉取庞大的 CUDA 依赖
# torch 是镜像体积大头，CPU 版约 200MB（评测现场无需 GPU）
# typing-extensions 必须先从 PyPI 装：pytorch CPU 索引里它的 wheel 元数据异常会被跳过、
# 回退 sdist 又需 flit_core 构建（--index-url 下 PyPI 不可达，找不到 flit_core）
RUN pip install --no-cache-dir "pip<24.1" typing-extensions && \
    pip install --no-cache-dir torch torchvision \
        --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt /tmp/requirements.txt
# 显式指定 PyPI 索引：上一条 torch 安装的 --index-url 会持久化到 pip 配置，
# 不指定则 Step 5 只从 pytorch 索引找，Flask 等找不到
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
        --index-url https://pypi.org/simple/


# ── 阶段二：运行时镜像 ───────────────────────────────────────────────────
FROM python:3.10-slim-bullseye AS runtime

# 系统依赖：
#   ffmpeg        — 打水印 drawtext + RTSP 推流（含 libfreetype）
#   fonts-dejavu  — 水印字体（process_single.py 查找 DejaVuSans-Bold.ttf）
#   fonts-noto-cjk— 中文字体（报告/页面渲染中文不缺字）
#   curl          — 健康检查
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-dejavu \
        fonts-noto-cjk \
        curl \
    && rm -rf /var/lib/apt/lists/*

# 从 builder 拷贝已装好的 Python 依赖（含 torch、easyocr、playwright 等）
COPY --from=builder /usr/local/lib/python3.10/site-packages /usr/local/lib/python3.10/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# 非 root 用户运行（UID 1000，配合 volume 权限）
RUN useradd -m -u 1000 appuser

# 预下载 EasyOCR 英文模型（参赛现场可能无外网，OCR 是核心功能）
# 以 appuser 身份运行，模型落在其家目录 ~/.EasyOCR/model，约 100MB
# 模型从 JaidedAI CDN 下载，网络不稳，加重试（最多 5 次）
USER appuser
RUN for i in 1 2 3 4 5; do \
        python -c "import easyocr; easyocr.Reader(['en'], gpu=False)" \
        && break \
        || { echo "第 $i 次下载失败，重试..."; sleep 5; }; \
    done && python -c "import easyocr; r=easyocr.Reader(['en'], gpu=False); print('EasyOCR 模型就绪')"

# 安装 Playwright chromium 浏览器（PDF 报告生成依赖，约 300MB）
# --with-deps 装系统库需 root，切回 root 装完再切回 appuser
USER root
ENV PLAYWRIGHT_BROWSERS_PATH=/home/appuser/.cache/ms-playwright
RUN playwright install --with-deps chromium \
    && chown -R appuser:appuser /home/appuser/.cache
USER appuser

WORKDIR /app

# 拷贝应用代码与配置（数据目录由 volume 持久化，不拷入）
COPY --chown=appuser:appuser app/ ./app/
COPY --chown=appuser:appuser scripts/ ./scripts/
COPY --chown=appuser:appuser config/ ./config/
COPY --chown=appuser:appuser run.py process.py requirements.txt ./

# od 目标检测评测子服务（合并进主镜像，端口 5000）
COPY --chown=appuser:appuser od-dataset-manager/ ./od-dataset-manager/

# 单容器双服务启动脚本（主 8080 前台 + od 5000 后台）
COPY --chown=appuser:appuser docker-entrypoint.sh ./

# 预创建运行时目录（init_db 启动时需要写入；/app/data 供 DATABASE_PATH 持久化卷挂载）
# 以 root 创建目录并改属主，再切回 appuser
USER root
RUN mkdir -p uploads/videos uploads/alerts output ground_truth thumbnails \
        generated_videos extracted_frames auto_annotation_frames logs tmp report data \
        od-dataset-manager/data \
        od-dataset-manager/datasets/calling/images \
        od-dataset-manager/datasets/calling/labels \
    && chmod +x /app/docker-entrypoint.sh \
    && chown -R appuser:appuser /app

# ── 软瘦身：清理运行不需要的编译产物 / torch 自带测试目录 / pip 缓存 ──
# 只做零功能风险清理；不动 torch 本体、Playwright、scipy、opencv、EasyOCR
# 模型、字体——保证 OCR 与 PDF 报告全链路完整。3.2GB → 约 3.1GB（仍 2-5GB
# 档，镜像更干净、离线导出体积更小）。
USER root
RUN find /usr/local/lib/python3.10/site-packages -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null ; \
    find /usr/local/lib/python3.10/site-packages/torch -type d \( -name 'test' -o -name 'tests' \) -exec rm -rf {} + 2>/dev/null ; \
    find /usr/local/lib/python3.10/site-packages -name '*.pyc' -delete 2>/dev/null ; \
    rm -rf /root/.cache /home/appuser/.cache/pip ; \
    true

USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080 5000

# 单容器双服务：docker-entrypoint.sh 后台起 od(5000)，前台起主平台(8080, waitress)
CMD ["/app/docker-entrypoint.sh"]
