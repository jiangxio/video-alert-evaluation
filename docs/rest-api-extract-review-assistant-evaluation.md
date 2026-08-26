# /api/v1 extract / review / assistant / evaluation 改造文档

> REST API 改造第 9–12 模块（2026-08-21 一轮完成）。把 `app/routes/{extract,review,assistant,evaluation}.py` 共 54 个 JSON 端点资源化进 `/api/v1/*`，统一信封 + 5 位错误码（FF=11/14/12/13）。本轮共 +121 测试，全套 v1 由 146 → **267 passed, 1 skipped**。复用新建的 `app/api/v1/_helpers.py`（`parse_pagination`/`paginate`/`raise_msg`）。旧端点保留并自动加弃用 header。

## 变更总览（TL;DR）

| 模块 | FF | 端点 | 委托 | 重写 | 测试 | 核心 |
|---|---|---|---|---|---|---|
| extract | 11 | 5 | 2 | 3 | 16 | worker 被 assistant_tools 共享；conftest 补 DATABASE_PATH+EXTRACTED_FRAMES_DIR |
| review | 14 | 4 | 2 | 2 | 15 | 独立蓝图，**修 deprecation successor**；只写 ai_suggestion 不碰指标 |
| assistant | 12 | 9 | 5 | 4 | 17 | chat/confirm/cancel 保 **200+type 契约**不映射 4xx |
| evaluation | 13 | 36 | 13 | 23 | 73 | **核心指标区只调不改**；get_results 委托避转录 |

- **架构**：高风险异步（线程/锁/模块态/指标计算/LLM）→ 委托 `call_old_view`（不改不重测，只验信封/状态码/错误码/委托真触发）；纯查询/CRUD → 原位重写复用 `get_db`。
- **真分页**：列表端点全用 SQL `LIMIT/OFFSET + COUNT(*)`（`_helpers.paginate`），不 fetchall 切片（纠正 streaming 旧法）。
- **语义修正**：PUT→PATCH×5、DELETE→204×4、create/clone→201、冲突 400→409、不存在 400→404，均对齐既有先例、不改交互语义。
- **conftest**：补 4 处 DATABASE_PATH 双绑定 patch（extract/review/evaluation + 已有 auto_annotation）+ extract EXTRACTED_FRAMES_DIR。
- **未 commit**（按 CLAUDE.md 等用户授权）。

---

## 1. extract（FF=11）— 5 端点

蓝图 `api_v1_extract`，`url_prefix=/api/v1`。

| # | 方法+路径 | 旧视图 | 策略 | 成功 | 错误码 |
|---|---|---|---|---|---|
| 1 | `POST /extract/tasks` | `start_extract` | 委托 | `ok({task_id,video_count})` | `11100/11101/11102/21100` |
| 2 | `GET /extract/tasks/<id>/status` | `extract_status` | 委托 | `ok({status,done,total,frame_count,video_count,output_dir,error})` | `21101` |
| 3 | `GET /extract/tasks/<id>/download` | `download_frames` | 重写(二进制 zip) | `send_file` | `21101/21102` |
| 4 | `DELETE /extract/tasks/<id>` | `delete_extract` | 重写→204 | `no_content` | `21101` |
| 5 | `GET /extract/tasks` | `list_tasks` | 重写·真分页 | `paginated` | — |

**关键**：worker `_do_extract_batch` 是干净模块级函数（可 stub），但 `app.services.assistant_tools:1221` `from app.routes.extract import _do_extract_batch, _extract_tasks, _extract_lock` **共享**它 → 不能改名/重构。后台线程用 `sqlite3.connect(str(DATABASE_PATH))` 直连（不走 get_db，无 app_context 问题）。DELETE→204、start 保 200。

**代码错漏**：`interval_sec = float(data.get('interval_sec') or 1.0)`——0 是 falsy 被 `or 1.0` 当默认，**须传负数**才命中「抽帧间隔必须大于0」(11101)。

**盲区**（bug-audit 另修）：`_fail_task` 死代码（:319 从无调用）+ `_do_extract_batch` 单帧失败 `except:pass` 后无条件标 status='done'（失败不可见）---> 原因：将状态写死为done；`float(interval_sec)` 传字符串崩 500。

---

## 2. review（FF=14）— 4 端点

蓝图 `api_v1_review`，`url_prefix=/api/v1`。

| # | 方法+路径 | 旧视图 | 策略 | 成功 | 错误码 |
|---|---|---|---|---|---|
| 1 | `GET /review/tasks/<tid>/alerts` | `get_alerts` | 重写 | `ok({alerts,count})` | `21400` |
| 2 | `GET /review/tasks/<tid>/gt-context` | `gt_context` | 重写 | `ok({gt_events,alerts})` | `11401` |
| 3 | `POST /review/tasks/<tid>/ai-check` | `ai_check` | 委托 | `ok({batch_id,total})` | `11400/11402/21400/21401` |
| 4 | `GET /review/tasks/<tid>/ai-check/status` | `ai_check_status` | 委托 | `ok({status,total,done,current_id,results,error})` | `11403/21402` |

