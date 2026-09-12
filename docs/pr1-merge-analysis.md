# PR #1 合并冲突：两边功能差异 + 取舍建议

> 分析对象：GitHub PR #1 `refactor/v1-align-origin` → `main`
> PR 状态：`mergeable=False, state=dirty`（GitHub 无法自动合并）
> 分析方式：5 个并行子 agent 只读对比 `git show origin/main:<path>` vs `git show pr-1:<path>`，不触碰工作区
> 日期：2026-08-29

## 1. 背景与现状

| 项 | 值 |
|---|---|
| 分叉点（merge-base） | `35a44b7` |
| main 端已前进到 | `673c02b`（docker 合并 od 子服务，实际带入 v1 测试 + old_api 快照 + od_api + 异常测试，121 文件） |
| PR 端 2 个 commit | `d865e92` REST API v1 + AI 助手扩展 → `42d5fbc` 对齐 origin/scheme3（自评 380/3 green） |
| 冲突文件 | 14 个（5 内容冲突 + 9 添加/添加冲突） |

**冲突根因**：PR 从 `35a44b7` 分叉后，main 也前进了，且 main 的 `673c02b` 自己也带进来一套 `app/api/v1/*` 和 `tests/test_api_v1_*` 实现+测试。两边对同名文件各有版本——这不是「PR 落后于 main」，而是**两套各自对齐过的 v1 实现撞在一起**。

## 2. 总体结论

**以 PR 为基底 + 6 处必须回退/取 main**。

PR 是更新的、对齐 final v1 的版本（全域 12 域 v1 注册、scheme3 错误码、wrap_old_view 兼容、tool_calls 安全剥离、引擎重写），但有几处 PR 的改动是**回归**或**丢失了 main 的关键守护**，必须保留 main 的内容。详见第 3 节红线点。

## 3. 红线点（必须回退 main / 谨慎，共 6 处）

| # | 文件 | 问题 | 必须动作 |
|---|---|---|---|
| 1 | `app/routes/evaluation.py` | PR 把 `avg_fp_per_hour` 从**各事件类型算术平均**改成 `total_fp/duration_hours`（合计/总时长） | **必须用 main 的宏平均公式**。违反 CLAUDE.md 不变量（「不是合计/总时长」），且与未改的 `compute_overall_avg_fp`（宏平均）造成 get_results ↔ 详细报告口径不一致 |
| 2 | `app/api/v1/alerts.py` 底层 | 冲突根因是底层 `list_dataset_images` 形状分叉：PR 退化为**裸 list**（无分页无筛选，是回归），main 是服务端分页 dict+筛选 | **保留 main 的服务端分页+筛选底层 view**，PR wrapper（兼容 list+dict）为底。`dataset_ids` 回传保留 |
| 3 | `tests/test_deprecation.py` | main 的 `TestBodyUntouched` 直接守护 wrap_old_view「旧端点 body 不被改写」的根本契约；PR 删了它，Link 也从精确匹配放宽成 `in` | **采用 main**（补回 alerts/verification 两端点 + body-untouched），fixture 迁到 `client` |
| 4 | scheme3 `error_code` 浭试断言 | main 显式断言 `DATASET_NOT_FOUND`/`UNKNOWN_FIELD`/`EVAL_SET_NOT_FOUND`/`VALIDATION_ERROR`/`DATASET_EXISTS` 等 error_code 字符串；PR 几乎只断言 HTTP `code` | 测试融合时**必须补回 main 的 error_code 断言**（这是 scheme3 落地的关键锁）。OCR 注释提 30340/10311 子码却未断言，若属契约应补 |
| 5 | `tests/conftest.py` | 两版 fixture 名字完全不重叠（main `app_client`/`app_ctx`，PR `app`/`client`），**两方向互不兼容**。PR 隔离更全（patch 6 模块双绑定 DATABASE_PATH+上传/配置路径，修了 main 后台线程写真实库的风险），但 PR **删了 od fixture** | 以 PR 的 `app`/`client` 为基底，**必须补回 main 的 `od_module`/`od_client`**（否则 `test_od_api`/`test_od_unit` 无 fixture）。统一命名是合并前提 |
| 6 | main 独有 `tests/snapshots/` + `tests/test_od_*.py` | 不在冲突文件里（PR 没碰），但 conftest 改动会波及 | 需单独决策归属：建议**保留**（main 的金牌快照 + od 测试是 PR 缺失的能力），conftest 补回 od fixture 后即可跑 |

## 4. 逐文件取舍总表

### 直接采用 PR（4 文件）

| 文件 | 冲突类型 | 理由 | 注意点 |
|---|---|---|---|
| `app/routes/streaming.py` (513) | 内容 | PR 单 ffmpeg concat+stream_loop 引擎重写，解决 reader 被踢 + Windows 误杀 + 编码不兼容三个痛点，main 无对应能力 | **须连 `app/database.py` 的 transcode 列迁移一起取**；`current_video_index`/`current_loop` 语义变占位 0/1，同步前端与测试。本质是引擎重写非 REST 对齐 |
| `app/api/v1/videos.py` (285) | 添加/添加 | PR 是 main 的严格超集：保留 main 的 6 委托端点 + 补 6 fork 端点（GET 单条/PATCH rename/GET watermarked JOIN/PUT DELETE eval-sets），scheme3 已落地 | merge 时直接取 PR 版，main 无独有内容丢失 |
| `app/api/__init__.py` (15) | 添加/添加 | PR 注册 12 域 v1（main 只 3 域 videos/alerts/alerts_ocr） | 合并后逐一确认 9 个新 v1 模块自身是否对齐 |
| `app/routes/assistant.py` (21) | 内容 | PR 剥离向前端泄漏的 tool_calls（安全收口），保留 LLM 上下文 | — |

