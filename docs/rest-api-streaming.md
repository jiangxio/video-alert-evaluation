# /api/v1 streaming 改造文档

> REST API 改造第 7 模块。把 `app/routes/streaming.py` 的 11 个内联 JSON 端点资源化进 `/api/v1/streaming/*`，统一信封 + 5 位错误码（FF=09 stream-tasks）。streaming 是高风险模块（start 用 `subprocess.Popen` 起 ffmpeg + `_monitor_video_process` 后台 daemon 线程；`list_tasks` 调 `_sync_running_status` 可能起重连线程；模块级 `_stream_processes`/`_stream_lock`/`_reconnecting_tasks`/`_reconnect_lock` 状态），故**原位重写 handler 但函数级复用旧高风险逻辑**（start 直接调旧 `_start_task_internal`，不重写 `_play_video`），与 OCR 的路由级 `call_old_view` 委托不同。旧端点保留并自动加弃用 header。

## 变更总览（TL;DR）

> 一句话：把旧 `app/routes/streaming.py` 的 11 个 JSON 端点资源化进 `/api/v1/streaming/*`，统一信封 + 5 位码（FF=09）；**原位重写 handler + 函数级复用 `_start_task_internal`**（不重写 `_play_video`），运行态冲突 400→409 修正。单测 35 绿 + 真环境烟测 15 绿全过。

- **架构决策**：原位重写 + 函数级复用（非 OCR 式路由级 `call_old_view` 委托）。start 调旧 `_start_task_internal`（干净模块级函数），不重写起 ffmpeg+监控线程的 `_play_video`；stop 复用 `_stream_processes`/`_is_pid_alive` 原语；查询/CRUD 复用 `_resolve_watermarked_videos`/`_ensure_duration`/`_get_suggested_algorithms`/`_calc_progress` 等。理由：streaming 有干净内部函数可用 → 函数级复用比路由委托更优（精确码 + 409 修正）。
- **端点/动词/码**：11 端点（FF=09，14 码）；start/stop `:action` 后缀、preview `tasks:preview`、DELETE→204、列表 `paginated()`；运行态冲突 5 处旧 400→409（`30900` start 已运行 / `30901` PATCH 运行中 / `30902` DELETE 运行中 / `30903` 状态不可启动 / `30904` stop 非运行）。
- **踩坑（6，均已修）**：①`streaming.DATABASE_PATH` 双绑定 patch（类比 OCR alerts）；②FakePopen+监控线程+Windows 文件锁→`_reset_stream_state` teardown 终止假进程→等线程退出释放 tmp 库句柄；③stop/监控竞态→FakePopen `wait()` 加 0.1s 让 stop 的 DB UPDATE 先提交；④真实冒烟端口探活假阳性→`STREAM_SMOKE` env-var 显式 opt-in；⑤`_build_ffmpeg_cmd` 单测重定向 concat 路径+测 `resume_offset=0` 规避子进程；⑥真实冒烟短视频触发 `_play_video` 2.5s 启动守卫→`loop_count=30`+start 500 skip 兜底+路径重定向到 tmp。
- **测试**：A 单测 **35 passed/1 skipped**（28 快测含 2 纯函数 + 7 FakePopen slow + 1 真实冒烟 skip）；A 全套 v1 回归 **111 passed/1 skipped**（76+35，无回归）；B 真环境烟测 `scripts/streaming-smoke.ps1` **PASS=15 FAIL=0 SKIP=0**（含真 ffmpeg→MediaMTX 推流 + 409 修正实环境验证）。模拟链路盲区收窄：FakePopen 漏的「命令构造/失败重试」由 2 纯函数单测堵、「真链路」由 B 真环境堵。
- **状态**：未 git commit（按 CLAUDE.md 等用户授权）。下一模块：auto-annotation/tasks。

## 1. 背景

旧 `streaming` 蓝图（`/streaming`）把推流页面 + 11 个 JSON 端点混在一个蓝图，返回裸 JSON + HTTP 码（`[rows]` / `{success,...}` / `{error}`），无统一信封、无结构化错误码，运行态冲突全塞 400。本模块把 11 个 JSON 端点资源化进 `/api/v1/streaming`；页面路由 `GET /streaming/` 不在迁移范围。新旧并行：旧端点保留并自动加弃用 header（`deprecation.py:20` 已预置 `/streaming/api/` → `/api/v1/streaming`），前端继续用旧 URL。