**关键**：review 是**独立蓝图**（`/review`，非 evaluation 子部分）→ 独立 FF=14 + 独立前缀 `/api/v1/review`。**必做修正**：`deprecation.py:24` successor 从 `/api/v1/evaluation` 改为 `/api/v1/review`。review 只写 `ai_suggestion`（LLM 建议文本），**不碰 `manual_status`/`is_false_positive`/指标**——后者写端点在 `evaluation.py:912/938` 归 FF=13。读端点复用纯读 `eval_service.get_effective_status`。review 是全仓**唯一自带 LLM `timeout=120`** 的调用点（:333），不受 #20 影响。`ai-check/status` 保 query param `batch_id`（委托兼容，不强行入路径）。ai-check 成功保 200、无 DELETE。

**无 bug-audit 项**：review 无专属审计条目，不触及 #8/#9/#19/#20。

---

## 3. assistant（FF=12）— 9 端点

蓝图 `api_v1_assistant`，`url_prefix=/api/v1`。

| # | 方法+路径 | 旧视图 | 策略 | 成功 | 错误码 |
|---|---|---|---|---|---|
| 1 | `GET /assistant/settings` | `api_get_settings` | 重写 | `ok({settings})` | — |
| 2 | `POST /assistant/settings` | `api_update_settings` | 重写 | `ok({settings})` | — |
| 3 | `POST /assistant/chat` | `api_chat` | 委托 | `ok(body)` | `11200` |
| 4 | `POST /assistant/pending-confirmations:confirm` | `api_confirm` | 委托 | `ok(body)` | `11201` |
| 5 | `POST /assistant/pending-confirmations:cancel` | `api_cancel` | 委托 | `ok(body)` | `11202` |
| 6 | `POST /assistant/sessions:clear` | `api_clear` | 委托 | `ok({message})` | — |
| 7 | `GET /assistant/sessions/history` | `api_history` | 委托 | `ok({messages})` | — |
| 8 | `GET /assistant/tasks` | `api_list_tasks` | 重写·真分页 | `paginated`（params JSON 解析） | — |
| 9 | `GET /assistant/tasks/<id>` | `api_task_status` | 重写 | `ok({task,progress})` | `21200` |

**核心约束**：chat/confirm/cancel 的 `200 + {type:'error', error_code:...}`（NOT_CONFIGURED/EXECUTION_FAILED/CONFIRMATION_EXPIRED）是**前端契约**，v1 **保 200 套 `ok(body)`，绝不映射成 4xx/5xx**——仅「消息不能空」「缺 confirmation_id」这类纯参数缺失映射 400。无 DELETE、无 400→409（HTTP 语义已干净）。clear/history 改委托（避免复制 session/role 过滤逻辑引入偏差）。worker 只在 `/confirm` 路径经 `execute_write_tool` 触发（stub 它即避开真 worker + #4 app_context）。**无需 conftest 双绑定 patch**（全走 get_db 或函数级 import）。

**审计状态**：#2（缺 import json）**已修**、#4 batch_ocr/watermark（app_context）**已修**（`_run_in_app_context` 辅助）、#20 max_tokens 已设 2048（timeout 仍缺）、低危 list_alerts rowcount -1 仍存（唯一影响：AI 助手调这个只读工具去"列告警"时，拿到 count=-1，可能跟用户说"共 -1）。

---

## 4. evaluation（FF=13）— 36 端点（6 页面 SKIP）

蓝图 `api_v1_evaluation`，`url_prefix=/api/v1`。最重最险——核心指标计算区。

### 4.1 委托（13 端点，指标邻接/状态变更/报告/LLM/sync）

