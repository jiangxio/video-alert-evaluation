# /api/v1 auto-annotation 改造文档

> ⚠️ **错误码已改为方案3**（HTTP 状态即 `code` + 可选 `error_code` 字符串）。下方 5 位 `H FF SS` 码列已废弃，以代码实际行为为准；完整规范见 [错误码文档](./rest-api-error-codes.md)。

> REST API 改造第 8 模块。把 `app/routes/auto_annotation.py` 的 10 个 JSON 端点资源化进 `/api/v1/auto-annotation/*`，统一信封 + 5 位错误码（FF=10 auto-annotation-tasks）。auto-annotation 是高风险模块（`start_task` 内联起 `_do_auto_annotation` daemon 线程跑真 ffmpeg 抽帧 + 多模态模型 API；`convert_to_events` 起 `_batch_capture_gt_frames` 抓帧线程；模块级 `_auto_anno_tasks`/`_auto_anno_lock`/`_stop_requested`/`_current_task_id`/`_task_queue` 状态），但与 streaming 不同，**它的 `start_task` 没有干净的模块级启动函数**（校验/建库/排队/起线程全内联在路由），无法函数级复用 → 走 OCR 那套**路由级 `call_old_view` 委托**（4 个端点），纯查询/CRUD（6 个）原位重写。旧端点保留并自动加弃用 header。

## 变更总览（TL;DR）

> 一句话：旧 `app/routes/auto_annotation.py` 的 10 个 JSON 端点资源化进 `/api/v1/auto-annotation/*`，统一信封 + 5 位码（FF=10，11 码）；**4 委托 + 6 原位重写**（start 无干净启动函数 → 路由委托，区别于 streaming 的函数级复用）；列表用 **SQL 层 `LIMIT/OFFSET + COUNT(*)` 真分页**（纠正 streaming 的 fetchall+切片）；4 处旧 400 修正（2→404、2→409）、DELETE→204、成功保 200。单测 35 绿 + 全套 v1 回归 146 绿。

- **架构决策**：4 委托 + 6 原位重写。`start`/`stop`/`status`/`convert` 委托旧视图（起后台线程 / 操作模块级任务态），只套信封 + 映射错误码，不抽不改旧逻辑；`videos-without-events`/`tasks` 列表/`by-video`/`get-json`/`delete`/`clear` 原位重写复用 `get_db`。理由：`start_task` 的启动逻辑全内联在路由（无 `_start_task_internal` 式干净函数）→ 函数级复用不可行，走 OCR 的路由委托；纯查询/CRUD 无线程/锁 → 原位重写更直接。
- **端点/动词/码**：10 端点（FF=10，11 码）；stop 用集合动作 `tasks:stop`、convert 用 `tasks/<id>:convert-to-events`、clear 用 `tasks/<id>:clear`、status 是单例 `GET /status`、DELETE→204；start 创建+启动合一（POST `/tasks`，对齐旧 `/api/start` 语义不改 201/202）。
- **真分页（本轮新决策）**：列表端点 `_paginate(db, base_sql, order_sql, params, page, page_size, mapper)` —— `COUNT(*) FROM (base) _c` 取 total、`base + order + LIMIT ? OFFSET ?` 取当页，**不 fetchall 后 Python 切片**（streaming 那套 `_slice_page` 在大表上等于全表加载，且 `list_tasks` 还丢了旧 `LIMIT 50`）。`COUNT(*) FROM (含 GROUP BY/HAVING 的子查询)` 计分组数，`LIMIT/OFFSET` 在 `GROUP BY/HAVING/ORDER BY` 后生效。
- **踩坑（2，均修在 conftest）**：①`auto_annotation.DATABASE_PATH` 双绑定 patch（类比 OCR alerts/streaming；注：auto-annotation 后台线程全用 `sqlite3.connect(str(DATABASE_PATH))` 直连、不走 `get_db()`，故**无 assistant_tools #4 那种 app_context 问题**，别混）；②`behavior_analysis_service.DEFAULT_CONFIG_PATH`（= 真实 `app/auto_anno_config.json`）重定向到 tmp，防 `save_config`（start 传 api_key 时）改写仓库配置。
- **新发现 bug（#27，已加 bug-audit，未修）**：`stop_task` 缺 `global _stop_requested` → `_stop_requested=True` 是局部赋值、模块全局不翻转 → worker 停止判断永假 → **stop 信号传不到 worker，中断静默失效**。测试只断响应契约（200+task_id），不断不会变的内部态。
- **测试**：单测 **35 passed**；全套 v1 回归 **146 passed, 1 skipped**（111 + 35，无回归）。委托测试 stub worker 成 no-op（避开真 ffmpeg/模型 API/仓库写/Windows 文件锁），只验信封/状态码/错误码/委托真触发。
- **状态**：未 git commit（按 CLAUDE.md 等用户授权）。下一模块：extract/tasks。