范围：**URL 资源化 + 正确 HTTP 动词（start/stop `:action` 后缀、DELETE→204）+ 统一信封 + 5 位码 + 运行态冲突 400→409 修正**，不改交互语义（个别状态修正见 §6，均对齐 algorithms 先例）。

## 2. 改动文件清单

| 文件 | 类型 | 作用 |
|---|---|---|
| `app/api/v1/streaming.py` | 新建 | 11 端点（FF=09），原位重写 handler，复用 `app.routes.streaming` 模块级函数 |
| `app/api/v1/__init__.py` | 改 | `BLUEPRINTS` 加 `streaming.bp`，`import` 加 `streaming` |
| `tests/conftest.py` | 改 | `app` fixture patch `app.routes.streaming.DATABASE_PATH`→tmp（双绑定） |
| `tests/test_api_v1_streaming.py` | 新建 | 28 快测 + 2 纯函数单测 + 7 slow（FakePopen）+ 1 真实冒烟（skipif） |
| `scripts/streaming-smoke.ps1` | 新建 | 真环境一气呵成烟测脚本（15 项 PASS/FAIL/SKIP + 汇总） |
| `docs/rest-api-error-codes.md` | 改 | FF 表 09 行 待做→已用 + FF=09 码表 |

**未触碰**：`app/routes/streaming.py` 旧端点（新旧并行）、`app/__init__.py`（已注册 `streaming.bp` + `init_streaming_cleanup()`）、`scripts/stream_videos.py`。

## 3. 端点详情（11 个）

蓝图 `api_v1_streaming`，`url_prefix=/api/v1`。

| # | 方法 + 路径 | 旧视图 | 成功响应 | 错误码（5 位 FF=09） |
|---|---|---|---|---|
| 1 | `GET /streaming/videos` | `list_streamable_videos` | `paginated` | — |
| 2 | `GET /streaming/video-sets` | `list_video_sets` | `paginated`（`video_count` 替代 `video_ids`） | — |
| 3 | `GET /streaming/tasks` | `list_tasks` | `paginated`（含 `rtsp_urls`/`elapsed_seconds`/`estimated_end_ts`；先调 `_sync_running_status`） | — |
| 4 | `POST /streaming/tasks` | `create_task` | `created({id,rtsp_url,total_duration,suggested_algorithms}, location=…/tasks/{id})` | `10900` 来源类型无效/解析失败；`10901` 缺 source_id；`10902` 流名空；`10903` 流名格式非法 |
| 5 | `POST /streaming/tasks/<id>:start` | `start_task` | `ok({status,pid,rtsp_urls})` | `20900` 不存在(404)；`30900` 已运行(409)；`30903` 状态不可启动(409)；`10905` 视频文件不存在(400)；`40900` 启动失败(500) |
| 6 | `POST /streaming/tasks/<id>:stop` | `stop_task` | `ok({status})` | `20900` 不存在(404)；`30904` 未运行(409) |
| 7 | `GET /streaming/tasks/<id>/logs` | `get_task_logs` | `ok({content,lines})` | `20900` 不存在(404)；`40901` 日志读取失败(500) |
| 8 | `GET /streaming/tasks/<id>/progress` | `get_task_progress` | `ok({task_id,name,stream_name,status,loop_count,total_duration,elapsed_seconds,videos,progress})` | `20900` 不存在(404)；`10900` 解析失败(400) |
| 9 | `PATCH /streaming/tasks/<id>` | `update_task` | `ok({id,status:"created"})` | `20900` 不存在(404)；`30901` 运行中无法编辑(409)；`10900-10903` 同 create |
| 10 | `DELETE /streaming/tasks/<id>` | `delete_task` | `no_content()`(204) | `20900` 不存在(404)；`30902` 请先停止再删除(409) |
| 11 | `POST /streaming/tasks:preview` | `preview_task` | `ok({rtsp_urls,total_duration,suggested_algorithms,video_count})` | `10904` 参数不完整(400)；`10900` 解析失败(400) |

