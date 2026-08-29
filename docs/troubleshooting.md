# 故障排查

本文件汇总平台部署、运行、评测过程中的常见问题与解决方案。每条按「现象 → 原因 → 解决」组织，排查步骤可直接照做。

> 命令中项目根目录以 `$PWD` 表示，Docker 部署场景默认已执行 `docker compose up -d`。

---

## 目录

- [安装与启动](#安装与启动)
  - [Docker 构建时下载 torch 极慢](#docker-构建时下载-torch-极慢)
  - [pip 安装报 flit_core 找不到](#pip-安装报-flit_core-找不到)
  - [Playwright 报不支持 debian11-x64](#playwright-报不支持-debian11-x64)
  - [启动报 sqlite3.Row 没有 .get() 方法](#启动报-sqlite3row-没有-get-方法)
  - [端口 8080 被占用](#端口-8080-被占用)
  - [容器启动后访问 404 / 连接被拒绝](#容器启动后访问-404--连接被拒绝)
  - [重建容器后数据丢失](#重建容器后数据丢失)
- [OCR 识别](#ocr-识别)
  - [OCR 识别失败 / 结果为空](#ocr-识别失败--结果为空)
  - [EasyOCR 模型下载失败](#easyocr-模型下载失败)
- [水印与视频](#水印与视频)
  - [打水印报错 fontfile 找不到](#打水印报错-fontfile-找不到)
  - [macOS 打水印失败](#macos-打水印失败)
  - [ffmpeg 未安装或不在 PATH](#ffmpeg-未安装或不在-path)
- [评测](#评测)
  - [评测结果异常 / 指标偏差大](#评测结果异常--指标偏差大)
  - [修改 confirmed_count 后精确率不变](#修改-confirmed_count-后精确率不变)
  - [评测执行报错 / 任务卡在 evaluating](#评测执行报错--任务卡在-evaluating)
  - [PDF 报告生成失败](#pdf-报告生成失败)
- [AI 助手](#ai-助手)
  - [AI 助手报 API key 无效](#ai-助手报-api-key-无效)
  - [自动标注卡住 / 速度慢](#自动标注卡住--速度慢)

---

## 安装与启动

### Docker 构建时下载 torch 极慢

**现象**：`docker compose build` 长时间卡在下载 torch wheel（`download.pytorch.org`），或超时失败。

**原因**：Dockerfile 中 torch 从 pytorch 官方 CPU 索引 `https://download.pytorch.org/whl/cpu` 拉取，该索引在国内网络环境下访问不稳定。CPU 版 torch 约 200MB，是镜像体积大头。

**解决**：

1. **多重试几次**——网络波动，时快时慢，`docker compose build` 会利用层缓存跳过已成功的步骤。
2. **配置 Docker 代理**（如有代理）：
   ```bash
   docker compose build --build-arg HTTPS_PROXY=http://代理地址:端口
   ```
3. **改用国内 PyPI 镜像装 torch**（离线/无代理时）：编辑 Dockerfile，将 torch 安装行改为从国内源拉取，例如：
   ```dockerfile
   RUN pip install --no-cache-dir torch torchvision \
       --index-url https://mirror.sjtu.edu.cn/pytorch-wheels/cpu
   ```
   构建完成后建议改回官方索引。

---

### pip 安装报 flit_core 找不到

**现象**：安装 torch 时报类似以下错误：

```
Could not find a version that satisfies the requirement flit_core
ERROR: Cannot install typing-extensions...
```

**原因**：pip 24.1+ 严格校验包名，会拒绝 pytorch CPU 索引中 `typing-extensions` 的 wheel（其元数据异常）。pip 回退去构建 sdist 时又需要 `flit_core`，而在 `--index-url` 指定 pytorch 索引时 PyPI 不可达，于是找不到 `flit_core`。

**解决**：Dockerfile 已处理此问题，正常构建无需干预。具体措施（见 Dockerfile 第 13-15 行）：

1. 先固定 `pip<24.1`：`pip install "pip<24.1"`
2. 再预装 `typing-extensions`（从默认 PyPI 拉，避免后续从 pytorch 索引拉）：
   ```bash
   pip install typing-extensions
   ```
3. 然后才装 torch：`pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu`

手动安装（非 Docker）遇到同样报错时，执行上述三步即可。

---

### Playwright 报不支持 debian11-x64

**现象**：执行 `playwright install chromium` 时报：

```
playwright._impl._driver.Error: Browser does not support chromium on debian11-x64
```

**原因**：Playwright 1.62 及以上版本已停止支持 Debian 11（bullseye）。本项目 Docker 基础镜像是 `python:3.10-slim-bullseye`（Debian 11），若不固定版本会拉到最新的不兼容版本。

**解决**：固定 Playwright 版本为 1.49.1（`requirements.txt` 已固定）：

```bash
pip install playwright==1.49.1
playwright install --with-deps chromium
```

Docker 镜像构建时已自动执行上述安装，无需手动处理。

---

### 启动报 sqlite3.Row 没有 .get() 方法

**现象**：访问页面或 API 时 500 报错，日志中出现：

```
AttributeError: 'sqlite3.Row' object has no attribute 'get'
```

**原因**：数据库连接使用 `row_factory = sqlite3.Row`（见 `app/database.py:19`）。`sqlite3.Row` 对象支持字典式索引访问（`row['key']`），但**不支持** `.get()` 方法。

**解决**：

```python
# 错误写法
value = row.get('key', default)

# 正确写法一：直接索引
value = row['key'] if row['key'] is not None else default

# 正确写法二：先转字典
row_dict = dict(row)
value = row_dict.get('key', default)
```

常见触发场景：从 `eval_tasks`、`events`、`videos` 等表读取数据后直接调用 `.get()`。详见 `CLAUDE.md` 的「sqlite3.Row 对象没有 .get() 方法」一节。

---

### 端口 8080 被占用

**现象**：`docker compose up` 或本地 `python run.py` 启动失败，报：

```
OSError: [Errno 98] Address already in use
```

或 Docker 报端口映射冲突。

**原因**：8080 端口已被其他进程占用（可能是上次未正常退出的平台实例，或其他服务）。

**解决**：

1. **查找占用进程**：
   ```bash
   sudo lsof -i :8080
   # 或
   sudo ss -tlnp | grep :8080
   ```
2. **结束占用进程**：
   ```bash
   kill <PID>      # 若不释放则 kill -9 <PID>
   ```
3. **或改用其他端口**：编辑 `docker-compose.yml`，将端口映射改为如 `"9090:8080"`（宿主 9090 → 容器 8080）。本地运行则改 `run.py` 中 `port=8080`。

> 注意：容器内部固定监听 8080（`run.py` 中 `waitress` 硬编码），只需改宿主映射端口即可。

---

### 容器启动后访问 404 / 连接被拒绝

**现象**：`docker compose up -d` 后，浏览器访问 `http://localhost:8080` 返回 404 或连接被拒绝。

**排查步骤**：

1. **查看容器日志**，确认 waitress 是否成功启动：
   ```bash
   docker compose logs web
   ```
   正常启动会看到 banner：
   ```
   ╔══════════════════════════════════════════════════════════════╗
   ║                    视频水印Benchmark平台                         ║
   ╚══════════════════════════════════════════════════════════════╝
   ```
2. **确认容器状态为 healthy**：
   ```bash
   docker ps
   ```
   healthcheck 配置为 `curl -fsS http://localhost:8080/`，`start_period` 40 秒。
3. **确认端口映射正确**：`docker ps` 输出中 PORTS 列应显示 `0.0.0.0:8080->8080/tcp`。
4. **确认防火墙放行**：云服务器需在安全组/防火墙放行 8080 入站。

---

### 重建容器后数据丢失

**现象**：`docker compose down && docker compose up -d` 后，数据库、上传的图片、水印视频等数据全部消失。

**原因**：数据目录未用命名卷（named volume）持久化，或使用了 `docker compose down -v`（`-v` 会删除命名卷）。

**解决**：

1. 确认 `docker-compose.yml` 的 `volumes` 配置完整。本项目已配置以下命名卷：

   | 卷名 | 容器路径 | 用途 |
   |------|---------|------|
   | `db_data` | `/app/data` | SQLite 数据库（`benchmark.db`） |
   | `uploads_data` | `/app/uploads` | 上传的原始视频和告警图片 |
   | `ground_truth_data` | `/app/ground_truth` | Ground Truth JSON 文件 |
   | `output_data` | `/app/output` | 水印视频输出 |
   | `report_data` | `/app/report` | 报告文件 |
   | `thumbnails_data` | `/app/thumbnails` | 缩略图 |
   | `generated_videos_data` | `/app/generated_videos` | 拼接视频 |
   | `extracted_frames_data` | `/app/extracted_frames` | 抽帧图片 |
   | `auto_annotation_frames_data` | `/app/auto_annotation_frames` | 自动标注抽帧 |
   | `logs_data` | `/app/logs` | 日志 |

2. **`docker compose down` 保留数据，`docker compose down -v` 删除数据**。若只想重启不要加 `-v`。

3. 若已误删，只能从备份恢复。建议定期备份 `db_data` 卷中的 `benchmark.db`。

---

## OCR 识别

### OCR 识别失败 / 结果为空

**现象**：上传告警图片后 OCR 识别返回空结果，或 `video_id`、`timestamp` 解析失败（`success: false`）。

**排查步骤**：

1. **直接用脚本测试单张图片**，查看原始 OCR 输出：
   ```bash
   python scripts/ocr_easy.py report/402_1774925112_103.png
   ```
   输出 JSON 中 `raw_ocr_text` 是 EasyOCR 的原始识别文本，`success` 表示是否解析出完整的 video_id + 时间戳。

2. **确认图片左上角有水印**：水印格式为 `{10位视频ID} {HH:MM:SS.sss}`（视频ID + 空格 + 时间戳，见 `scripts/process_single.py` 的 `text=`），位于左上角 (20, 20) 位置，32px 白字 + 黑底。若图片本身无水印或水印被遮挡，OCR 必然失败。

3. **检查预处理是否生效**——`ocr_easy.py` 的预处理流程：
   - 灰度化：`img.convert('L')`
   - 裁剪左上角水印区域：`img.crop((0, 0, min(540, w), min(50, h)))`（宽 540 × 高 50 像素）
   - 对比度增强 2.5 倍
   - 反色（黑底白字 → 白底黑字）

   > 注意：`scripts/final_ocr.py` 是 CLI 专用脚本，裁剪区域更大（`min(700, w) × min(120, h)`），预处理流程相同。Web 平台使用的是 `ocr_easy.py`。

4. **检查水印清晰度**：水印模糊、视频分辨率过低、水印被其他元素遮挡都会导致识别失败。

5. **检查 video_id 格式**：`parse_watermark_text()` 要求 video_id 恰好 10 位数字（正则 `\b\d{10}\b`），时间戳严格匹配 `HH:MM:SS.sss`。OCR 若把 `0` 识别成 `O`，代码会自动替换（`O/o → 0`），但其他识别错误无法自动纠正。

**常见原因**：水印模糊、视频本身无水印（未打水印的原始视频截图）、裁剪区域不对（水印位置不在左上角）。

---

### EasyOCR 模型下载失败

**现象**：首次执行 OCR 时卡住或超时，日志报：

```
ConnectionError: Failed to establish a new connection
```

或

```
TimeoutError: [Errno 110] Connection timed out
```

**原因**：EasyOCR 首次运行需从 JaidedAI CDN 下载英文识别模型（`craft_mlt25k.pth` + `english_g2.pth`，约 100MB），下载到 `~/.EasyOCR/model/`。离线环境或网络不稳时下载会失败。

**解决**：

1. **Docker 部署**：Dockerfile 已预下载模型并内置重试逻辑（最多 5 次，每次间隔 5 秒），正常情况下镜像内已有模型，无需联网下载。
2. **手动安装/本地运行**：多次重试即可。或在有网机器上先运行一次 `python -c "import easyocr; easyocr.Reader(['en'], gpu=False)"` 完成下载，再将 `~/.EasyOCR/model/` 整个目录拷贝到目标机器相同路径。
3. **确认模型文件到位**：
   ```bash
   ls ~/.EasyOCR/model/
   # 应看到 craft_mlt25k.pth 和 english_g2.pth
   ```

---

## 水印与视频

### 打水印报错 fontfile 找不到

**现象**：执行 `python process.py --single ...` 或 Web 平台打水印时，FFmpeg 报错：

```
Cannot find a valid font for the family Sans
Fontfile cannot be found
```

或 `process_single.py` 输出 `错误: 找不到合适的字体文件`。

**原因**：`scripts/process_single.py` 的 `find_font()` 函数按操作系统查找字体文件（见 `_FONT_CANDIDATES` 字典），若系统未安装对应字体则返回 `None`。

| 操作系统 | 查找路径 |
|---------|---------|
| Linux | `/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf`、`/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf` |
| macOS | `/System/Library/Fonts/Helvetica.ttc`、`/Library/Fonts/Arial.ttf` |
| Windows | `C:/Windows/Fonts/arial.ttf` 等 |

**解决**：

1. **Linux（Debian/Ubuntu）**：安装 DejaVu 字体：
   ```bash
   sudo apt-get install fonts-dejavu
   ```
   Docker 镜像已内置 `fonts-dejavu`。

2. **自定义字体路径**：修改 `scripts/process_single.py` 中的 `_FONT_CANDIDATES` 字典，添加系统中存在的字体路径：
   ```python
   'Linux': [
       '/usr/share/fonts/your-font.ttf',           # 添加你的路径
       '/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf',
       '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
   ],
   ```

---

### macOS 打水印失败

**现象**：在 macOS 上执行打水印，FFmpeg 报 `No such filter: 'drawtext'` 或 `Fontconfig error`。

**原因**：Homebrew 安装的 `ffmpeg` 默认不含 `drawtext` 滤镜所需的 `libfreetype` / `fontconfig` 库，导致无法绘制文字水印。

**解决**：改用 conda-forge 的 ffmpeg（含完整滤镜支持）：

```bash
conda install -c conda-forge ffmpeg
which ffmpeg    # 应指向 conda 环境路径，如 ~/miniconda3/bin/ffmpeg
ffmpeg -filters | grep drawtext   # 确认 drawtext 可用
```

或用 MacPorts：`sudo port install ffmpeg +nonfree`。

---

### ffmpeg 未安装或不在 PATH

**现象**：打水印、视频抽帧、RTSP 推流时报：

```
FileNotFoundError: [ErrNo 2] No such file or directory: 'ffmpeg'
```

或 `process_single.py` 中 `shutil.which('ffprobe')` 返回 `None`，导致无法探测视频时长。

**原因**：系统未安装 FFmpeg，或未加入 PATH 环境变量。

**解决**：

1. **确认 ffmpeg / ffprobe 可用**：
   ```bash
   ffmpeg -version
   ffprobe -version
   ```
2. **安装 FFmpeg**：
   - Linux：`sudo apt-get install ffmpeg`
   - macOS：`conda install -c conda-forge ffmpeg`（推荐，含 drawtext 支持）
   - Windows：从 [ffmpeg.org](https://ffmpeg.org/download.html) 下载并添加到 PATH
3. Docker 镜像已内置 `ffmpeg`（含 `libfreetype`），无需额外安装。

---

## 评测

> 本节每条均对照代码核验；指标口径以 `CLAUDE.md`「评测核心指标计算逻辑」为权威。

### 评测结果异常 / 指标偏差大

**现象**：评测完成后精确率、召回率、误检数/小时等指标与预期明显不符。

**排查步骤**：

1. **确认 Ground Truth 已正确导入**：GT 数据来自 `ground_truth/{video_id}.json`，需先导入数据库。可在评测任务详情页或通过 API 重新同步：
   ```
   POST /evaluation/api/sync-gt
   Body: {"video_db_id": <ID>, "direction": "gt_to_db"}
   ```

2. **确认告警文件名规范正确**：告警图片文件名格式为 `{prefix}_{unix_timestamp}_{alert_type_id}.png`，其中 `alert_type_id` 用于查 `config/alert_types.json` 映射事件类型（配置位置见 `app/config.py` 的 `ALERT_TYPES_CONFIG`）。文件名不规范会导致事件类型匹配失败。

3. **确认视频 ID 匹配**：告警 OCR 识别出的 `video_id` 必须与 GT 中的 `video_id` 一致（10 位数字），且与评测视频集中的视频匹配。

4. **核对指标计算口径**——以下是核心计算逻辑（详见 `CLAUDE.md` 的「评测核心指标计算逻辑」）：

   - **命中判定**（单张告警 → 命中/误检）：`video_id` 相同 AND `event_type` 相同 AND 时间窗口与 GT 事件重叠（±5s 容差）。只看时间重叠，不限次数。
   - **精确率** = `correct_pred_count / alert_count`（有效状态 ≠ `ignored` 的告警中，有效状态 = `correct` 的占比）。与 `confirmed_count` 无关。
   - **召回率** = 各事件类型召回率的**算术平均**（非加权）。每个类型的 `hit_count = min(actual, confirmed)`。
   - **平均误检数/小时** = 各事件类型 `fp_count / total_duration_hours` 的**算术平均**。
   - **有效状态**：`manual_status` 优先于 `is_false_positive`，所有统计必须通过 `get_effective_status()` 获取。

5. **检查是否有告警被标记为 ignored**：`ignored` 状态的告警不参与精确率计算（分子分母都不计入），会影响最终数值。

---

### 修改 confirmed_count 后精确率不变

**现象**：在评测任务详情页修改了某 GT 事件的 `confirmed_count`（预期触发数），召回率实时更新了，但精确率没有变化。

**原因**：这是**设计行为，不是 bug**。

- `confirmed_count` 只影响**召回率**计算（`hit_count = min(actual, confirmed)` 封顶逻辑）。
- **精确率**取决于单张告警的有效状态（`correct` / `false_positive`），该状态在评测执行阶段（`execute_task`）就已通过命中判定确定，与 `confirmed_count` 无关。

**操作建议**：

- 想改变精确率，需要修改单张告警的状态——在评测详情页点击告警，将 `manual_status` 从 `auto` 改为 `correct` / `false_positive` / `ignored`。
- 详见 `CLAUDE.md` 常见陷阱第 5 条。

---

### 评测执行报错 / 任务卡在 evaluating

**现象**：点击「执行评测」后任务状态变为 `evaluating` 并长时间不返回，或状态变为 `failed`。

**排查步骤**：

1. **查看任务错误信息**：若状态为 `failed`，`eval_tasks.error_message` 字段会记录异常原因（见 `app/database.py` 的 `error_message` 列）。在任务详情页或通过 API 查看：
   ```
   GET /evaluation/api/tasks/<task_id>
   ```
   常见错误：数据集未关联算法版本、GT 事件为空、video_id 不在评测视频集中。

2. **确认任务前置条件**：
   - 已确认合并告警和 GT 事件（任务状态为 `confirming`）
   - 数据集关联的算法版本每种类型最多一个，否则报 `数据集关联了多个 'xxx' 类型的算法版本`（见 `app/routes/evaluation.py` 校验）。
   - 告警的 `video_id` 全部包含在评测视频集中（实时模式跳过此校验）

3. **查看后端日志**：
   ```bash
   docker compose logs web --tail 100
   ```
   评测在后台线程执行，异常会打印完整 traceback。

4. **重新执行**：修正问题后，在任务详情页点击「重新执行评测」即可，旧结果会被自动清除。

---

### PDF 报告生成失败

**现象**：在报告配置页点击「生成 PDF」报错，返回 `PDF 生成失败`。

**原因**：PDF 生成依赖 Playwright + Chromium 渲染 HTML 后转 PDF（见 `app/routes/evaluation.py` 的 `detailed_report_pdf` 路由）。若 Chromium 未安装或系统库缺失，会报错。

**解决**：

1. **Docker 部署**：Dockerfile 已预装 Chromium（`playwright install --with-deps chromium`），正常无需处理。
2. **手动安装**：
   ```bash
   pip install playwright==1.49.1
   playwright install --with-deps chromium
   ```
3. **确认 Chromium 就绪**：
   ```bash
   python -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(); print('OK'); b.close()"
   ```
4. **临时替代方案**：HTML 报告不依赖 Chromium，可正常生成和下载。如只需查看报告内容，使用「生成 HTML 报告」即可。

---

## AI 助手

### AI 助手报 API key 无效

**现象**：AI 助手对话、报告摘要生成、自动标注等功能报 `API key 无效` 或 `401 Unauthorized`。

**原因**：AI 功能分两组配置（见下表）。API Key 默认从 `.env` 环境变量读取，也可在「助手设置」页（`/assistant/settings`）配置——密钥经 `ASSISTANT_ENCRYPTION_KEY` 加密后存入数据库，优先级高于 `.env`（见 `app/services/assistant_settings.py`）。未配置或配置错误会导致调用失败。

| 能力组 | 环境变量 | 用途 | 回退 |
|--------|---------|------|------|
| 文本逻辑组 | `OPENAI_API_KEY` | AI 助手对话、评测报告生成（摘要/结论/对话改写） | 无 |
| 多模态审查组 | `VISION_API_KEY` | 智能审查、自动标注 | 未配置时回退到 `OPENAI_API_KEY` |

**解决**：

1. **编辑 `.env` 文件**（Docker 部署时 `.env` 是唯一需修改的配置文件）：
   ```bash
   cp .env.example .env
   # 编辑 .env，填入真实 API Key
   ```
   ```env
   # 文本逻辑组
   OPENAI_API_KEY=sk-your-key-here
   OPENAI_BASE_URL=https://api.openai.com/v1
   OPENAI_MODEL=gpt-4o-mini

   # 多模态审查组（可选，未填写时回退到文本组）
   VISION_API_KEY=
   VISION_BASE_URL=https://api.openai.com/v1
   VISION_MODEL=Qwen3-VL-8B-Instruct
   ```

2. **或在「API 配置」页面统一配置**：访问 `/api-config/` 页面，设置 base_url 和 model（密钥仍从 `.env` 注入，页面只显示「是否已配置」标记）。

3. **确认 `OPENAI_BASE_URL` 正确**：必须指向兼容 OpenAI 协议的端点，且以 `/v1` 结尾（代码会自动补全 `/v1`）。

4. **重启服务使 `.env` 生效**：
   ```bash
   docker compose restart web
   ```

---

### 自动标注卡住 / 速度慢

**现象**：自动标注任务长时间不完成，或进度增长极慢。

**原因**：自动标注使用多模态 VLM 逐帧分析视频帧，帧数越多耗时越长。默认每秒抽 1 帧（`frame_interval_sec=1`），一段 10 分钟视频需分析 600 帧。

**解决**：

1. **调大 `frame_interval_sec`**：创建自动标注任务时设为 2 或 3（每 2-3 秒一帧），帧数减半/三分之二。
2. **确认 VLM 端点响应速度**：多模态模型推理本身耗时，若 `VISION_BASE_URL` 指向的端点响应慢，整体时间线性增长。
3. **查看任务进度和阶段**：自动标注分多阶段执行（抽帧 → 逐帧分析 → 合并事件），可在任务详情页查看 `current_phase` 和 `phase_progress`。
4. **检查是否有报错**：查看 `auto_annotation_tasks.error_message` 字段或后端日志。
