# 交接文档：REST API 一致性与健壮性修复

> 本文档对应 `jianyi.png` 中的审计建议清单。当前项目处于参赛收尾阶段，因此本修复计划采用"抓大放小"原则：**优先修复会导致功能断裂或状态卡死的崩溃/安全缺陷；工程规范债（响应格式统一、分页、状态码等）暂缓到赛后处理**。
>
> 实施者：按本文档 P0 部分逐条修复并验证即可；P1/P2 需与用户确认后再启动。

---

## 一、背景与修复原则

### 问题来源

2026-08-13 审计发现项目 REST API 存在 7 类一致性与健壮性问题：

1. 响应封装不一致
2. 无全局 JSON 错误处理
3. 无分页
4. 阻塞式同步操作
5. 输入校验浅
6. 状态码不规范
7. **遗留安全/崩溃缺陷**（部分已在工作区修复，需逐条复核）

### 修复原则

| 优先级 | 处理策略 | 原因 |
|--------|---------|------|
| **P0** | 必须修复 | 会直接导致评测流程断裂、状态卡死、页面 500、跨平台崩溃，影响参赛评分中的「核心功能」和「工具落地可用性」 |
| **P1** | 有余力时修复 | 输入校验浅可能抛 500，但截图标注为低危，且当前核心异常测试已通过 |
| **P2** | 暂缓至赛后 | 响应格式统一、全局错误处理、分页、状态码规范化、异步化——改动面大、收益不直接关联评分项 |

---

## 二、P0：遗留崩溃/功能断裂缺陷修复（5 条）

### 2.1 `evaluation.py:1836` —— `detailed_report_pdf` 缺少路由装饰器

**位置**：`app/routes/evaluation.py:1836`

**现状**：
```python
@bp.route('/api/tasks/<int:task_id>/chat-sessions/<int:session_id>', methods=['DELETE'])
def delete_chat_session(...):
    ...
    return jsonify({'success': True})

def detailed_report_pdf(task_id):   # ← 缺少 @bp.route
    """生成详细报告 PDF（Playwright 渲染）"""
    ...
```

**影响**：PDF 报告下载接口未注册，客户端请求该端点恒返回 404，**报告生成环节断裂**。

**修复方案**：在 `detailed_report_pdf` 上方补装饰器。需确认前端/客户端实际调用的 URL 和 Method：
- 如果前端用 `POST /api/tasks/<task_id>/detailed_report_pdf`：
  ```python
  @bp.route('/api/tasks/<int:task_id>/detailed_report_pdf', methods=['POST'])
  def detailed_report_pdf(task_id):
      ...
  ```
- 如果前端用 `GET`：
  ```python
  @bp.route('/api/tasks/<int:task_id>/detailed_report_pdf', methods=['GET', 'POST'])
  ```

> **实施注意**：先搜索前端调用处（`detailed_report_pdf` 字符串），确认 method 和 path 后再写装饰器，避免 405。

**验证方式**：
1. 完成一次评测任务
2. 点击"生成详细报告 PDF"
3. 确认请求返回 200 并能下载 PDF

---

### 2.2 `evaluation.py:570-706` —— 评测 worker 无顶层 try/except

**位置**：`app/routes/evaluation.py:570-706`（`_worker` 函数整体）

**现状**：`_worker()` 内部没有任何顶层异常捕获。一旦命中判定、指标计算或数据库更新过程中抛异常：
- 数据库连接 `conn` 不会 `close()`，造成连接泄漏；
- `eval_tasks.status` 永远停留在 `evaluating`；
- `_eval_progress[task_id]['running']` 不会置为 `False`，前端进度卡住。

**修复方案**：给 `_worker` 加顶层 try/except/finally，异常时把任务标为 `failed` 并记录错误信息，finally 里确保连接关闭、进度状态重置。

```python
def _worker():
    import sqlite3
    conn = None
    error_msg = None
    try:
        conn = sqlite3.connect(str(DATABASE_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        # ... 原有逻辑 ...
    except Exception as e:
        error_msg = f"评测执行异常: {e}"
        import traceback
        traceback.print_exc()
    finally:
        if conn is not None:
            try:
                if error_msg:
                    cur = conn.cursor()
                    cur.execute(
                        "UPDATE eval_tasks SET status = 'failed', error_message = ? WHERE id = ?",
                        (error_msg, task_id)
                    )
                    conn.commit()
            except Exception:
                pass
            conn.close()
        with _eval_lock:
            _eval_progress[task_id]['running'] = False
```