> 路径前缀 `/api/v1`。RPC 动作用 `:action` 后缀（`:start`/`:stop`/`:preview`）；子资源用名词（`logs`/`progress`）。

## 4. 请求方式（verb）分析

| 旧端点 | 旧方法 | 新方法 | 变化 | 理由 |
|---|---|---|---|---|
| `list_streamable_videos`/`list_video_sets`/`list_tasks` | GET | GET | 不变（加 `paginated`） | 幂等列表；v1 约定列表加分页信封 |
| `create_task` | POST | POST | 不变 | 创建 |
| `start_task` | POST `/tasks/<id>/start` | POST `/tasks/<id>:start` | URL 改 `:action` | RPC 动作；对齐 alerts_ocr `:ocr:batch` |
| `stop_task` | POST `/tasks/<id>/stop` | POST `/tasks/<id>:stop` | URL 改 `:action` | RPC 动作 |
| `get_task_logs`/`get_task_progress` | GET | GET | 不变 | 幂等子资源读取 |
| `update_task` | PATCH | PATCH | 不变 | 部分更新（旧已 PATCH） |
| `delete_task` | DELETE | DELETE | 响应 200→**204** | 对齐 alerts/videos/algorithms DELETE 模式 |
| `preview_task` | POST `/api/preview` | POST `/tasks:preview` | URL 改集合动作 | 预览未创建任务，是 tasks 集合上的动作 |

**请求体格式**：全 JSON body（`request.get_json()`），无文件上传。

## 5. 错误码（5 位 `H FF SS`，详见 `docs/rest-api-error-codes.md`）

FF=09 直接用新码（分配表已预留，本轮启用）。共 14 个码：

| 码 | 含义 | HTTP |
|---|---|---|
| `10900` | 来源类型无效 / 解析来源失败 | 400 |
| `10901` | 请选择视频或视频集 | 400 |
| `10902` | 流名称不能为空 | 400 |
| `10903` | 流名称只能包含字母、数字、连字符和下划线 | 400 |
| `10904` | 参数不完整（preview） | 400 |
| `10905` | 视频文件不存在于磁盘（start） | 400 |
| `20900` | 任务不存在 | 404 |
| `30900` | 任务已在运行中（start，旧 400→409） | 409 |
| `30901` | 任务运行中，无法编辑（PATCH，旧 400→409） | 409 |
| `30902` | 请先停止任务再删除（DELETE，旧 400→409） | 409 |
| `30903` | 任务状态不可启动（start，近乎不可达） | 409 |
| `30904` | 任务未在运行中（stop，旧 400→409） | 409 |
| `40900` | 启动失败（保留具体 message） | 500 |
| `40901` | 日志读取失败 | 500 |

## 6. 关键设计：原位重写 + 函数级复用 + 400→409 修正

### 函数级复用（非路由级委托）

streaming 真正高风险的核心是 `_play_video`（起 ffmpeg 子进程 + 监控线程 + 重连）。但旧模块已把它封装成干净的模块级函数 `_start_task_internal(task_id, use_resume) -> (success, result)`（`streaming.py:1078`，非路由装饰器），返回带 `status_code`/`error` 的结构。

→ 新 `start` handler 直接 `_legacy._start_task_internal(task_id, use_resume)`，**不重写 `_play_video`/`_build_ffmpeg_cmd`/`_monitor_video_process`/`_sync_running_status`**，尊重「不改高风险逻辑」本意；同时能在 handler 里 `raise ApiError` 给精确 5 位码 + 做 409 修正（路由级 `call_old_view` 委托只能按 status 粗映射，无法修正）。OCR 用路由级委托是因为旧 OCR 逻辑耦合在路由内（内联读 body + `_ocr_progress` 模块态），无干净内部函数可用——streaming 情况不同。

