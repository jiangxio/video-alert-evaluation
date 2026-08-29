# 文档完整性核验与完善 — 交接文档

> **执行状态（2026-08-26）**：四篇文档已由本会话按下方方法核验完成，CLAUDE.md 过时事实已同步修正。第 5 节列出的可疑点均已处理。详见会话末的核验摘要；下方原文保留作方法论参考。

> 本文既是**策略计划**，也是给接手 Claude 的**可执行交接**。目标对象：`docs/install.md`、`docs/integration.md`、`docs/usage.md`、`docs/troubleshooting.md` 四篇。

---

## 0. 一句话任务

四篇文档**已存在且篇幅不小**（install 263 / integration 924 / usage 529 / troubleshooting 513 行），由之前的 AI 会话基于代码生成。当前要做的不是从零写，而是：**核验真实准确性 → 补齐缺失 → 统一口径 → 清晰化**，并给故障排查一套诚实的写法。

---

## 1. 背景与受众

- **主受众：测试人员**。他们会照这四篇文档操作平台、并据此测试功能完整性。所以文档里**每一条 CLI 标志、路径、端点、配置、UI 步骤、指标口径都必须与平台真实行为一致**——文档说能做的，平台必须真能做；否则测试员会卡住、且直接暴露文档不可信。
- **次受众：评测大赛评委**，可能翻阅。要求清晰、专业、可快速浏览（目录清晰、术语一致、无冗余）。
- **两者统一于一个原则：真实 + 清晰 + 完整**。准确性是底线，清晰度服务评委，完整性服务测试员。**不为凑字数编造任何内容**（故障排查尤其如此，见第 7 节）。

---

## 2. 现状评估

| 文档 | 行数 | 覆盖 | 状态判断 |
|---|---|---|---|
| install.md | 263 | Docker/手动两条路径、环境变量、字体、安装验证、常见问题 | 结构完整；需核 .env 变量与 `.env.example` 一致、两条路径可跑通 |
| integration.md | 924 | 评测闭环总览、算法版本注册(Web+REST)、事件类型、打水印+真值标注、告警导入(文件名/alert_types/数据集/OCR/评测集/模式)、评测任务(创建/执行/确认/指标/报告) | 最详尽；需核 REST 端点、水印设置、文件名规范、指标口径 |
| usage.md | 529 | 快速开始、8 个 Web 页、CLI 三脚本、OCR 规则、评测全流程 8 步、REST API(旧+v1) | 需核 CLI 标志(已发现 process.py 与 process_single.py 混淆风险)、Web 页与 templates 一致 |
| troubleshooting.md | 513 | 16 条：安装/OCR/水印/评测/AI助手 | 多数有真实根基；需逐条核验来源、删凑字数、补测试期积累机制 |

**关键判断**：四篇是"代码派生"产物，结构完整，但**准确性未经实测复核**，存在过时/混淆/编造风险。首要工作是系统核验。

---

## 3. 核心原则

1. **真实优先**：任何一条可执行内容（CLI/路径/端点/配置/UI 步骤/指标公式）必须有代码对照。拿不准就去代码里 `grep`/读源码，**不要凭印象写**。
2. **照文档可跑通**：测试员照 install 能从零部署、照 usage 能跑通评测全流程、照 integration 能完成接入。
3. **口径一致**：四篇之间、以及与 CLAUDE.md 之间不得矛盾（如告警类型配置文件名、指标公式、水印格式）。发现矛盾以**代码现状**为准，并同步修正 CLAUDE.md。
4. **不编造**：故障排查只写代码可佐证/已知真实/可复现环境的问题；运行时未遇过的报错不写（见第 7 节）。
5. **清晰**：中文、短句、命令用代码块、步骤编号、每篇开头一句话用途 + 目录。避免过度技术化（评委也看）。

---

## 4. 真值锚点清单（核验依据）

接手时先建立这份"事实基线"，再逐条核文档：

**CLI（用 `grep add_argument` 核每个文档里的标志归属）**
- `process.py`：只有 `--single` / `--batch` / `--install` / `--help` 四个。
- `scripts/process_single.py`：`input_video`(位置参数) / `--output-dir` / `--video-id` / `--tpad-duration`。
- `scripts/ocr_easy.py`、`scripts/verify_alert.py`：同样用 `grep add_argument` 取真实标志。

**路由 / API**
- 11 个蓝图（`app/__init__.py`）：videos、alerts、verification、evaluation、auto_annotation、streaming、algorithms、assistant、api_config、review、extract。
- 旧版 API：各蓝图内嵌 `@bp.route("/api/...")`。
- REST API v1：**真实存在**，`app/__init__.py:32` 注册 `/api/v1/*`，与旧端点并行、旧端点保留。核 v1 端点见 `docs/sxs-rest-api-*.md`、`docs/rest-api-v1-delivery-summary.md`、记忆 `rest-api-migration.md`。

**配置 / 部署**
- `.env` / `.env.example`（核文档列的变量名与此一致）、`docker-compose.yml`（两个 service，环境变量在 `environment:` 段）、`app/config.py`、`Dockerfile`。

