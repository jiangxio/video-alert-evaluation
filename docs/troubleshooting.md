# 故障排查

汇总平台常见问题与解决方案。

## 安装与启动

### Q: Docker 构建时下载 torch 极慢

**现象**：`docker compose build` 卡在下载 torch wheel。

**原因**：pytorch.org 官方索引国内访问不稳定。

**解决**：
- 多重试几次（网络波动，时快时慢）
- Dockerfile 已配置 CPU 版 torch（`--index-url https://download.pytorch.org/whl/cpu`），体积仅约 200MB
- 若仍慢，可配置代理：`docker build --build-arg HTTPS_PROXY=http://代理:端口`

### Q: pip 安装报 `flit_core` 找不到 / `typing-extensions inconsistent Name`

**现象**：装 torch 时报 `Could not find a version that satisfies flit_core`。

**原因**：pip 24.1+ 严格校验包名，拒绝 pytorch 索引里的 typing-extensions wheel。

**解决**：Dockerfile 已固定 `pip<24.1` 并预装 typing-extensions，正常无需处理。手动安装时降级 pip：
```bash
pip install "pip<24.1"
```

### Q: Playwright 报不支持 debian11-x64

**现象**：`playwright install chromium` 报 `does not support chromium on debian11-x64`。

**原因**：playwright 1.62+ 不支持 Debian 11（bullseye）。

**解决**：用兼容版本 `pip install playwright==1.49.1`（requirements.txt 已固定）。

### Q: 启动报 sqlite3.Row 没有 .get() 方法

**现象**：`'sqlite3.Row' object has no attribute 'get'`。

**原因**：数据库连接用 `row_factory = sqlite3.Row`，该对象不支持 `.get()`。

**解决**：用 `row['key']` 或 `dict(row).get('key', default)`，详见 `CLAUDE.md`。

---

## OCR 识别

### Q: OCR 识别失败/结果为空

**排查步骤**：
1. 确认告警图片左上角有水印（视频 ID + 时间戳）
2. 确认水印清晰、未被遮挡
3. 用 `python scripts/ocr_easy.py 图片路径` 直接测试，看输出
4. 检查预处理：裁剪区域（左上角 700×120）、灰度、对比度、反色

**常见原因**：水印模糊、视频无水印、裁剪区域不对。

### Q: EasyOCR 模型下载失败

**现象**：首次 OCR 时 `Connection timed out` 下载模型。

**解决**：Docker 镜像已预下载模型（含重试逻辑）。手动安装时多次重试，或离线环境把模型放到 `~/.EasyOCR/model/`。

---

## 水印与视频

### Q: 打水印报错 fontfile 找不到

**现象**：FFmpeg drawtext 报找不到字体。

**解决**：
- Linux：装字体 `sudo apt-get install fonts-dejavu`
- macOS：用 conda-forge 的 ffmpeg（Homebrew 版缺 libfreetype）
- 自定义路径：改 `scripts/process_single.py` 的 `_FONT_CANDIDATES`

### Q: macOS 打水印失败

**原因**：Homebrew 的 ffmpeg 不含 `drawtext` 所需的 libfreetype。

**解决**：
```bash
conda install -c conda-forge ffmpeg
which ffmpeg  # 应指向 conda 路径
```

### Q: 视频抽帧/推流依赖找不到

**现象**：`ffmpeg 未安装或不在 PATH 中`。

**解决**：确认 `ffmpeg`、`ffprobe` 在 PATH：`ffmpeg -version`。Docker 镜像已内置。

---

## 评测

### Q: 评测结果异常 / 指标偏差大

**排查**：
1. 确认 ground_truth 已正确导入（`/evaluation/api/sync-gt` 重新同步）
2. 确认告警文件名规范正确（含告警类型 ID 与时间戳）
3. 确认视频 ID 匹配（告警 OCR 的 video_id 与 GT 的 video_id 一致）
4. 核对计算口径，详见 `CLAUDE.md` 的「评测核心指标计算逻辑」

### Q: 修改 confirmed_count 后指标不变

**原因**：`confirmed_count` 只影响召回率，不影响精确率。精确率取决于单张告警的有效状态（命中/误检），在评测执行阶段就确定了。

详见 `CLAUDE.md` 常见陷阱第 5 条。

### Q: PDF 报告生成失败

**现象**：生成 PDF 时报 chromium 错误。

**解决**：
- Docker 镜像已预装 chromium
- 手动安装：`playwright install chromium`
- HTML 报告不依赖 chromium，可作为替代

---

## 部署

### Q: 端口 8080 被占用

**解决**：改 `docker-compose.yml` 端口映射，如 `"9090:8080"`。

### Q: 容器启动后访问 404/连接拒绝

**排查**：
1. `docker compose logs` 看是否启动成功（waitress banner）
2. `docker ps` 确认容器 healthy
3. 确认端口映射正确、防火墙放行

### Q: 重建容器后数据丢失

**原因**：数据目录未用命名卷持久化。

**解决**：确认 `docker-compose.yml` 的 volumes 配置完整（db_data、uploads_data 等）。命名卷数据在 `docker compose down` 后保留，`docker compose down -v` 才删除。

---

## AI 助手

### Q: AI 助手报 API key 无效

**解决**：
1. 确认 `.env` 的 `OPENAI_API_KEY` 正确
2. 或在「API 配置」页面（/api-config/）统一配置，密钥加密存数据库
3. 确认 `OPENAI_BASE_URL` 指向兼容 OpenAI 协议的端点

### Q: 自动标注卡住/慢

**原因**：VLM 逐帧分析，帧数多则耗时长。

**解决**：调大 `frame_interval_sec`（默认每秒一帧，可改为每 2-3 秒）。
