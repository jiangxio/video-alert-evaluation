# 局域网访问卡顿优化方案

> 分析日期：2026-11-06
> 分析方式：在服务端本机（`127.0.0.1:8080`）实测，**排除网络带宽因素**（`enp1s0` 协商 1Gbps）。
> 结论：卡顿主因在服务端，不在局域网带宽。

---

## 0. TL;DR

| # | 根因 | 实测影响 | 修复 | 预期 |
|---|------|----------|------|------|
| P0-1 | 评测结果 / 复核接口的 SQL 派生表执行计划退化 | `/evaluation/api/tasks/27/results` **4.58s**；`/review/api/27/alerts` **4.56s** | 改写为相关子查询 | **~35ms**（约 130×） |
| P0-2 | GT 帧查看弹窗一次加载 60 张原图 PNG | 60 × ~2.8MB ≈ **168MB/次** | 改用 `?w=320` 缩略图 + lazy | ~**300KB**（约 500×） |
| P0-3 | 所有缩略图实时解码生成，无磁盘缓存；GT 缩略图仍输出 PNG | 单张 45–59ms CPU；结果页首屏 104 张 ≈ **3.8s CPU** | 缩略图落盘缓存 + 统一 JPEG | 命中缓存后 **~2ms**，体积降 60–80% |
| P1-1 | 弹窗/详情页用原图；数据集详情还带 `?t=` 缓存破坏参数 | 每次打开重新下载 0.6–1.2MB | 改用 `?w=1280` 预览图，去掉 `?t=` | 单次 **~100–170KB**，且可缓存 |
| P2-1 | 静态资源 `Cache-Control: no-cache` | `echarts.min.js` 1MB 每次导航需回源校验 | 设置静态缓存头 | 304 往返/首次 1MB 下载减少 |
| P2-2 | 无 gzip；大文件经 Waitress/Python 转发 | 结果 JSON 1MB 未压缩 | gzip / Nginx 反代 | JSON 降至 ~100KB |

---

## 1. 实测基线

### 1.1 接口耗时（本机 localhost）

| 接口 | 耗时 | 响应大小 | 备注 |
|------|------|----------|------|
| `/evaluation/api/tasks/27/results` | **4.58s** | 1.06MB / 2178 条告警 | 评测结果页首屏必经 |
| `/review/api/27/alerts` | **4.56s** | 637KB / 2178 条告警 | 复核工作台首屏必经 |
| `/evaluation/api/tasks/26/results` | **3.20s** | 781KB / 1503 条 | 同类问题 |
| `/evaluation/api/tasks/23/results` | 0.08s | 830KB / 2005 条 | **数据分布不同导致的假象**，见 2.1 |
| `/evaluation/api/tasks/27/event-metrics` | 0.018s | 2KB | 正常 |
| `/evaluation/api/tasks/27/check-updates` | 0.005s | 22B | 正常 |
| `/alerts/` 首页 | 0.041s | 29KB | 正常 |
| `/alerts/api/datasets/17/images?per_page=80` | 0.009s | 41KB | 正常 |
| `/videos/` | 0.012s | 49KB | 正常 |
| `/videos/api/all` | 0.015s | 39KB | 正常 |
| `/evaluation/` | 0.007s | 21KB | 正常 |
| `/algorithms/` | 0.012s | 34KB | 正常 |

> 除评测/复核两个接口外，其余主要接口均在 20ms 内，说明「全站卡」的直接体感主要来自这两个秒级接口和图片链路。

### 1.2 图片链路

| 项目 | 实测值 |
|------|--------|
| 告警图片 | 13,060 张，平均 **765KB**，最大 1.2MB；尺寸以 2560×1440 / 1920×1080 为主 |
| GT 帧 | 10,098 条记录 / 7,133 个文件，平均 **2.45MB PNG**（1904×1080 / 1920×1080），目录共 **16.7GB** |
| 告警缩略图（`?w=240`） | 单次 25–45ms CPU，输出 9–11KB |
| GT 帧缩略图（`?w=240`） | 单次 **48–59ms** CPU，输出 **29KB**（仍是 PNG） |
| 评测结果页首屏 | 52 张告警缩略图 + 52 张 GT 缩略图 = **104 张**，服务端 CPU 合计约 **3.8s** |
| GT 帧弹窗（video_annotate） | 60 张原图 PNG，单张 ~2.8MB → **约 168MB/次** |
| 数据集详情弹窗 | 原图 0.6–1.2MB，且 URL 带 `?t=${Date.now()}`，**每次都强制重新下载** |
| `/static/js/echarts.min.js` | 1MB，`Cache-Control: no-cache` |