## 1. 背景

旧 `auto_annotation` 蓝图（`/auto-annotation`）把自动标注页面 + 10 个 JSON 端点混在一个蓝图，返回裸 JSON + HTTP 码（`[rows]` / `{success,...}` / `{error}`），无统一信封、无结构化错误码，若干前置条件失败塞 400。本模块把 10 个 JSON 端点资源化进 `/api/v1/auto-annotation`；页面路由 `GET /auto-annotation/`、`GET /auto-annotation/config/<id>` 不在迁移范围。新旧并行：旧端点保留并自动加弃用 header（`deprecation.py:19` 已预置 `/auto-annotation/api/` → `/api/v1/auto-annotation`），前端继续用旧 URL。

范围：**URL 资源化 + 正确 HTTP 动词（`:action` 后缀、DELETE→204）+ 统一信封 + 5 位码 + 个别 400→404/409 修正 + 真分页**，不改交互语义（状态修正均对齐 videos/streaming 先例，见 §6）。

## 2. 改动文件清单

| 文件 | 类型 | 作用 |
|---|---|---|
| `app/api/v1/auto_annotation.py` | 新建 | 10 端点（FF=10）：4 委托 + 6 原位重写，`_paginate`/`_raise_msg`/`_clear_frames_dir` 辅助 |
| `app/api/v1/__init__.py` | 改 | `BLUEPRINTS` 加 `auto_annotation.bp`，`import` 加 `auto_annotation` |
| `tests/conftest.py` | 改 | `app` fixture +2 patch：`auto_annotation.DATABASE_PATH`→tmp（双绑定）、`behavior_analysis_service.DEFAULT_CONFIG_PATH`→tmp（写目标） |
| `tests/test_api_v1_auto_annotation.py` | 新建 | 35 用例（4 委托 + 6 重写；`_stub_worker`/`_reset_auto_anno_state` autouse + `_proj_root` per-test） |
| `docs/rest-api-error-codes.md` | 改 | FF 表 10 行 待做→已用 + FF=10 码表专节 |

**未触碰**：`app/routes/auto_annotation.py` 旧端点（新旧并行）、`app/routes/videos.py`（`generate_ground_truth_json`/`_capture_gt_frames_async` 原样复用）、`app/services/behavior_analysis_service.py`。

## 3. 端点详情（10 个）

蓝图 `api_v1_auto_annotation`，`url_prefix=/api/v1`。

| # | 方法 + 路径 | 旧视图 | 策略 | 成功响应 | 错误码（5 位 FF=10） |
|---|---|---|---|---|---|
| 1 | `GET /auto-annotation/videos-without-events` | `list_videos_without_events` | 重写·分页 | `paginated`（`id`=wm_id, `video_db_id`=视频主键） | — |
| 2 | `POST /auto-annotation/tasks` | `start_task` | 委托 | `ok({task_id,queued})`（200，创建+启动合一） | `11000` 未选视频；`11001` 抽帧间隔；`11002` 合并间隔；`11003` 至少一类型；`21021` 视频不存在(404)；`21022` 尚无水印(404) |
| 3 | `GET /auto-annotation/tasks` | `list_tasks` | 重写·分页 | `paginated`（含 `video_filename`） | — |
| 4 | `GET /auto-annotation/tasks/<id>/json` | `get_task_json` | 重写 | `ok(GT内容)` | `21020` 任务不存在(404)；`21023` JSON 不存在(404)；`41080` 读取失败(500) |
| 5 | `GET /auto-annotation/videos/<vid>/tasks` | `list_tasks_by_video` | 重写·分页 | `paginated`（done+有结果） | — |
| 6 | `DELETE /auto-annotation/tasks/<id>` | `delete_task` | 重写 | `no_content()`(204) | `21020` 不存在(404) |
| 7 | `POST /auto-annotation/tasks/<id>:clear` | `clear_intermediate` | 重写 | `ok({task_id})`（幂等） | —（task_id 入路径，旧「缺 task_id」不可达） |
| 8 | `POST /auto-annotation/tasks:stop` | `stop_task` | 委托 | `ok({task_id})` | `31040` 无运行任务(409) |
| 9 | `GET /auto-annotation/status` | `get_status` | 委托 | `ok(引擎态)` | —（旧恒 200） |
| 10 | `POST /auto-annotation/tasks/<id>:convert-to-events` | `convert_to_events` | 委托 | `ok({event_count})` | `21020` 不存在(404)；`31041` 未完成(409)；`21023` JSON 不存在(404) |