| 端点 | 旧视图 | 委托理由 | 关键错误码 |
|---|---|---|---|
| `POST /tasks/<id>:execute` | `execute_task` | 起 worker 闭包（内联命中判定三条件±5s + `compute_task_metrics`），**绝不抽改** | `21300/31300/11312` |
| `POST /tasks/<id>:finalize` | `finalize_task` | 调 `compute_task_metrics` 算指标+锁定 | `21300/31301` |
| `POST /tasks/<id>:confirm` | `confirm_merged` | 写 GT 指标输入（10 列 INSERT）+跨表校验 | `21300/11313` |
| `POST /tasks/<id>:unconfirm` | `unconfirm_task` | metric lifecycle 状态转换（清指标） | `21300/31302` |
| `GET /tasks/<id>/results` | `get_results` | **D3 委托**：含内联 realtime 指标公式（:792-843），转录风险高 | `21300` |
| `GET /tasks/<id>/event-metrics` | `get_event_metrics` | 调 `compute_task_metrics`（指标邻接） | `21300` |
| `GET /tasks/<id>/report/image` | `get_report_image` | 二进制 + `compute_overall_avg_fp` | `21300` |
| `POST /tasks/<id>/report` | `detailed_report` | 二进制 HTML（`_delegate_binary` 直传） | `21300/31303/41301` |
| `POST /tasks/<id>/report/pdf` | `detailed_report_pdf` | Playwright PDF（二进制直传） | `21300/31303/41301/41302` |
| `POST /tasks/<id>/report:preview` | `detailed_report_preview` | 调 `_call_claude`（LLM） | `21300/11310` |
| `POST /tasks/<id>/report:chat` | `detailed_report_chat` | 调 `_call_claude_chat`（LLM） | `21300/11310` |
| `POST /gt:sync` | `sync_ground_truth` | 写 events 表 GT 指标输入 | `11306-11308/21309/21310/41303` |
| `GET /tasks/<id>/status` | `eval_status` | 读模块态 `_eval_progress` | `21311` |

> 二进制端点（report-image/report/report-pdf）用 `_delegate_binary`：旧视图成功返 Response（send_file/HTML）直传不走信封，失败返 `(jsonify,code)` 走 `raise_msg`。

### 4.2 原位重写（23 端点，查询/CRUD）

- **任务**：`GET /tasks`（真分页+名称富化+algo_versions）、`GET /tasks/<id>`、`POST /tasks`→201、`POST /tasks/<id>:clone`→201、`PATCH /tasks/<id>`（PUT→PATCH）、`DELETE /tasks/<id>`→204、`POST /tasks/<id>:analyze`（复用 `analyze_merged_events` 纯函数）、`GET /tasks/<id>/check-updates`。
- **人工状态/GT 计数**：`PATCH /tasks/<id>/merged-events/<mid>/status`（PUT→PATCH，写 manual_status 仅 UPDATE 下游 finalize 重算）、`PATCH /tasks/<id>/merged-events:batch-status`（PUT→PATCH）、`PATCH /tasks/<id>/gt-events/<gid>`（PUT→PATCH，写 confirmed/actual_count）。
- **测前分析**：`GET /pre-analysis`、`GET /pre-analysis/<id>`、`GET /pre-analysis:by-set/<id>`（真分页）、`POST /pre-analysis`→201（复用 `_legacy._run_pre_analysis`）、`DELETE /pre-analysis/<id>`→204。
- **评测集**：`GET /eval-sets`（含 video_count+gt_frame_count）、`GET /eval-sets:with-analysis-count`（D2 用 FF=13 共用，不启 FF=04）。
- **Chat 会话**：`GET /tasks/<id>/chat-sessions`（真分页）、`POST /tasks/<id>/chat-sessions`（upsert，create→201/update→200）、`GET /tasks/<id>/chat-sessions/<sid>`、`DELETE /tasks/<id>/chat-sessions/<sid>`→204。
- **GT 帧**：`GET /gt-frames/<id>/file`（二进制，?w=&h= 缩略图）。

### 4.3 最高原则：指标算法只调不改

命中判定三条件±5s（:641-668）、`is_fp` 纯时间重叠、`gt_hit_counts` 只统计不影响命中、`actual_count=gt_hit_counts`、`compute_task_metrics`（召回 `min(actual,confirmed)` 封顶、`confirmed==0→{gt=1,hit=1}`、整体召回算术平均）、`compute_overall_avg_fp`、`get_effective_status`——**函数体一行不碰**，委托端点内部被调，重写端点即便调也是「调不改」。CRUD 写 `manual_status`/`confirmed_count`/`actual_count` 仅存用户决策（UPDATE），下游 finalize 重算，不在重写里算任何指标。

### 4.4 测试隔离

- `_fake_thread`：patch `app.routes.evaluation.threading.Thread` 不跑 execute 的 worker 闭包（worker 是闭包无法 stub target）。
- `_stub_metrics`：`compute_task_metrics`→canned（避真指标计算，#8 范畴）+ `compute_overall_avg_fp`→canned。
- `_stub_claude`：`get_claude_creds`→fake + `_call_claude`/`_call_claude_chat`→canned（避真 LLM，#20 无 timeout 盲区）。注：detailed-report:preview/chat 在**函数内** `from app.services import api_config_service`，故 patch 真模块的 `get_claude_creds`（函数内 import 读 patched 模块属性），**不是** patch `app.routes.evaluation.api_config_service`（该名不存在）。
- conftest 补 `evaluation.DATABASE_PATH`（双绑定，execute worker :574 直连）。

### 4.5 旧行为忠实保留（非新 bug）