---

## 2. 根因分析

### 2.1 P0-1：SQL 派生表执行计划退化（影响最大）

涉及位置：

- `app/routes/evaluation.py:866-872`（`get_results`，评测结果页）
- `app/routes/review.py:54-59`（`get_alerts`，复核工作台）
- `app/services/eval_service.py:159-167`（评测执行，影响较小）
- `app/services/assistant_tools.py:587-592`（助手工具，影响较小）

问题写法（以 `get_results` 为例）：

```sql
LEFT JOIN (
    SELECT alert_image_id, timestamp_seconds
    FROM ocr_results
    WHERE id IN (
        SELECT MAX(id) FROM ocr_results GROUP BY alert_image_id
    )
) o ON o.alert_image_id = m.representative_image_id
```

SQLite 的 `EXPLAIN QUERY PLAN` 显示该派生表被当作 `LIST SUBQUERY` + `BLOOM FILTER` 处理，无法有效把外层 `representative_image_id` 下推到索引上，实际随「外层行数 × OCR 行数」放大。

**为什么 task 23 很快、task 26/27 很慢？**

| task | 告警数 | representative_image_id 能在 ocr_results 中匹配到 |
|------|--------|--------------------------------------------------|
| 23 | 2005 | **0 条**（全部无 OCR 结果，连接被快速短路） |
| 26 | 1503 | 1503 条 |
| 27 | 2178 | 2178 条 |

所以 task 23 的 0.08s 是假象，真实数据一旦有 OCR 结果就会退化到 3–4.5s。这也是为什么问题看起来「时好时坏」。

**修复方式**：改为相关子查询，明确用 `idx_ocr_alert_image` 索引取每个告警的最新 OCR 行：

```sql
LEFT JOIN ocr_results o
  ON o.id = (
      SELECT MAX(o2.id)
      FROM ocr_results o2
      WHERE o2.alert_image_id = m.representative_image_id
  )
```

同理，videos 去重子查询：

```sql
-- 修改前
LEFT JOIN (
    SELECT id, video_id FROM videos
    WHERE id IN (SELECT MAX(id) FROM videos GROUP BY video_id)
) v ON v.video_id = m.video_id

-- 修改后（语义等价：取同 video_id 的最大 id）
LEFT JOIN videos v
  ON v.id = (SELECT MAX(v2.id) FROM videos v2 WHERE v2.video_id = m.video_id)
```

**实测对比（同样 2178 / 1503 行）**：

| task | 修改前 | 修改后 |
|------|--------|--------|
| 27 | 4497ms | **34ms** |
| 26 | 3115ms | **24ms** |

> 语义等价性：原写法取「每个 `alert_image_id` 的最大 `id`」那一行；相关子查询 `MAX(o2.id) WHERE o2.alert_image_id = ...` 完全等价；无 OCR 记录时两者都返回 NULL（LEFT JOIN 保持一行）。

**建议同时删除** `get_results` 查询中前端未使用的 `a.filename, a.file_path`：`eval_task.html` 与 `review_workbench.js` 均未读取这两个字段，删除可减小响应体并避免泄露绝对路径。若删除 `a` 表连接，还能再省一点 JOIN 开销。

---

### 2.2 P0-2：GT 帧弹窗一次加载 168MB

`app/templates/video_annotate.html:848`：

```html
<img src="/evaluation/api/gt-frames/${f.id}/file"
     alt="${f.timestamp_sec}s"
     onclick="window.open('/evaluation/api/gt-frames/${f.id}/file', '_blank')">
```

