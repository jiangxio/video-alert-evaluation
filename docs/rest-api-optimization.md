# REST API 优化方案

> 制定日期：2026-08-14
> 状态：**方案待审阅，未开始改动**
> 范围：`app/` 下 11 个蓝图、共 183 个路由端点（混合架构：服务端渲染页面 + JSON API）
> 原则：框架层优先、向后兼容——共性能力下沉到 app 工厂与 helper 模块，旧端点"成功路径"不动，仅"错误路径"自动统一；需改前端契约的能力单列阶段、待确认后再做。

---

## 1. 背景（为什么做）

本项目 REST API 经探查存在以下一致性与健壮性问题（均有 file:line 证据）：

1. **响应封装不一致**：18+ 端点直接返回裸数组/裸对象，如 `return jsonify(result)`（`videos.py:171/201/286/2072`）、`return jsonify(rows)`（`algorithms.py:65/356`、`alerts.py:95`、`streaming.py:907/1010`）、`return jsonify([dict(r)...])`（`extract.py:205`）、`return jsonify(data)`（`videos.py:1298`、`auto_annotation.py:713`）；另一些端点返回 `{success:True,...}`，又有 `{error:'...'}`、`{results:[...]}` 等多种形状。客户端无法用统一契约解析。
2. **无全局 JSON 错误处理**：`app/__init__.py` 仅注册了 413 处理器。未捕获异常与 404 返回 Flask **默认 HTML 页**，JSON 客户端拿到 HTML 直接解析失败、且看不到错误原因。
3. **无分页**：所有列表端点（`list_all_videos`、`list_watermarked_videos`、alerts 列表、algorithms 列表、streaming 列表等）一次返回全量。
4. **阻塞式同步操作**：`verification.py` 的 OCR/verify/批量验证**同步阻塞**请求（`run_ocr` 跑 EasyOCR、`batch_verify` 串行遍历全部告警图）；而 `videos.py` 的水印/拼接/打包已是"后台线程 + 轮询"异步模式（`watermark_tasks` / `video_process_tasks` + `/progress` 端点），模式不统一。
5. **输入校验浅**：普遍 `data = request.get_json() or {}` 后 `data.get('field', default)`，无类型校验。streaming/eval 多处 `int(data.get(...))` 对非数字字符串抛 ValueError→500（审计低危记录：`streaming.py:992/1007/1281/1296/1360/1365` 等）。
6. **状态码不规范**：创建类操作（`upload_video`、`create_eval_set`、`concat`、`package`、`add_event`）返回 200 而非 201；DELETE 返回 200（可接受）。
7. **遗留安全/崩溃缺陷**（来自 2026-08-13 审计，部分已在工作区修复，需逐条复核）：
   - `algorithms.py` 下载用 `startswith` 字符串前缀防穿越，可被 `uploads_evil` 绕过（#13）
   - `evaluation.py:1836` `detailed_report_pdf` 缺 `@bp.route` 装饰器→下载恒 404（#9）
   - `evaluation.py:570-706` execute_task worker 无顶层 try/except→异常时 status 永远 running、连接泄漏（#17）
   - `streaming.py:43` `_is_pid_alive` 用 `os.kill(pid,0)`→Windows 上会杀掉 FFmpeg（#3）
   - `streaming.py:932-940` list_tasks 把重连中任务误标 done（#23）
   - `assistant.py:131` 缺 `import json`→GET /assistant/api/tasks 崩（#2）
   - 注：#1（verify_service 缺 import subprocess）、#5/#6/#7（路径穿越）、#14（zip-slip）已在工作区修复（已复核）。

---

## 2. 设计原则

- **框架层优先**：把"统一错误响应""分页""输入校验"做成 app 工厂的通用机制 + helper 模块，**一次接入、全局受益**，避免逐个端点手改 183 处。
- **向后兼容**：旧端点成功路径（裸数组返回）**保持不变**，前端 JS 无需改；只在错误路径上由全局处理器统一成 JSON；分页改为 **opt-in**（带 `?page=&page_size=` 才走分页结构，否则返回原数组）。
- **分阶段、可分别落地**：阶段 1-2 为核心（不改前端契约），阶段 3 opt-in 兼容，阶段 4 需前端配合、单列。
- 复用已有工具：`app/utils/__init__.py` 的 `safe_filename`/`row_to_dict`；`videos.py` 的异步水印模式作为阶段 4 参照。