> **实施注意**：不要改变原有正常流程；仅在异常分支把状态置为 `failed`。`error_message` 列若不存在需用 `try/except` 兼容旧库。

**验证方式**：
1. 构造一个会让 `_worker` 抛异常的评测任务（例如：删除 `eval_gt_events` 表让 SQL 失败，或构造空 GT 但让合并逻辑异常）。
2. 触发评测执行。
3. 确认：
   - 数据库中该任务 `status = 'failed'`；
   - `error_message` 有内容；
   - 前端进度不再卡在"运行中"；
   - 后端无未关闭连接堆积。

---

### 2.3 `streaming.py:38-46` —— `_is_pid_alive` Windows 兼容性

**位置**：`app/routes/streaming.py:38-46`

**现状**：
```python
def _is_pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False
```

**影响**：在 Windows 上，`os.kill(pid, 0)` 会抛 `ValueError` 或 `OSError`，导致进程状态检测崩溃或误判。

**修复方案**：使用跨平台的 `psutil` 或 `subprocess` 方案。若不想引入新依赖，优先用 `psutil`（已在主项目requirements里的话），否则用平台判断：

```python
import sys

def _is_pid_alive(pid: int) -> bool:
    """跨平台检查 PID 是否存活。"""
    if not pid:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(1, False, pid)
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False
```

> 更简单且推荐的做法：直接引入 `psutil`（`pip install psutil`），`psutil.pid_exists(pid)` 跨平台。若项目已用 psutil，请改用 psutil。

**验证方式**：
1. Linux：启动一个推流任务，确认 `_is_pid_alive(pid)` 返回 True；杀掉 FFmpeg 后返回 False。
2. Windows：同样流程，确认不再抛 `ValueError`。

---

### 2.4 `streaming.py:893-904` —— `list_tasks` 把重连中/已失败任务误标为 `done`

**位置**：`app/routes/streaming.py:872-904`

**现状**：
```python
@bp.route("/api/tasks", methods=["GET"])
def list_tasks():
    db = get_db()
    cur = db.cursor()
    app = current_app._get_current_object()
    _sync_running_status(db, app=app)   # ← 这里会把死亡 running 任务标 failed 或触发重连
    ...
    for r in rows:
        if r.get("status") == "running" and r.get("pid"):
            if not _is_pid_alive(r["pid"]):
                cur.execute(
                    "UPDATE stream_tasks SET status = 'done', ended_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (r["id"],),
                )
                db.commit()
                r["status"] = "done"
```

**影响**：`_sync_running_status` 已经把死亡任务处理为 `failed` 或触发重连；但 `list_tasks` 后续遍历又把同一批任务直接改为 `done`，导致：
- 本应显示"失败"的任务显示"已完成"；
- 正在自动重连的任务被误判为已结束；
- 与 `_sync_running_status` 逻辑冲突。

**修复方案**：删除 `list_tasks` 中这段独立的死亡 PID 处理逻辑，完全交给 `_sync_running_status` 统一处理。若担心 `_sync_running_status` 未覆盖，可确保 `_sync_running_status` 在 `list_tasks` 返回前被调用即可。

```python
# 删除以下整段：
# for r in rows:
#     if r.get("status") == "running" and r.get("pid"):
#         if not _is_pid_alive(r["pid"]):
#             cur.execute("UPDATE stream_tasks SET status = 'done' ...")
```

**验证方式**：
1. 启动一个推流任务。
2. 手动 kill 掉 FFmpeg 进程。
3. 调用 `GET /streaming/api/tasks`：
   - 任务应被 `_sync_running_status` 标为 `failed`（若超过重试次数）或触发重连；
   - 绝不应该出现 `status = 'done'`。

---

### 2.5 `assistant.py:114` —— `/assistant/api/tasks` 缺少 `import json`

**位置**：`app/routes/assistant.py:114-135`

**现状**：文件头部没有 `import json`，但 `api_list_tasks` 中使用了 `json.loads`：

```python
# 文件头 imports（无 json）
from flask import Blueprint, request, jsonify, render_template, session
from app.database import get_db
...

@bp.route('/api/tasks', methods=['GET'])
def api_list_tasks():
    ...
    for row in cursor.fetchall():
        t = dict(row)
        try:
            t['params'] = json.loads(t['params']) if t['params'] else {}   # ← NameError
        except Exception:
            t['params'] = {}
```

**影响**：请求 `GET /assistant/api/tasks` 会直接抛 `NameError: name 'json' is not defined`，返回 500。