- 每个事件最多 **60 帧**（`videos.py:1204` 注释），单帧 PNG **约 2.8MB**；
- 打开一次弹窗 = **约 168MB** 图片同时进入 DOM；
- 文件没有 `loading="lazy"`，60 张全部立刻请求；
- 浏览器还要解码 60 张 1904×1080 PNG，内存/CPU 压力极大，表现为「点开就卡死」。

同类问题：

- `app/templates/eval_task.html:1899,1904,1957`：弹窗单图仍用原图（GT 帧单张 2.8MB）；
- `app/static/js/review_workbench.js:568`：复核工作台大图用原图（0.6–1.2MB）；
- `app/templates/dataset_detail.html:551`：详情弹窗原图，且带 `?t=${Date.now()}` 缓存破坏。

---

### 2.3 P0-3：缩略图实时生成且无缓存

`app/routes/__init__.py:35` 的 `send_image_with_thumbnail` 每次请求都：

1. `Image.open(file_path)` 完整解码原图（1920×1080 JPEG ≈ 23ms；1904×1080 PNG ≈ 40ms+）；
2. `thumbnail(..., LANCZOS)`；
3. 按**源文件扩展名**决定输出格式——GT 帧源文件是 `.png`，因此缩略图也编码成 **PNG（29KB）**，而不是更小的 JPEG（~5KB）。

没有任何磁盘缓存，也没有 `ETag` / `Last-Modified`（`BytesIO` 响应拿不到文件校验器）。浏览器虽然能靠 `max-age=86400` 缓存，但：
- 每个新访客 / 硬刷新 / 换浏览器都要重新解码；
- 多个局域网用户同时访问时，服务端 CPU 会叠加；
- 评测结果页一次渲染 104 张（告警 + GT），仅 GT 缩略图就约 **3s CPU**。

对照：视频封面缩略图（`app/routes/videos.py:905`）已经是「生成一次落盘、之后直接 `send_file`」的正确做法，图片链路应复用同一模式。

---

### 2.4 P1：大图与缓存破坏

- `dataset_detail.html:551` 的 `?t=${Date.now()}` 让浏览器**永远无法命中缓存**，每点开一张图都重新下载 0.6–1.2MB。图片文件在标注过程中不会被修改，这个参数没有存在必要。
- 结果页 / 复核页大图可直接用 `?w=1280` 预览（GT 帧 1280px JPEG 约 **70KB**，告警图约 **166KB**），需要看细节时再提供「查看原图」入口。

---

### 2.5 P2：静态资源与传输

- `/static/**` 实测 `Cache-Control: no-cache`（`echarts.min.js` 1MB）。当前依赖 `ETag` 做 304 校验，缓存命中时不会重复下载 1MB，但每次导航每个静态文件仍要多一次往返。
- Waitress 不做 gzip。评测结果 JSON 1.06MB、复核 JSON 637KB，gzip 后可降到 ~10%。
- 视频播放/大文件下载也经过 Waitress（Python 转发），没有 `sendfile`；如果是多用户场景，建议加 Nginx 反代。

---

## 3. 优化方案

### 阶段一（P0，建议优先实施，1–2 天）

#### 3.1 修复 SQL（预计 0.5 小时）

**修改文件：**

1. `app/routes/evaluation.py` `get_results()`：
   - 告警查询（866–872 行附近）改为相关子查询；
   - GT 查询（958 行附近）的 videos 派生表同样改写；
   - 可选：删除未使用的 `a.filename, a.file_path`，甚至删除 `alert_images` 连接。
2. `app/routes/review.py` `get_alerts()`（54–59 行）：同上。
3. 可选一致性修改：`app/services/eval_service.py:159`、`app/services/assistant_tools.py:587`。

**验收：**

```bash
time curl -s -o /dev/null "http://127.0.0.1:8080/evaluation/api/tasks/27/results"
time curl -s -o /dev/null "http://127.0.0.1:8080/review/api/27/alerts"
```

目标：均 < 200ms（预期 30–60ms）。

**正确性校验**（改动前后各跑一次，结果必须一致）：