> 路径前缀 `/api/v1`。RPC 动作用 `:action` 后缀（`:stop`/`:clear`/`:convert-to-events`）；子资源用名词（`/json`）；`status` 是引擎单例（`GET /auto-annotation/status`）；`by-video` 作视频子资源（`/videos/<vid>/tasks`）。

## 4. 请求方式（verb）分析

| 旧端点 | 旧方法 | 新方法 | 变化 | 理由 |
|---|---|---|---|---|
| `list_videos_without_events`/`list_tasks`/`list_tasks_by_video` | GET | GET | 不变（加真分页） | 幂等列表；v1 约定列表加分页信封 |
| `start_task` | POST `/api/start` | POST `/tasks` | URL 资源化 | 创建+启动合一（旧语义），保 200（对齐 OCR `ocr:batch`「不改 202」先例） |
| `stop_task` | POST `/api/stop` | POST `/tasks:stop` | 集合动作 | 旧 stop 无参（停当前任务）→ tasks 集合上的动作 |
| `get_status` | GET `/api/status` | GET `/status` | 单例资源 | 引擎态（current+queue+last），非任务集合操作 |
| `get_task_json` | GET `/api/json/<id>` | GET `/tasks/<id>/json` | 子资源 | GT JSON 是 task 的子资源 |
| `list_tasks_by_video` | GET `/api/tasks/video/<vid>` | GET `/videos/<vid>/tasks` | 子资源 | 视频的标注任务子资源（保两列表，不合并 filter） |
| `delete_task` | DELETE | DELETE | 响应 200→**204** | 对齐 alerts/videos/streaming/algorithms |
| `clear_intermediate` | POST `/api/clear-intermediate` body{task_id} | POST `/tasks/<id>:clear` | task_id 入路径 | 动作；保 POST（保「清空=动作」语义，不引入 `/frames` 子资源） |
| `convert_to_events` | POST `/api/convert-to-events/<id>` | POST `/tasks/<id>:convert-to-events` | URL 资源化 | 动作；保动作名 |

**请求体格式**：全 JSON body（`request.get_json()`），无文件上传。委托端点的 body 由旧视图自读（新端点透传）。

## 5. 错误码（5 位 `H FF SS`，详见 `docs/rest-api-error-codes.md`）

FF=10 直接用新码（分配表已预留，本轮启用）。共 11 个码：

| 码 | 含义 | HTTP | 端点 |
|---|---|---|---|
| `11000` | 未选择视频（video_db_id 缺失） | 400 | start |
| `11001` | 抽帧间隔至少为1秒（frame_interval_sec<1） | 400 | start |
| `11002` | 合并间隔不能为负数（merge_interval_sec<0） | 400 | start |
| `11003` | 至少选择一个事件类型（event_types 为空） | 400 | start |
| `21020` | 任务不存在 | 404 | delete / get-json / convert |
| `21021` | 视频不存在 | 404 | start |
| `21022` | 尚未生成水印视频（旧 400→404） | 404 | start |
| `21023` | JSON 文件不存在（旧 400→404） | 404 | get-json / convert |
| `31040` | 当前没有运行中的任务（stop，旧 400→409） | 409 | stop |
| `31041` | 任务尚未完成（convert，旧 400→409） | 409 | convert |
| `41080` | JSON 文件读取失败 | 500 | get-json |

**委托端点错误映射**：`_raise_msg(body, msg_to_code)` 按旧视图 `error` 文案子串匹配 `(code, http_status)`，无匹配走 `41080/500`。`start` 在 400/404 各多条（4×400 + 2×404），用 dict 多码映射；`stop`/`convert` 的旧 400 修正为 409 时**映射项自带新 http_status**（不沿用旧 code），H 位对齐。仿 alerts_ocr `_raise_from_legacy`，但 alerts_ocr 单端点单错误用元组、本模块多错误用 dict。

## 6. 关键设计：4 委托 + 6 原位重写 + 真分页 + 语义修正

### 4 委托 + 6 原位重写（与 streaming 的函数级复用对比）