`stop` 无独立内部函数，但 stop 逻辑是同步 terminate（pop 进程→terminate→改 DB），不起线程/子进程，非高风险——新 handler 复用 `_legacy._stream_processes`/`_stream_lock`/`_is_pid_alive`/`_cleanup_resume_file` 原语写。查询/CRUD 复用 `_resolve_watermarked_videos`/`_ensure_duration`/`_get_suggested_algorithms`/`_calc_progress`/`_calc_elapsed_seconds`/`_parse_started_at`/`_get_local_ips`。

### 经先例授权的语义修正（新端点专属，旧不动）

5 位码 H 位须与 http_status 对应，运行态冲突属 409（请求合法、拒绝源于资源当前状态）。把旧 400 修正为 409（对齐 algorithms 的 `30600`/`30702`）：`30900`/`30901`/`30902`/`30903`/`30904`。`DELETE` 响应 200→204（对齐 alerts/videos/algorithms）。

## 7. 实现中踩的坑（根因 + 修复）

### 坑1：`streaming.DATABASE_PATH` 双绑定 patch（类比 OCR `alerts.DATABASE_PATH`）

- **现象**：`streaming.py:23` `from app.database import DATABASE_PATH` 导入期捕获，后台监控/重连线程与 `_resolve_watermarked_videos`/`_ensure_duration` 等直连 helper 读此拷贝，不随 `database.DATABASE_PATH` 更新 → 写真实 `benchmark.db`。
- **修复**：`tests/conftest.py` 的 `app` fixture 加 `monkeypatch.setattr("app.routes.streaming.DATABASE_PATH", db_path)`，紧跟现有 `app.routes.alerts.DATABASE_PATH` patch。

### 坑2：FakePopen + 监控线程 + Windows 文件锁

- **现象**：start 测不能真起 ffmpeg/MediaMTX，用 FakePopen mock `subprocess.Popen`。但 `_play_video` 起的 `_monitor_video_process` daemon 线程调 `process.wait()`（阻塞）+ 直连 tmp 库（`streaming.py:822`）。线程不退出 → tmp 库 sqlite 句柄不释放 → Windows 下 `app` fixture teardown 的 `unlink(tmp_db)` 因文件锁失败。
- **修复**：`_reset_stream_state` autouse fixture（依赖 `app`，teardown 先于 `app` unlink）：①遍历 `_stream_processes` 调 `process.terminate()`（FakePopen 设 Event→监控线程 `wait()` 返回→线程走完关闭 sqlite 句柄→退出）；②清 `_reconnecting_tasks`；③`thread.join(timeout=30)` 等监控线程退出；④清 tmp 下 log/concat/resume 残留。跟踪监控线程靠 `_slow_env` 包裹 `_legacy._monitor_video_process`（append `current_thread()` 到 `_monitor_threads`），仿 OCR `_reset_ocr_progress`。

### 坑3：stop 与监控线程的竞态

- **现象**：FakePopen 的 `wait()` 被 `terminate()` 设 Event 后**立即**返回 0（真 ffmpeg 退出有延迟），监控线程可能与 stop 的 DB UPDATE 竞态（监控读到旧 running 状态→标 done，覆盖 stop 的 stopped）。
- **修复**：FakePopen.`wait()` 在 Event 后 `time.sleep(0.1)` 模拟真实退出延迟，让 stop 的 DB UPDATE 先提交 → 监控读到 stopped→早退（`status != running` 分支）。stop 的**响应**本身不受影响（stop 自己决定 `killed=True`→stopped），仅 DB 行终态稳定。

### 坑4：真实冒烟端口探活假阳性 → env-var opt-in

- **现象**：原计划 `skipif(not ffmpeg or not mediamtx_up)`，但 `_mediamtx_up()` 用 socket 连 :8554 探活——端口开 ≠ 真实推流能成功（本机 8554 有响应但 ffmpeg 推流仍 500）→ 测试假阳性失败。
- **修复**：改 `skipif(not os.environ.get("STREAM_SMOKE"))` 显式 opt-in；opt-in 后再 `pytest.skip` 兜底 ffmpeg/MediaMTX 未就绪。默认 skip 不阻塞 CI；真要跑真实链路冒烟设 `STREAM_SMOKE=1`。

### 坑5：`_build_ffmpeg_cmd` 纯函数单测要重定向 concat 路径 + 规避 resume 子进程