```sql
-- 行数
SELECT COUNT(*) FROM (...) ;
-- 时间戳求和/校验和
SELECT COUNT(*), ROUND(SUM(o.timestamp_seconds), 3) FROM ... ;
```

建议对 task 23 / 26 / 27 三个任务分别比对 `COUNT(*)` 与 `SUM(timestamp_seconds)`。

#### 3.2 缩略图落盘缓存 + 统一 JPEG（预计 0.5 天）

**修改文件：** `app/routes/__init__.py` 的 `send_image_with_thumbnail`

**设计要点：**

- 缓存目录：`{THUMBNAILS_DIR}/imgcache/`（已在 `app/config.py` 配置，Docker 已有持久卷）。
- 缓存 key：`sha1(绝对路径 + mtime_ns + size + "WxH")[:20] + ".jpg"`。
  - 源文件一旦被替换（mtime/size 变化）自动换 key，无需手动失效；
  - 旧文件可定期清理，或按总大小做 LRU（初期可不做，先观察磁盘占用）。
- 命中缓存时直接 `send_file_with_cache(cache_path)`，由真实文件自动获得 `ETag` / `Last-Modified` 和 Range 支持。
- 未命中时：
  1. 解码原图，`ImageOps.exif_transpose` 修正方向；
  2. `thumbnail((w,h), LANCZOS)`；
  3. 统一转 RGB 后保存为 **JPEG quality=82, optimize=True**（RGBA/P 先合成白底）；
  4. 写入同目录临时文件后 `os.replace()` 原子落盘，避免并发读到半张图。
- 尺寸参数做上限保护（如 `min(w, 2048)`），避免异常参数。
- 生成失败时回退到原图（保留现有 `except` 行为）。

**代码骨架：**

```python
import hashlib, os, tempfile
from pathlib import Path
from flask import current_app, send_file, make_response
from PIL import Image, ImageOps

def _thumb_cache_path(file_path, max_width, max_height):
    src = Path(file_path)
    st = src.stat()
    key = hashlib.sha1(
        f"{src.resolve()}|{st.st_mtime_ns}|{st.st_size}|{max_width}x{max_height}".encode()
    ).hexdigest()[:20]
    cache_dir = Path(current_app.config['THUMBNAILS_DIR']) / 'imgcache'
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f'{key}.jpg'

def send_image_with_thumbnail(file_path, max_width=None, max_height=None, mimetype=None):
    if (not max_width and not max_height) or not file_path:
        return send_file_with_cache(file_path, mimetype=mimetype)
    max_width = min(max_width or 2048, 2048)
    max_height = min(max_height or 2048, 2048)
    try:
        cache_path = _thumb_cache_path(file_path, max_width, max_height)
        if cache_path.exists():
            return send_file_with_cache(str(cache_path), mimetype='image/jpeg')

        img = ImageOps.exif_transpose(Image.open(file_path))
        img.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGBA')
            bg = Image.new('RGB', img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        else:
            img = img.convert('RGB')

        fd, tmp = tempfile.mkstemp(dir=str(cache_path.parent), suffix='.tmp')
        os.close(fd)
        img.save(tmp, format='JPEG', quality=82, optimize=True)
        os.replace(tmp, cache_path)
        return send_file_with_cache(str(cache_path), mimetype='image/jpeg')
    except Exception:
        return send_file_with_cache(file_path, mimetype=mimetype)
```

> 注意：并发首次生成同一 key 时可能重复计算一次，但 `os.replace` 保证结果正确；若在意可加 per-key 锁，但不建议用全局锁（会把所有缩略图串行化）。

**附带收益（可选）：**

- JPEG 源图可先 `img.draft('RGB', (max_width*2, max_height*2))` 再缩略，1920×1080→240px 可再省约 25% 解码时间。
- 可写一个一次性回填脚本，对现有 13k 告警图 / 10k GT 帧预生成 240px 缩略图（约 190MB 磁盘、数分钟 CPU），让首个访客也无需等待。

**容量估算：**