**数据契约**
- 真值：`ground_truth/{video_id}.json`，结构 `{file, id, events:[{type, start, end}]}`。
- 告警类型配置：**真实文件是 `config/alert_types.json`**（代码 `current_app.config.get("ALERT_TYPES_CONFIG", "config/alert_types.json")`）。⚠️ CLAUDE.md 仍写 `report/config.json`，已过时，需统一。
- 告警文件名规范、水印设置（字体/位置/字号）：以 `scripts/process_single.py` 现状为准。

**指标口径（权威）**
- CLAUDE.md "评测核心指标计算逻辑" 一节是权威：精确率=正确/有效告警；召回率按事件类型算术平均、`hit_count=min(actual,confirmed)` 封顶；平均误检/小时按类型算术平均；所有统计走 `_get_effective_status`。文档必须与此一致。（注：申报 docx 已做评委化简化，但**这四篇技术文档要保持精确口径**。）

**已知真实 bug / 修复（故障排查素材）**
- CLAUDE.md：`sqlite3.Row` 无 `.get()` 方法。
- `docs/api-robustness-fix-handoff.md`、`docs/od-classification-handoff.md`：真实修复记录。
- 真实错误锚点：`app/routes/*.py`、`app/services/*.py` 中 `raise ValueError`(×13)、`raise RuntimeError`(×3)，以及各处边界校验/文件缺失/配置缺失分支。

---

## 5. 已发现的可疑 / 待核验点（示范，非全部）

接手 Claude 要按这个力度继续找：

1. **usage.md §3.1（process.py）**：第 245 行附近写"指定输出目录、视频 ID、开头黑帧时长"——这些是 `scripts/process_single.py` 的参数，不是 `process.py`。核文档是否把两者混淆，归到正确脚本。
2. **告警类型配置文件名**：integration.md §3.3 写 `alert_types.json`（对），但 CLAUDE.md 写 `report/config.json`（过时）。统一为 `config/alert_types.json`。
3. **REST API v1 端点**：usage.md/integration.md 列的 v1 端点，逐个对照 v1 路由定义核实路径与方法。
4. **8 个 Web 页**：usage.md 列的 `/videos/` `/alerts/` `/evaluation/` `/algorithms/` `/assistant/` `/auto-annotation/` `/streaming/` `/api-config/` 是否与 `app/templates/` 实际页面 + 路由一致；`/pre-analysis`、`/review`、`/extract`、`/compare` 等页面是否被遗漏。
5. **指标公式**：usage.md/integration.md 的指标段落与 CLAUDE.md 权威口径逐字比对。
6. **.env 变量**：install.md §环境变量 与 `.env.example` 逐项核对。

---

## 6. 各文档的核验与完善方法

### 6.1 install.md
- Docker 路径：照 `docker-compose.yml` + `Dockerfile` 核步骤；确认 `docker compose up -d` 命令、健康检查、端口、卷映射真实。
- 手动路径：FFmpeg(含 drawtext/libfreetype)、venv、CPU 版 torch、Playwright 浏览器、字体——每条与 `requirements.txt`/`process.py --install`/`Dockerfile` 对照。
- `.env` 变量段：与 `.env.example` 逐项一致。
- 安装验证步骤：真实可执行（健康检查端点、`ffmpeg -drawtext`、EasyOCR 模型加载）。
- 字体路径说明：跨平台真实（与 CLAUDE.md 跨平台路径规范一致）。

### 6.2 integration.md
- 算法版本注册 / 事件类型：REST 端点与 `app/routes/algorithms.py` 逐个核对（创建/更新/删除/引用/批量下载）。
- 水印格式与 FFmpeg 设置：与 `scripts/process_single.py` 现状一致。
- 告警文件名规范 / 解析规则 / `alert_types.json` 格式：与代码一致（配置文件统一为 `config/alert_types.json`）。
- 评测任务参数（merge_interval/event_start/event_end/event_interval/trigger_rate/min_event_duration）：与 `eval_tasks` 表 schema（`app/database.py`）+ `eval_service.py` 一致。
- 指标口径：与 CLAUDE.md 一致。
- 报告生成：与实际报告路由/服务一致。

### 6.3 usage.md
- 8 个（+可能遗漏的）Web 页：每页与 `app/templates/*.html` + 路由核对，步骤与实际 UI 一致。
- CLI 三脚本：§3.1 process.py、§3.2 ocr_easy.py、§3.3 verify_alert.py——逐个 `grep add_argument` 核标志，纠正 process.py/process_single.py 混淆。
- 评测全流程 8 步：与 `app/templates/eval_task.html` 实际 UI 流程一致（含测前分析、合并事件、确认锁定、报告）。
- REST API：旧版 + v1 端点真实。

### 6.4 troubleshooting.md
见第 7 节专门方法。

---

## 7. 故障排查的诚实写法（重点）