---

## 3. 阶段 1：统一错误处理与响应规范（核心，不改前端）

### 3.1 新增 `app/api_response.py`——响应/错误封装 helper

提供统一信封与工厂函数，供新端点与改造端点使用：

```python
# 统一成功信封：{"success": true, "data": ...}
def ok(data=None, status=200): ...
def created(data=None): ...          # status=201

# 统一错误信封：{"success": false, "error": {"message","code","details"}}
def err(message, status=400, code=None, details=None): ...

# 分页信封：{"success": true, "data": {"items","total","page","page_size","pages"}}
def paginated(items, total, page, page_size): ...

# 分页参数解析（从 request.args），带默认值与上限
def parse_pagination(default_size=50, max_size=200): ...
```

> 设计说明：信封是**可选采用**，不强求立即迁移旧端点；但全局错误处理器（3.2）无条件产出该信封的 error 形状，确保"出错时"全站一致。

### 3.2 app 工厂注册全局 JSON 错误处理器（`app/__init__.py`）

新增一个"是否为 API 请求"判定：路径以 `/api/` 开头 **或** `Accept` 含 `application/json`。对 API 请求，以下错误统一返回 `err(...)` 信封（HTML 页面请求保持 Flask 默认行为）：

- `404` → `err("资源不存在", 404)`（API）/ 默认 HTML（页面）
- `405` Method Not Allowed → `err("方法不允许", 405)`
- `400`（Flask 解析 JSON 失败）→ `err("请求体不是合法 JSON", 400)`
- `413` → 改用 `err(...)` 信封（替换现有 413 处理器，形状统一）
- `500` 未捕获异常 → `err("服务器内部错误", 500)` + `current_app.logger.exception(...)` 记录堆栈
- Werkzeug `HTTPException` 通用兜底 → `err(...)`

**收益**：JSON 客户端不再收到 HTML 错误页；所有错误可被统一解析；零端点改动、零前端破坏（成功路径不变）。

### 3.3 状态码规范（新端点/改造端点）

- 创建类返回 201（用 `created()`）；查询/更新/删除返回 200。
- 仅影响**采用新 helper 的端点**，旧端点不改以免影响前端判定逻辑。

---

## 4. 阶段 2：安全与崩溃修复（逐条复核后修复，不改前端）

> 工作区已有未提交改动，部分审计项已修；实施时先 `git diff`/读码确认当前状态，再决定是否动手。

| # | 文件:行 | 问题 | 修法 |
|---|---|---|---|
| 2.1 | algorithms.py 下载端点 | `startswith` 前缀防穿越可绕过 | 改 `Path.resolve().relative_to(upload_dir.resolve())`，抛 ValueError 则 400 |
| 2.2 | evaluation.py:~1836 | `detailed_report_pdf` 缺 `@bp.route`→404 | 补装饰器；finally 清理临时文件 |
| 2.3 | evaluation.py:570-706 | execute_task worker 无顶层 try/except→status 卡 running、conn 泄漏 | 包顶层 try/except：异常设 status='failed'+error_message；finally 关 conn |
| 2.4 | streaming.py:43 | `_is_pid_alive` 用 `os.kill(pid,0)`→Windows 杀 FFmpeg | 改 `process.poll() is None`（用已缓存的 Popen 对象） |
| 2.5 | streaming.py:932-940 | list_tasks 把重连中任务误标 done | 区分 reconnecting 状态，不判 done |
| 2.6 | streaming.py 多处 | `int(data.get(...))` 非数字→500 | 用 `as_int()` helper（见阶段 3.2）或 try/except→400 |
| 2.7 | assistant.py:131 | 缺 `import json`→tasks 接口崩 | 补 import（复核是否已修） |

---

## 5. 阶段 3：列表分页与输入校验（opt-in，向后兼容）