- `eval_tasks` **无 `updated_at` 列**（ALTER 未加）→ `check-updates` 的 `SELECT updated_at` 永抛 OperationalError→except→**200 `has_updates:false`，404 不可达**（旧既有，bug-audit 低危 evaluated_at 恒 null 范畴）。
- report 端点 POST 须带 `json={}` 否则 Flask `request.get_json()` 抛 **415**（旧视图同款；v1 委托故继承）。
- execute_task 的 UPDATE updated_at 有 try/except 兼容兜底（列不存在时 fallback 不带 updated_at）。

### 4.6 盲区（委托继承、bug-audit 另修）

#8 前端 sum/len（后端已修）、#18 service TypeError（路由级已 guard）、#19 报告图字体 Linux 路径（Windows 回退 load_default）、#20 LLM 无 timeout、低危 evaluated_at 恒 null。委托端点不断言这些。

---

## 5. 跨模块改动

### 5.1 共享 helper `app/api/v1/_helpers.py`

`parse_pagination()`（?page/&page_size=，1..100，默认 20）、`paginate(db,base_sql,order_sql,params,page,page_size,mapper)`（SQL `LIMIT/OFFSET + COUNT(*) FROM (base) _c` 真分页）、`raise_msg(body,msg_to_code,fallback)`（旧视图非 200 按 error 文案子串映射多码）。extract/review/assistant/evaluation 复用。auto_annotation/streaming 各有本地副本暂不回改（已测，不动）。

### 5.2 conftest `app` fixture 新增 patch

| patch | 模块 | 原因 |
|---|---|---|
| `app.routes.extract.DATABASE_PATH` | extract | 双绑定，worker `_do_extract_batch`/`_fail_task` 直连 |
| `Config.EXTRACTED_FRAMES_DIR` | extract | start 写 output_dir + worker 抽帧到此（真实仓库目录） |
| `app.routes.review.DATABASE_PATH` | review | 双绑定，worker `_ai_check_worker` 直连 |
| `app.routes.evaluation.DATABASE_PATH` | evaluation | 双绑定，execute worker 闭包直连 |

> 双绑定根因：`from app.database import DATABASE_PATH` 导入期捕获副本，不随 `database.DATABASE_PATH`（conftest patch 的原件）更新；后台线程用此副本 `sqlite3.connect(str(DATABASE_PATH))` 直连（不走 get_db），须显式重绑到 tmp。注：这些线程全用 `sqlite3.connect` 直连、**不走 get_db()，故无 assistant_tools #4 那种 app_context 问题**（#4 是线程内调 get_db 缺 app_context，已由 `_run_in_app_context` 修好 batch_ocr/watermark）。

### 5.3 deprecation 修正

`deprecation.py:24` `/review/api/` successor 从 `/api/v1/evaluation`（写错）改为 `/api/v1/review`（review 是独立蓝图）。

### 5.4 bug-audit 记忆更新（已落 `bug-audit-2026-08-13.md`）

本轮调研确认 5 条此后已自行修复：#2（assistant import json）、#9（detailed_report_pdf 路由）、#17（execute worker try/except）、#8 后端（compute_overall_avg_fp 改 sum）、#4 batch_ocr/watermark（`_run_in_app_context`）。仍存：#18/#19/#20/#21/#457 + 本轮新发现 #27（auto_annotation stop_task 缺 global）+ extract 同族隐患。

---

## 6. 测试结果

| 命令 | 结果 |
|---|---|
| `py -m pytest tests/test_api_v1_extract.py -q` | **16 passed** |
| `py -m pytest tests/test_api_v1_review.py -q` | **15 passed** |
| `py -m pytest tests/test_api_v1_assistant.py -q` | **17 passed** |
| `py -m pytest tests/test_api_v1_evaluation.py -q` | **73 passed** |
| 全套 v1 回归 `py -m pytest tests/ -k "api_v1" -q` | **267 passed, 1 skipped**（146 + 121，无回归；1 skip 是 streaming 真实冒烟 env-var opt-in） |

> 运行环境：`py` 启动器（Python 3.13.14, pytest 9.1.1）。委托测试 stub worker/Thread/metrics/LLM，避开真 ffmpeg/模型 API/指标计算/Playwright。

## 7. 状态

- **未 git commit**（按 CLAUDE.md 等用户授权）。连同前几轮 `app/api/` 整目录均未上库。
- REST 改造全部资源模块（videos/alerts/OCR/algorithms/event_types/config/streaming/auto-annotation/extract/review/assistant/evaluation）已就绪。
- 仅剩收口文档 **docs/rest-api.md**（全套 v1 汇总 API 文档）。
- 可选后续：错误码迁移（videos/alerts 旧 4 位→5 位 + 测试断言，单独一轮）；streaming 列表端点迁移到 `_helpers.paginate` 真分页（去 `_slice_page`）；streaming/auto_annotation 回改用共享 `_helpers`（去本地副本）。