**现状**：513 行、16 条，多数有真实根基（sqlite3.Row .get()、Playwright debian11、EasyOCR 模型下载、fontfile 找不到、端口 8080 占用等）。但用户明确：真实故障需测试员实操积累，**不能凑字数编造**。当前感觉这部分"有缺失"。

**方法（四步，严格按序）**：

### 7.1 逐条核验现有 16 条
每条必须能落到一个**可佐证来源**之一：
- 代码里的 `raise/abort/return 4xx`/边界校验/文件缺失分支（给出文件:行）；
- CLAUDE.md 已知 bug（如 sqlite3.Row .get()）；
- `docs/*-handoff.md` 里的真实修复；
- 可复现的环境/依赖问题（FFmpeg/字体/端口/Docker/模型下载）。

**无任何来源的条目 → 删除或改写到有来源为止。** 给每条加一个来源标注（HTML 注释 `<!-- src: file:line -->` 或文末"来源"列），便于后续审计。

### 7.2 从代码挖真实故障路径
对 `app/routes/*.py`、`app/services/*.py`、`scripts/*.py` 跑：
```
grep -rnE "raise |abort\(|return .*4[0-9]{2}" app/ scripts/
```
每条真实的错误路径（校验失败、文件不存在、配置缺失、状态非法）可对应一条排查。**只写代码里确实会抛出的**。

### 7.3 环境/依赖类（真实可复现，可放心写）
FFmpeg 缺失/无 drawtext、字体路径不对、EasyOCR 模型下载失败、Playwright 浏览器未装、端口 8080/5000 冲突、Docker daemon 未运行、`.env` 缺 key、磁盘/权限、`/userdata/...` 路径适配（见 CLAUDE.md 跨平台规范）。

### 7.4 测试期积累机制（living document）
真正的运行时故障要靠测试员实操积累，现在写不出。**诚实做法**：
- 文末设 **"测试期积累"** 区，给结构化模板：`现象 / 复现步骤 / 期望 / 实际 / 原因 / 解决 / 状态`。
- 区首明确标注：**"本节为初始版本，基于已知约束与代码审计；运行时新问题将在测试期持续补充。"**
- 这样既不编造凑数，又给测试员一个可填充的框架。

**红线**：不编造没遇到过的运行时报错、不为凑篇幅造场景。宁可条目少而真实，不要多而虚。

---

## 8. 风格规范

- 中文；短句；命令/路径/文件名用代码块。
- 步骤统一编号；每篇开头一句话写用途 + 目录。
- 术语一致（如"告警"vs"报警"统一、"精确率/召回率"统一）。
- 避免过度技术化（评委也看），但关键参数要准。
- 四篇互链（install 验证后指向 usage、usage 接入指向 integration、遇错指向 troubleshooting）。

---

## 9. 完成判定（验收标准）

1. **逐条核验清单**：每条 CLI 标志/端点/路径/配置/指标公式都有"代码对照"记录（可放单独核验清单文件或文内注释）。
2. **可跑通**：照 install.md 从零部署成功；照 usage.md 跑通评测全流程；照 integration.md 完成一次接入。
3. **口径一致**：四篇 + CLAUDE.md 之间告警类型文件名、水印格式、指标公式无矛盾。
4. **故障排查**：每条有来源标注；无凑字数条目；有测试期积累模板。
5. **清晰度**：每篇有目录+用途说明；步骤可照做。

---

## 10. 执行步骤清单（给接手 Claude）

1. 通读四篇 + CLAUDE.md，建立第 4 节"真值锚点"基线（可写一份 `docs/doc-verify-checklist.md`）。
2. 按第 5、6 节逐篇核验：每发现一处文档与代码不符，**改文档（除非 CLAUDE.md 过时则改 CLAUDE.md）**，并在核验清单记一行。
3. 重点核：process.py/process_single.py 标志归属、告警类型文件名统一、REST v1 端点、指标口径、.env 变量。
4. 按第 7 节重做 troubleshooting：核验现有条目→补代码挖出的真实路径→补环境/依赖类→加测试期积累模板→删无来源条目。
5. 统一口径与风格（第 8 节），四篇互链。
6. 用第 9 节验收标准自检；有条件则实跑一次 install→usage 全流程验证。
7. 把核验清单与本次改动摘要一并交付。

---

## 11. 参考资料索引
- `CLAUDE.md`：架构、评测核心指标计算逻辑、sqlite3.Row bug、跨平台路径规范。
- `docs/sxs-rest-api-*.md`、`docs/rest-api-v1-delivery-summary.md`、`docs/rest-api-error-codes.md`：REST v1 真相。
- `docs/api-robustness-fix-handoff.md`、`docs/od-classification-handoff.md`：真实修复。
- `app/__init__.py`、`app/routes/`、`app/services/`、`app/database.py`、`app/config.py`：代码基线。
- `scripts/process_single.py`、`scripts/ocr_easy.py`、`scripts/verify_alert.py`、`process.py`：CLI 基线。
- `.env.example`、`docker-compose.yml`、`Dockerfile`、`requirements.txt`：部署基线。
- 记忆 `rest-api-migration.md`：v1 迁移现状与错误码方案。