streaming 有干净内部函数 `_start_task_internal(task_id,use_resume)->(success,result)` 可直接调，故函数级复用更优（精确码 + 409 修正）。auto-annotation 的 `start_task` 把校验→`load_anno_config`→建库行→模块态更新→排队/立即判断→`threading.Thread(target=_do_auto_annotation,...).start()` **全内联在路由**，无干净启动函数 → 函数级复用不可行，走 OCR 的路由级 `call_old_view` 委托：旧视图在同一个 request context 内运行（`request.get_json`/`get_db`/`current_app` 可用），新端点拆其 `(jsonify(...)[,code])` 为 `(body, status)`，按 status+文案子串映射 5 位码 + 套信封。

`convert_to_events` 同样起后台线程（`_batch_capture_gt_frames`）→ 委托。`stop`/`status` 读模块级任务态（`_auto_anno_lock`/`_current_task_id`/`_task_queue`/`_stop_requested`）→ 委托（与状态机绑定，保持一致）。纯查询/CRUD（list 系列/get-json/delete/clear）无线程/锁 → 原位重写复用 `get_db`。

### 真分页（纠正 streaming 的 fetchall+切片）

streaming 的 `_slice_page` 是 `fetchall()` 后 Python 切片——SQL 无 `LIMIT/OFFSET`，等于全表读进内存再丢大部分，**不是真分页**；且 `list_tasks` 我初版还丢了旧 `LIMIT 50`，更糟。本轮改用 SQL 层分页：

```python
def _paginate(db, base_sql, order_sql, params, page, page_size, mapper=dict):
    cur = db.cursor()
    cur.execute(f"SELECT COUNT(*) FROM ({base_sql}) _c", params)   # total
    total = cur.fetchone()[0]
    offset = (page - 1) * page_size
    cur.execute(f"{base_sql} {order_sql} LIMIT ? OFFSET ?",
                (*params, page_size, offset))                       # 当页
    return paginated([mapper(r) for r in cur.fetchall()], total, page, page_size)
```

`base_sql` 不含 ORDER/LIMIT（COUNT 子查询与 items 查询共用）。`COUNT(*) FROM (含 GROUP BY/HAVING 的子查询)` 计分组数，`LIMIT/OFFSET` 在 `GROUP BY/HAVING/ORDER BY` 后生效——SQLite 都支持。后续模块列表端点应直接用此式，别再抄 `_slice_page`。

### 经先例授权的语义修正（新端点专属，旧不动）

5 位码 H 位须与 http_status 对应。本轮修正（均对齐既有先例）：
- `21022` 尚未生成水印视频：400→**404**（对齐 videos 族 `20121 尚未生成水印视频 404`）。
- `31040` 当前没有运行中的任务：400→**409**（对齐 streaming `30904 任务未在运行中`）。
- `31041` 任务尚未完成：400→**409**（状态冲突，任务未 done 不能 convert）。
- `21023` 结果 JSON 不存在：400→**404**（对齐同模块 `get-json` 的 404）。
- DELETE：200→**204**（对齐 alerts/videos/streaming/algorithms）。
- start/stop/convert 成功：保 **200**（对齐 OCR `ocr:batch`「200 不改 202」先例，不改交互语义）。

## 7. 实现中踩的坑（根因 + 修复）

### 坑1：`auto_annotation.DATABASE_PATH` 双绑定 patch（类比 OCR alerts/streaming）

- **现象**：`auto_annotation.py:17` `from app.database import DATABASE_PATH` 导入期捕获，后台 worker `_do_auto_annotation`/`_process_queue`/`_batch_capture_gt_frames` 及其调用的 `app/routes/videos.py:_capture_gt_frames_async`（line 1161→1174）全用 `sqlite3.connect(str(DATABASE_PATH))` 直连此拷贝，不随 `database.DATABASE_PATH` 更新 → 写真实 `benchmark.db`。
- **修复**：`tests/conftest.py` 的 `app` fixture 加 `monkeypatch.setattr("app.routes.auto_annotation.DATABASE_PATH", db_path)`，紧跟现有 `streaming.DATABASE_PATH` patch。
- **注**：auto-annotation 后台线程**全用 `sqlite3.connect` 直连、不走 `get_db()`**，故**无 assistant_tools #4 那种 app_context 问题**（#4 是 `assistant_tools.py` 在线程内调 `get_db()` 缺 `app.app_context()`，与本模块无关，勿混）。