### 5.1 分页（opt-in，不改默认行为）

在 `api_response.py` 的 `parse_pagination()` 基础上，为最重的列表端点接入分页：`list_all_videos`、`list_watermarked_videos`、alerts 列表、algorithms 列表。

行为：
- **不带** `?page=` 参数 → 返回原裸数组（前端零改动）。
- **带** `?page=&page_size=` → 返回 `paginated(...)` 信封（`{items,total,page,page_size,pages}`）。

实现要点：先在 SQL 层加 `LIMIT/OFFSET` 与 `COUNT(*)`；无分页参数时走原查询路径。

### 5.2 输入校验 helper

`api_response.py` 新增：

```python
def as_int(value, default=None, field=None): ...   # 非数字→None 或 400
def require_fields(data, fields): ...              # 缺字段→raise→400
```

在 streaming/evaluation 的 `int(data.get(...))` 处替换为 `as_int()`，避免 ValueError→500。

---

## 6. 阶段 4：阻塞操作异步化（需前端配合，单列、待确认）

将 `verification.py` 的 OCR/verify/批量验证改为**后台线程 + 任务状态轮询**，复用 `videos.py` 的水印异步模式（`watermark_tasks` dict + `/api/.../progress` 端点）。

> ⚠️ 改变端点契约：原 `POST /api/alerts/<id>/ocr` 同步返回结果，改后返回 `task_id`，需轮询 `/progress`。**需同步改前端**，故单列，待确认是否纳入本次范围。
>
> 可选折中（不改契约）：仅给同步 OCR/verify 的 subprocess 加 **timeout**，避免无限挂起；不改返回方式。

---

## 7. 改动文件清单（阶段 1-3，核心）

| 文件 | 改动 |
|---|---|
| `app/api_response.py` | **新增**：`ok/created/err/paginated/parse_pagination/as_int/require_fields` |
| `app/__init__.py` | 注册全局 JSON 错误处理器（404/405/400/413/500/HTTPException），API 请求返回信封；替换现有 413 处理器 |
| `app/routes/algorithms.py` | 下载路径 `relative_to` 校验（2.1） |
| `app/routes/evaluation.py` | 补 PDF 路由装饰器+临时文件清理（2.2）；execute_task 顶层 try/except（2.3） |
| `app/routes/streaming.py` | `_is_pid_alive` 改 `poll()`（2.4）；list_tasks 重连状态（2.5）；`int()` 改 `as_int()`（2.6） |
| `app/routes/assistant.py` | 补 `import json`（2.7，若未修） |
| 列表端点（videos/alerts/algorithms/streaming） | opt-in 分页接入（5.1） |

阶段 4 若纳入：另改 `app/routes/verification.py` + 对应前端模板 + 可能新增 task 状态表/内存结构。

---

## 8. 验证方式

1. **全局错误处理**：`curl -i http://localhost:8080/api/notexist` → 404 返回 JSON 信封（非 HTML）；`curl -i -X POST http://localhost:8080/api/eval-sets -H 'Content-Type: application/json' -d 'BADJSON'` → 400 JSON 信封。
2. **成功路径不回归**：`GET /videos/api/all` 仍返回裸数组（前端不改）；`GET /videos/api/all?page=1&page_size=5` 返回分页信封。
3. **安全修复**：构造 `?type=../` 下载请求 → 400；eval PDF 下载 → 200 非 404。
4. **崩溃修复**：streaming 传非数字参数 → 400 非 500；execute_task 注入异常 → status='failed' 非 running。
5. **冒烟**：`python run.py` 起服务，遍历各蓝图主页与核心 API，确认无回归。
6. `git diff --stat` 确认改动范围与清单一致，未越界改 `scripts/`。

---

## 9. 不在本次范围

- 全量 RESTful URL 重构（资源化路径、HTTP 方法语义化）——会大面积破坏前端，不做。
- 强制迁移所有旧端点到新信封——仅错误路径统一、新端点采用，旧成功路径保持。
- 鉴权/认证体系（项目当前无 auth，按现状）。
- 数据库连接池/并发模型改造。