**修复方案**：在 `assistant.py` 文件头部加入 `import json`。

**验证方式**：
1. 访问 `GET /assistant/api/tasks`。
2. 确认返回 200 和 JSON 任务列表，不再 500。

---

## 三、P1：输入校验强化（有余力时做）

### 3.1 问题描述

多处接口使用 `int(data.get(...) or 1)`，对非数字字符串会抛 `ValueError` → 500。例如 `streaming.py:954`：

```python
loop_count = int(data.get("loop_count") or 1)
```

当客户端传入 `"abc"` 时，`data.get("loop_count")` 返回 `"abc"`，`int("abc")` 抛 `ValueError`，Flask 默认返回 HTML 500 页面。

### 3.2 修复方案

对可能来自客户端的整型/浮点参数字段，加一层安全转换：

```python
def _safe_int(value, default=1):
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default

# 使用
loop_count = _safe_int(data.get("loop_count"), 1)
```

### 3.3 建议优先处理的端点

按截图提示，重点关注 `streaming.py` 中的这些位置（行号可能随版本漂移，请以当前代码为准）：

- `create_task` 中的 `loop_count`、`source_id` 等参数
- `eval` 相关接口中的时间/索引参数
- 其他使用 `int(data.get(...))` 且未做异常处理的端点

### 3.4 验证方式

对每个修复的端点，构造异常输入测试：
- `loop_count = "abc"` → 应返回 400 或采用默认值，不应 500
- `loop_count = null` → 应使用默认值
- `loop_count = 3.5` → 建议按默认值或返回 400

---

## 四、P2：工程规范债（暂缓至赛后）

以下问题真实存在，但属于代码质量债，**当前参赛阶段不建议投入**：

| 问题 | 暂缓原因 |
|------|---------|
| 响应封装不一致 | 需改 18+ 端点和前端调用，改动面大，易引入回归 |
| 无全局 JSON 错误处理 | 需统一 404/500 处理，可能改变现有前端错误解析逻辑 |
| 无分页 | 当前数据规模无性能问题；赛后数据集扩大再补 |
| 阻塞式同步操作 | `run_ocr`/`batch_verify` 同步是已知设计；改异步需引入任务队列 |
| 状态码不规范 | 201/200 区别对评分无影响 |

---

## 五、验证总清单

完成 P0 修复后，按以下清单验证：

- [ ] `POST /api/tasks/<id>/detailed_report_pdf`（或实际 path）返回 200 并可下载 PDF
- [ ] 构造异常评测任务，执行后 `eval_tasks.status = 'failed'`，前端不再卡住
- [ ] 推流任务 PID 死亡后，状态为 `failed` 或触发重连，**不会**变成 `done`
- [ ] `GET /assistant/api/tasks` 返回 200 JSON
- [ ] Linux 下 `_is_pid_alive` 正确判断 FFmpeg 生死
- [ ] （如条件允许）Windows 下 `_is_pid_alive` 不再抛 `ValueError`
- [ ] 原有目标检测/视频评测核心流程回归通过

---

## 六、改动文件清单

| 文件 | 改动内容 | 优先级 |
|------|---------|--------|
| `app/routes/evaluation.py` | 补 `detailed_report_pdf` 路由装饰器；给 `_worker` 加顶层 try/except/finally | P0 |
| `app/routes/streaming.py` | 修复 `_is_pid_alive` 跨平台；删除 `list_tasks` 中误标 `done` 的逻辑 | P0 |
| `app/routes/assistant.py` | 文件头部加 `import json` | P0 |
| `app/routes/streaming.py` | 多处 `int(data.get(...))` 改为安全转换 | P1 |
| `tests/` | 补充对应异常/回归测试 | P0/P1 |

---

## 七、注意事项

1. **先确认再改**：`detailed_report_pdf` 的装饰器 path/method 必须和前端调用一致，不要凭假设写。
2. **最小改动**：只修 P0 中列出的具体函数，不要顺手重构相邻代码。
3. **数据库兼容**：`error_message` 等列若旧库不存在，用 `try/except` 包裹，参考 `evaluation.py` 中已有的 `updated_at` 兼容写法。
4. **Windows 测试**：若现场演示用 Windows，`_is_pid_alive` 必须测；若只用 Linux，可简化修复但建议做跨平台。
5. **回归测试**：P0 修复后必须跑一次完整视频评测+报告生成+推流任务生命周期，确认无回归。