### 坑2：`behavior_analysis_service.DEFAULT_CONFIG_PATH` 写目标重定向

- **现象**：`behavior_analysis_service.py:13` `DEFAULT_CONFIG_PATH = app/auto_anno_config.json`（真实仓库文件）。`load_config` 读它、`save_config` 写它。`start_task` 委托时调 `load_anno_config()`；请求体带 `api_key`/`base_url`/`model` 时调 `save_config()` → 改写仓库配置文件，违反「测试恢复原状」。
- **修复**：`app` fixture 加 `monkeypatch.setattr("app.services.behavior_analysis_service.DEFAULT_CONFIG_PATH", tmp_path/"auto_anno_config.json")`。重向后 `load` 读 tmp（不存在→`except` 返回默认值）、`save` 写 tmp，不碰真实配置。类比 `ALERT_TYPES_CONFIG_PATH`/`api_config_service.ENV_PATH` 的同套修法。

## 8. 测试方案

- **委托端点（16 用例）**：`_stub_worker` **autouse** 把 `_do_auto_annotation`+`_batch_capture_gt_frames` 替成 `lambda *a,**k:None`（autouse 防漏标导致 start 成功用例起真 worker 跑 ffmpeg/模型 API）。`_reset_auto_anno_state` autouse teardown 清模块态（stub 不开 sqlite 连接故无需等线程，防御性清态防跨用例串）。只验信封/状态码/错误码/委托真触发（task 落库、模块态更新）。start 8 用例（立即 200/queued + 4×400 校验 + 404 视频 + 404 无水印）、stop 2（200 + 409 无运行）、status 2（idle + with-current）、convert 4（404/409/404/200+events 入库）。
- **原位重写端点（19 用例）**：seed `videos`+`watermarked_videos`（`output_path` 指向 tmp 真文件）+ `auto_annotation_tasks`/`auto_annotation_frames` 行。`videos-without-events` 4（空/有数据/有事件被排除/分页）、`tasks` 列表 3（空/有/分页）、`by-video` 3（空/done/排除非 done）、`get-json` 3（404/文件缺失/正常）、`delete` 3（404/204+行删/帧文件删）、`clear` 3（200+帧行删/无目录幂等/重复幂等）。clear/delete 的 `PROJECT_ROOT` 用 `_proj_root` per-test `monkeypatch.setitem(app.config,"PROJECT_ROOT",tmp)` 重定向到 tmp（不污染全局/仓库，可造帧文件测删除）。
- **真分页验证**：插 25 行验 `total=25 / page1 has_next=True / page2 has_next=False`。
- **隔离**：conftest 双绑定 patch（坑1）+ DEFAULT_CONFIG_PATH 重定向（坑2）+ `_stub_worker` autouse + `_reset_auto_anno_state` autouse + `_proj_root` per-test。

> 35 用例全快测无 slow（无 EasyOCR/真 ffmpeg/真模型 API，worker 被 stub）。`py` 启动器（Python 3.13.14, pytest 9.1.1）。

## 9. 测试结果

| 命令 | 结果 |
|---|---|
| `py -m pytest tests/test_api_v1_auto_annotation.py -q` | **35 passed**（31 warnings, 12.70s） |
| 全套 v1 回归 `py -m pytest tests/ -k "api_v1" -q` | **146 passed, 1 skipped**（111 + 本轮 35，无回归；1 skip 是 streaming 真实冒烟 env-var opt-in） |

> 隔离验证：`git status --short benchmark.db app/auto_anno_config.json config/alert_types.json` 应空（真实库/配置未被测试触碰——conftest patch 全重定向到 tmp）。

## 10. 已知问题 / 盲区（不在本模块范围，由 bug-audit 另行修）