| 尺寸 | 数量 | 单张 | 合计 |
|------|------|------|------|
| 240px 告警缩略图 | 13,060 | ~10KB | ~130MB |
| 240px GT 缩略图 | 10,098 | ~5KB | ~50MB |
| 1280px 预览（按需生成） | 实际访问过的图片 | 70–166KB | 视访问量 |

#### 3.3 GT 帧弹窗改缩略图（预计 0.2 小时）

`app/templates/video_annotate.html:848`：

```html
<!-- 修改前 -->
<img src="/evaluation/api/gt-frames/${f.id}/file" ...>

<!-- 修改后 -->
<img src="/evaluation/api/gt-frames/${f.id}/file?w=320"
     loading="lazy" decoding="async"
     onclick="window.open('/evaluation/api/gt-frames/${f.id}/file', '_blank')" ...>
```

效果：60 张由 ~168MB 降到约 **300KB**，且首屏只加载可视区。

#### 3.4 结果网格补 `loading="lazy"`（预计 0.2 小时）

`app/templates/eval_task.html`：

- 1247 行（告警结果网格）
- 1337 行（GT 结果网格）
- 896 / 932 行（二次确认卡片）

给 `<img>` 增加 `loading="lazy" decoding="async"`。结合磁盘缓存后，首屏实际只请求可视区约 12–20 张。

---

### 阶段二（P1，建议同期或紧随）

#### 3.5 弹窗 / 预览统一使用 1280px 预览图

| 文件/位置 | 修改 |
|-----------|------|
| `app/templates/dataset_detail.html:551` | 去掉 `?t=${Date.now()}`，改为 `/file?w=1280`；需要原图时提供「查看原图」链接 |
| `app/static/js/review_workbench.js:568` | `imgUrl()` 返回 `/file?w=1280`；lightbox 可保持原图或 `?w=1920` |
| `app/templates/eval_task.html:1899,1904,1957` | 弹窗图改为 `?w=1280`；如需查看原始细节再加「原图」按钮 |

实测体积：告警图 1280px ≈ 166KB（原 617KB）；GT 帧 1280px ≈ 70KB（原 998KB–2.8MB）。

#### 3.6 静态资源缓存

在 `app/config.py` 或 `create_app()` 中设置：

```python
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 86400  # 1 天
```

或对 `/static/` 单独 `after_request` 设置 `Cache-Control: public, max-age=86400`。

> 上线后若担心 JS/CSS 更新不及时，可在模板引用上加 `?v={{ mtime }}`，或将 max-age 调到 3600。

---

### 阶段三（P2，可选，多用户/长期部署建议）

#### 3.7 gzip

引入 `flask-compress`：

```python
from flask_compress import Compress
Compress(app)
```

`results` JSON 1.06MB → 约 100KB；HTML/JS/CSS 同样受益。若不想加依赖，也可只对 `application/json` 做一个小型 `after_request` gzip（长度 > 1KB 且客户端支持时）。

#### 3.8 Nginx 反向代理（可选）

```
浏览器 → Nginx :80 → Waitress 127.0.0.1:8080
```

Nginx 负责：

- `/static/`、`/thumbnails/`、`/uploads/`、`/ground_truth_frames/` 直接 `sendfile`；
- 视频通过 `X-Accel-Redirect` 交给 Nginx 发送（支持 Range/seek，不占 Python 线程）；
- gzip、keepalive、HTTP/2。

对当前「1–3 个用户」的局域网场景，阶段一 + 二已经足够；Nginx 是多人并发或需要播放视频时的加固项。

#### 3.9 响应体瘦身（可选）

- `get_results` / `get_alerts` 不再返回 `file_path`、`filename`（前端未使用）。
- 评测指标可由已加载的 `alert_results` 在前端计算的部分，减少后端重复扫描（当前 `compute_task_metrics` 会再扫一遍任务数据，约占修复后耗时的 50–70ms，非瓶颈）。

---

## 4. 预期收益汇总