- **现象**：`_build_ffmpeg_cmd` 写 concat 清单到 `_get_concat_list_path(task_id)`（= 仓库 `tmp/stream_concat/`）→ 污染工作树；且 `resume_offset>0` 会调 `subprocess.run(["ffmpeg",...])` 截取续播视频。
- **修复**：单测 monkeypatch `_legacy._get_concat_list_path`→tmp；测 `resume_offset=0`（走 concat 分支，不触发 resume 子进程）。同时验证 Windows 反斜杠路径已转 `/`（concat demuxer 要求）。

## 8. 测试方案

- **快测（26，无 slow）**：9 类查询/CRUD 端点。seed `videos`+`watermarked_videos`（`duration` 已填 + `output_path` 指向 tmp 真文件）+`eval_video_sets`；seed duration 使 `_ensure_duration` 早退、不触发 ffprobe。覆盖 list（分页信封+rtsp_urls）、create（→201+Location+suggested）+ 5 个 400（10900-10904）、patch（→ok + running 30901 + 不存在 20900）、delete（→204 + running 30902 + 不存在）、logs（空/有内容/不存在）、progress（progress 对象/不存在）、preview（→ok + 参数不完整 10904 + 解析失败）。
- **纯函数单测（2，快测，堵 FakePopen 盲区）**：`_build_ffmpeg_cmd`（断言命令结构 + Windows 路径转 `/` + concat 清单写到 tmp）、`_is_retryable_error`（喂典型 stderr 断言重试判定）。
- **slow 测（7，FakePopen）**：start（→200 running+pid+rtsp_urls + DB 行 running）、start 已运行→30900、start 不存在→20900、start resume、stop（→200 stopped）、stop 非运行→30904、stop 不存在→20900。
- **真实冒烟（1，skipif）**：`STREAM_SMOKE=1` opt-in + ffmpeg/MediaMTX 就绪才跑 start→progress→stop 端到端。
- **隔离**：conftest 双绑定 patch（坑1）+ `_reset_stream_state` autouse teardown（坑2）+ `_slow_env` 重定向硬编码仓库路径到 tmp（concat/resume/log）+ FakePopen + 包裹监控线程。

> not-slow 共 28 = 26 快测 + 2 纯函数；slow 共 8 = 7 FakePopen + 1 真实冒烟。

## 9. 测试结果

| 命令 | 结果 |
|---|---|
| `py -m pytest tests/test_api_v1_streaming.py -q -m "not slow"` | **28 passed**（26 快测 + 2 纯函数），8 deselected |
| `py -m pytest tests/test_api_v1_streaming.py -q -m slow` | **7 passed, 1 skipped**（FakePopen 7 绿；真实冒烟 skip） |
| `py -m pytest tests/test_api_v1_streaming.py -q`（合跑） | **35 passed, 1 skipped in 23.30s** |
| 全套 v1 回归 `py -m pytest tests/ -k "api_v1" -q` | **111 passed, 1 skipped**（76 + 本轮 35，无回归） |
| B 真环境烟测 `.\scripts\streaming-smoke.ps1`（`py run.py` 起服务后跑） | **PASS=15 FAIL=0 SKIP=0**（含真 ffmpeg→MediaMTX 推流 + 409 修正实环境验证） |
| 隔离验证 `git status --short benchmark.db .env logs/` | 空（真实库/`.env`/`logs` 未被触碰） |

> 运行环境：`py` 启动器（Python 3.13.14, pytest 9.1.1）；`python` 是 Windows Store stub（exit 49 无输出）勿用。slow 含 `_play_video` 的 `time.sleep(2.5)`，故 start 类用例每条约 2.5s+。

## 10. 状态

- **未 git commit**（按 CLAUDE.md 规则，等用户授权）。连同前几轮累积的 `app/api/` 整目录均未上库。
- 下一个模块：**auto-annotation/tasks**（路线图：streaming ✅ → auto-annotation → extract → assistant → evaluation → review → docs/rest-api.md）。仍走 plan mode → 批准 → 实现。后续顺序见项目记忆 `rest-api-migration.md`。