1. **#27（中危，本轮新发现，已加 bug-audit）**：`stop_task`（`auto_annotation.py:588-596`）缺 `global _stop_requested` → `_stop_requested=True` 是局部赋值、模块全局不翻转 → worker `_do_auto_annotation` 的 `if _stop_requested and _current_task_id==task_id`（line 208）永假 → **stop 信号传不到 worker，中断静默失效**。修法：函数头加 `global _stop_requested`。测试只断响应契约（200+task_id），不断不会变的内部态。
2. **①（低危）**：`get_status`（`auto_annotation.py:~632`）`latest_tid = max(keys, key=lambda k: dict[k].get("updated_at", 0))`，而任务 dict **无 `updated_at` 字段** → 全映射 0 → `max` 平局取第一个（最旧任务）→ `last_task` 返回任意旧任务而非最近。**测试盲区**：`test_status_idle` 只覆盖空态（`last_task:null`），非空 idle 态（多任务取最近）未覆盖 → 此分支 bug 未被测试暴露。可加 `@pytest.mark.xfail` 用例（断言 `last_task==最近`，当前必败，修后转 xpass）显式跟踪，本轮未加。
3. **②（低危）**：`start_task`（`auto_annotation.py:486/488`）`frame_interval = data.get("frame_interval_sec", 1)` 后 `if frame_interval < 1:` —— 客户端传字符串时 `str<int` 抛 `TypeError`→500（本意 400）。测试传数字 `0` 命中干净 400，未覆盖字符串 cast 崩。
4. **worker 端到端**：抽帧/模型分析/合并/生成 GT 的正确性未被覆盖（委托边界，旧生产代码职责）。可选真实冒烟（`AUTO_ANNO_SMOKE=1` env-var opt-in，mock 模型 client + 真 ffmpeg 跑小视频）仿 streaming `STREAM_SMOKE`，本轮不做。

## 11. 手动测试

服务起在 `http://127.0.0.1:8080`（`py run.py`）。手动测试打真实 `benchmark.db`，读型安全、写型改库、start/convert 起真 worker（需模型 API+ffmpeg+水印视频）。Windows 用 `curl.exe`（勿用 PS 的 `curl` 别名，4xx/5xx 会抛）。

```powershell
# 读型（安全）
curl.exe -s http://127.0.0.1:8080/api/v1/auto-annotation/status | py -m json.tool
curl.exe -s http://127.0.0.1:8080/api/v1/auto-annotation/videos-without-events | py -m json.tool
curl.exe -s http://127.0.0.1:8080/api/v1/auto-annotation/tasks | py -m json.tool
curl.exe -s "http://127.0.0.1:8080/api/v1/auto-annotation/tasks?page=1&page_size=5" | py -m json.tool

# 错误码（命中前置条件即返回，不起 worker）
curl.exe -s -w "`nHTTP %{http_code}`n" http://127.0.0.1:8080/api/v1/auto-annotation/tasks/999/json          # 404/21020
curl.exe -s -w "`nHTTP %{http_code}`n" -X POST http://127.0.0.1:8080/api/v1/auto-annotation/tasks:stop      # 409/31040
curl.exe -s -w "`nHTTP %{http_code}`n" -X POST http://127.0.0.1:8080/api/v1/auto-annotation/tasks/999:convert-to-events  # 404/21020

# 弃用 header（旧端点仍可用 + 标记）
curl.exe -s -i http://127.0.0.1:8080/auto-annotation/api/tasks | Select-String -Pattern "Deprecation|Link"
#   期望: Deprecation: true  +  Link: </api/v1/auto-annotation>; rel="successor-version"

# 写型（改真实库，谨慎；先备份 benchmark.db）
curl.exe -s -X POST http://127.0.0.1:8080/api/v1/auto-annotation/tasks/1:clear | py -m json.tool          # 200/{task_id}
curl.exe -s -w "`nHTTP %{http_code}`n" -X DELETE http://127.0.0.1:8080/api/v1/auto-annotation/tasks/1      # 204

# start/convert（起真 worker，需模型 API+ffmpeg+水印视频；前置不满足会 failed）
curl.exe -s -X POST http://127.0.0.1:8080/api/v1/auto-annotation/tasks -H "Content-Type: application/json" -d "{\"video_db_id\":1,\"frame_interval_sec\":2,\"merge_interval_sec\":5,\"event_types\":[\"fight\"]}" | py -m json.tool
curl.exe -s -X POST http://127.0.0.1:8080/api/v1/auto-annotation/tasks/1:convert-to-events | py -m json.tool
```

**期望信封**：成功 `{"code":0,"message":"ok","data":{...}}`，列表 `data:{items,total,page,page_size,has_next}`，错误 `{"code":XXXXX,"message":"..."}` 且 HTTP 状态码与 code 首位对应（1↔400、2↔404、3↔409）。

## 12. 状态

- **未 git commit**（按 CLAUDE.md 规则，等用户授权）。连同前几轮累积的 `app/api/` 整目录均未上库。
- 下一模块：**extract/tasks**（路线图：streaming ✅ → auto-annotation ✅ → extract → assistant → evaluation → review → docs/rest-api.md）。仍走 plan mode → 批准 → 实现。后续顺序见项目记忆 `rest-api-migration.md`。