另：`app/__init__.py` (2) 纯注释噪声，任选（建议取 main，注释点出「旧端点保留」语义更明确）。

### 融合（8 文件）

| 文件 | 冲突类型 | 融合方案 |
|---|---|---|
| `app/routes/evaluation.py` (119) | 内容 | 取 PR 的 worker 简化（拆 `_worker_body`、去 conn_ref 闭包/双层包装）、PDF try/finally 必清临时 HTML、creds 改名 `get_claude_creds`/`auth_token`（对齐 9b11028 AI 角色分组重构）；**但 avg_fp_per_hour 回退 main 宏平均**。注意 PR 复用可能损坏的 conn 写 failed、丢失 error_message（main 开新连接更稳）需斟酌。命中判定/召回率算术平均/`get_effective_status`/`confirmed_count==0` 四条不变量 diff 均未触及 |
| `app/api/v1/alerts.py` (23) | 添加/添加 | PR wrapper（兼容 list+dict 两形状）为底 + **保留 main 的服务端分页+筛选底层 view**。`dataset_ids` 回传保留。错误码两版均 scheme3 |
| `docs/rest-api-error-codes.md` (141) | 添加/添加 | **两版均为 scheme3，无旧码残留**（修正了「PR 才改 scheme3」的推断——main 本就是 scheme3，5 位码只在附录 A 作被拒方案）。141 行 diff 主体是删除（详尽枚举/工具表/分流表/附录被压缩）。采用 PR 精简骨架 + **保留 main 的「已用 error_code 枚举表」与 errorhandler 分流表**。函数名以最终保留的 responses.py/compat.py 实际签名为准 |
| `tests/conftest.py` (166) | 内容 | 以 PR 的 `app`/`client` 为基底（隔离更全），**补回 main 的 `od_module`/`od_client`**。统一命名（建议 `app`/`client`） |
| `tests/test_api_v1_alerts.py` (508) | 添加/添加 | PR fixture + 真实上传/下载/分页/弃用头路径 + **main 的 error_code 字符串断言 + 信封键契约 + `:import` + 白名单前置校验** |
| `tests/test_api_v1_alerts_ocr.py` (291) | 添加/添加 | 以 PR 为主（真跑 OCR + conflict 409 + cancel 语义 + 后台线程隔离）+ 补 main 错误路径（not-found/no-images 已重叠）+ **补 scheme3 子码（30340/10311）断言**（若属契约） |
| `tests/test_api_v1_errors.py` (106) | 添加/添加 | 以 main 为主（ApiError `error_code` 传播测试 + 413 旧格式边界 + v1 正常端点不被误伤）+ PR 的 405 回退补充。fixture 迁到 `client` |
| `tests/test_api_v1_videos.py` (189) | 添加/添加 | PR 的 CRUD 全链路（main 完全没测）+ main 的分页 clamp 容错 + `?q=` 过滤 + 委托缺失资源只验错误信封的稳健思路。信封契约两版一致 |

### 采用 main（1 文件）

| 文件 | 冲突类型 | 理由 |
|---|---|---|
| `tests/test_deprecation.py` (64) | 添加/添加 | `TestBodyUntouched` 守护 wrap_old_view 根本契约，PR 删它是退步；main 覆盖三蓝图旧端点 + 精确 Link 校验。fixture 迁到 `client` |

## 5. 推荐合并操作（rebase 方案骨架）

```bash
# 0. 保护当前工作区未提交改动（docs/CLAUDE.md 等）——用独立 worktree，不碰主工作区
git worktree add ../vae-pr1-rebase pr-1
cd ../vae-pr1-rebase

# 1. 基于 main 最新 rebase
git rebase origin/main

# 2. 逐 commit 解决冲突（按第 4 节取舍）：
#    - 直接取 PR: streaming.py / app/api/v1/videos.py / app/api/__init__.py / assistant.py
#    - 融合: evaluation.py(回退 avg_fp 宏平均) / alerts.py(main 分页底层) /
#            conftest.py(补 od fixture) / 各 test 文件(补 error_code 断言)
#    - 取 main: test_deprecation.py
#    解完每个: git add <files> && git rebase --continue

# 3. 跑测试验证（PR 自评 380/3 green，对齐后应保持）
python -m pytest -q

# 4. 交用户确认后 force-push（难逆，须用户明确同意）
git push --force-with-lease origin HEAD:refactor/v1-align-origin

# 5. 回 GitHub PR 页面，mergeable 应变 clean，点 Merge
# 6. 清理 worktree
cd - && git worktree remove ../vae-pr1-rebase
```

## 6. 待用户拍板的判断点

1. **scheme3 子码**（30340/10311 等）是否属契约、是否要在 OCR 测试里补断言？
2. **main 独有 `tests/snapshots/` 金牌快照 + `tests/test_od_*.py`** 是否保留？（建议保留）
3. **evaluation.py worker 异常处理**：PR 复用同一 conn 写 failed 更内聚但可能不稳（conn 已损坏），main 开新连接更稳但双层包装啰嗦——倾向哪个？
4. **错误码文档**详尽度：PR 精简骨架 vs main 详尽枚举表，保留多少？