| 场景 | 优化前 | 优化后（预期） |
|------|--------|----------------|
| 打开评测结果页（服务端） | 4.6s | ~0.1–0.2s |
| 打开复核工作台（服务端） | 4.6s | ~0.1s |
| 评测结果页 104 张缩略图（首次） | ~3.8s CPU | ~1–1.5s CPU（首访）；~0.1s（命中缓存） |
| 评测结果页图片流量 | ~2MB | ~1MB（JPEG + lazy） |
| GT 帧弹窗 | ~168MB | ~0.3MB |
| 数据集详情看大图 | 0.6–1.2MB/次，且不可缓存 | 0.1–0.17MB，可缓存 |
| 单张缩略图服务端耗时 | 45–59ms | ~2–3ms（缓存命中） |
| 静态 JS（echarts） | no-cache 回源校验 | 1 天强缓存 |

---

## 5. 验证清单

### 5.1 性能

```bash
# 1) 结果/复核接口
for u in \
  "/evaluation/api/tasks/27/results" \
  "/evaluation/api/tasks/26/results" \
  "/review/api/27/alerts"; do
  printf "%-45s " "$u"
  curl -s -o /dev/null "http://127.0.0.1:8080$u" -w 'time=%{time_total}s size=%{size_download}\n'
done

# 2) 缩略图首次 / 第二次（应明显下降，第二次 <10ms）
curl -s -o /dev/null -w 'first=%{time_total}s\n' "http://127.0.0.1:8080/alerts/api/images/2/file?w=240"
curl -s -o /dev/null -w 'cached=%{time_total}s\n' "http://127.0.0.1:8080/alerts/api/images/2/file?w=240"

# 3) 缓存响应头（应包含 ETag / Last-Modified）
curl -sI "http://127.0.0.1:8080/evaluation/api/gt-frames/1/file?w=320"
```

### 5.2 正确性

- SQL 改写前后，对 task 23 / 26 / 27 比对：
  - `COUNT(*)`
  - `SUM(o.timestamp_seconds)`
  - 抽样 10 条 `representative_image_id → timestamp_seconds` 映射
- 前端回归：
  - 评测结果页告警/GT 数量、分页、筛选、状态修改
  - 复核工作台告警列表与时间轴
  - 视频标注页 GT 帧弹窗（缩略图显示、点击看原图）

### 5.3 回归测试

```bash
source .venv/bin/activate
pytest tests/ -q
```

重点：`tests/test_api_v1_evaluation.py`、`tests/test_eval_service.py`、`tests/test_api_v1_review.py`。

---

## 6. 风险与注意事项

1. **SQL 语义**：相关子查询与原派生表语义等价，但务必用上述校验 SQL 在真实任务上比对后再上线；`review.py` 与 `evaluation.py` 两处都要改。
2. **缩略图缓存失效**：key 包含源文件 `mtime_ns + size`，替换原图后自动换 key；若删除图片，缓存文件会残留，需要定期清理或加清理任务。
3. **磁盘占用**：240px 缓存约 190MB；1280px 预览按访问量增长。上线前确认 `thumbnails` 卷剩余空间。
4. **静态缓存更新**：设置 `max-age` 后，部署新 JS/CSS 可能被浏览器沿用旧版本；建议配合 `?v=` 版本参数，或先用 `max-age=3600`。
5. **并发首次生成**：多用户同时请求同一张未缓存缩略图时会重复解码一次，属于可接受的浪费；不要用全局锁。
6. **不要用 `?t=Date.now()` 做缓存破坏**：图片内容不随标注改变，该参数只会让每次打开都重传原图。

---

## 7. 建议实施顺序

1. **先改 SQL**（`evaluation.py` + `review.py`）——收益最大、改动最小、风险可控。
2. **缩略图磁盘缓存 + JPEG**（`routes/__init__.py`）——一次改动惠及所有页面。
3. **GT 帧弹窗缩略图 + lazy**（`video_annotate.html`、`eval_task.html`）。
4. **大图预览统一 `?w=1280`，去掉 `?t=`**。
5. **静态缓存头**。
6. （可选）gzip / Nginx / 响应体瘦身。

完成 1–3 后，局域网体感应有质变：评测/复核页从「数秒白屏」降到「秒开」，图片弹窗从「卡死」降到「即时」。
