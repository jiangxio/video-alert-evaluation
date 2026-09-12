# sxs-rest-api-algorithms-event-types.md 借鉴分析

> 调研对象：`sxs-rest-api-algorithms-event-types.md`（REST 改造第 5 模块：algorithms + event-types）。
> **前提更正（重要）**：本分析初稿误把该文档当「已交付模块」评估。实地核查代码后更正——
> - `app/api/v1/` 下**没有** `algorithms.py`、`event_types.py`；`tests/test_api_v1_algorithms.py`、`test_api_v1_event_types.py` 也不存在 → 文档的「已交付、36 测试全绿」是**完成态表述的提案**（和 sxs2.txt 同模式），不是成果。
> - 文档规划的 5 位 H-FF-SS 错误码（10600/20700/…）在全仓库**零命中**；`responses.py` 实际落地的是**方案 3**（HTTP 码 + 字符串 `error_code`），见 `docs/rest-api-error-codes.md`。
>
> 因此本分析问的不是「已交付模块提炼什么」，而是「**这份未落地提案里有哪些设计思想值得后续模块借鉴**」。结论先行：有，主要是设计思想（与是否落地无关）；但「5 位码」不搬——代码已是方案 3。

---

## 一、最值得借鉴：原位重写 vs 委托的二分判定（§6）

**出发点**：旧 `algorithms` 蓝图是纯同步 CRUD + 文件 I/O，无后台线程/锁/进程 → 照 alerts/videos **原位重写 handler**，复用旧模块的纯函数（`get_event_types` / `_sync_alert_types_json` / `send_file_with_cache` / `parse_config` / `secure_filename`），不重复实现。这与 alerts-ocr（第 4 模块）的**委托**选择形成对照——OCR 有 `_ocr_progress` + `_ocr_lock` + daemon 线程 + 线程内独立 sqlite 连接，属高风险区，**只委托不改**。

**借鉴价值**：高。`docs/rest-api-feasibility-and-test-plan.md` 2.2 只给了**端点级**特例（“带线程/进程的高风险异步要委托”“列表分页补不回来必须重实现”）；该提案把它提炼成**模块级**二分判定：

| 模块逻辑性质 | 策略 | 落地手段 |
|---|---|---|
| 同步 CRUD + 文件 I/O，无线程/锁/进程 | **原位重写** | 新 v1 handler 直接写，import 旧模块纯函数 |
| 带后台线程/锁/进程/独立连接 | **委托** | `wrap_old_view` 调旧视图，不改逻辑 |

两极都有范例（委托=alerts_ocr 已落地，原位重写=本提案规划），决策树完整。剩余模块（config 等）开工前先按此判定归属。

**怎么落地**：写进迁移约定作为“模块开工第一步”的 triage：先回答“本模块有没有线程/锁/进程/模块级可变状态”，有→委托，无→原位重写。端点级例外（列表分页必须重实现走 `responses.paginate()`）作为二极规则保留。

## 二、不搬：5 位 H-FF-SS 错误码（§5/§6）——未采纳提案

**更正**：初稿曾认为 5 位码是 `rest-api-alerts-borrow-analysis.md` 第四节方案之争的「干净解」——**错**。核查后：5 位码从未落地，代码用的是该节推荐的**方案 3**（HTTP 码 + 字符串 `error_code`），`alerts.py` 已有 `DATASET_NOT_FOUND` 等枚举。

**可借鉴的只剩「思路」**：错误码与 HTTP 语义自洽（首位/前缀对应 HTTP 族）、冲突 400→409、非法路径 403→400、「只改新端点、旧端点保留加弃用 header」——这些**取舍思路**在方案 3 下同样适用（见 `docs/rest-api-error-codes.md` §3：v1 不用 403、409 预留）。但**码表（10600 等）不搬**，方案 3 用字符串 `error_code` 已满足「精确业务定位」且自文档化。

**若未来要采纳 5 位码**：需全局改动（`err()`/`ApiError` 签名、回改 alerts.py、errors.py、compat.py + 全 v1 回归），单独立项，不在当前借鉴范围。

## 三、可迁移的编码思想：multipart 字段存在性 vs 真值（§7 坑2）

**现象**：旧 `update_version` 用 `description = request.form.get("description", "").strip()` + `if description is not None:`。默认 `""` 使 `is not None` **永真** → 每次 PATCH 都重写 description（不传也置空）→ 错误码 `10603`（空更新）经 multipart 不可达。

**根因**：multipart 用 `.get("field", "")` 默认值丢失「字段是否提供」的存在性语义，`is not None` 被默认值击穿。

**修复**：description 改 `if "description" in request.form`（未传不动、传空显式清空）；`name`/`version_date`/`algorithm_type` 维持 truthy 语义（非空才更新——NOT NULL，不应被空串清空）。

**借鉴价值**：高，纯编码收益，与 REST 无关、任何 multipart 表单适用。可迁移洞察是**区分两类字段**：
- **可清空的可选字段**（如 description）→ 存在性检测 `if field in request.form`（允许显式传空清空，不传则不动）；
- **不可清空的必填字段**（如 name、NOT NULL 列）→ 真值检测 `if data.get(field)`（空串/None 都跳过，不被清空）。

混用即潜伏 bug：必填字段用存在性检测会被空串清空，可选字段用真值检测会丢失「显式清空」能力。

**怎么落地**：作为 multipart PATCH 端点的编码约定 + 配一条回归测试（「PATCH 只传 A 字段 → B 字段不被清空」锁定）。注：该提案的 `update_version` 代码未落地，但旧 `app/routes/algorithms.py` 若存在此怪癖，重写时应一并修正。

## 四、可迁移的测试思想：模块级配置路径常量必须 patch（§7 坑1）

**现象**：`create/update/delete_event_type` 末尾调 `_sync_alert_types_json()`，写到 `app.event_types` 模块级常量 `ALERT_TYPES_CONFIG_PATH`（＝真实 `config/alert_types.json`）。不 patch → 测试改写仓库配置，违反「恢复原状」。

**根因**：`_sync` 用模块级常量定位写文件，与 `app.event_types` 命名空间绑定，**不随 `database.DATABASE_PATH` 变**——双 patch 了 DATABASE_PATH 不等于 patch 了它。

**修复**：conftest 的 `app` fixture 加 `monkeypatch.setattr("app.event_types.ALERT_TYPES_CONFIG_PATH", tmp_path / "alert_types.json")`。

**借鉴价值**：中高，测试隔离的通用 checklist 项。可迁移的是「**追踪读写与绑定层级**」：
- 函数**写**到模块级路径常量 → 该常量必须在 conftest patch（指向 tmp）；
- 函数只**读**配置（如 `init_db`→`_seed_event_types` 读 `config/alert_types.json`，路径硬编码、不读该常量）→ 不写不污染，无需 patch；
- 兜底直连（如 `app.event_types.DATABASE_PATH`）若端点内走请求上下文 `get_db()` 则不触发，无需 patch。

**怎么落地**：迁移每个模块前，扫旧 helper 有没有「写模块级路径常量」的副作用，有的全列入 conftest patch 清单；读写/兜底分开追踪，别假定「patch 了 DB 路径就万事大吉」。

## 五、文档纪律模板（§2/§4/§7 坑3）

低风险但值得固化为每模块交付文档的模板：
- **显式「未触碰」区**（§2 末尾）：声明不动哪些（旧端点、生产代码只 import、`scripts/`）——把爆炸半径写明，契合 CLAUDE.md “Surgical Changes”。
- **每模块 verb 分析表**（§4）：逐端点列 旧法→新法 + 变化 + 理由，哪怕结论是「本轮无动词改动」。诚实记录「no change」及原因，给后人留判据。
- **以 pytest 为准**（§7 坑3）：PowerShell 5.1 + curl.exe 会嚼坏内嵌双引号 JSON → 假阴性。终端 curl 只作信封/弃用 header 肉眼核对，断言以 test client 为准。

## 六、不值得借鉴

- 具体 13 端点 / 5 位码值（10600 等）——提案专属且未落地；
- PowerShell curl 的具体绕法（`Invoke-RestMethod` / `--%`）——环境专属，只留「以 pytest 为准」元规则；
- `event_types` 表 `id` 用 `COALESCE(MAX(id),0)+1`——该表 id 有业务含义，别当通用主键策略搬。

---

## 七、一句话总结

**这份第 5 模块文档是未落地提案（algorithms.py/event_types.py 不存在、5 位码零命中、代码实为方案 3），不能当已交付成果。可借鉴的是设计思想：原位重写 vs 委托的模块级二分判定（与 alerts_ocr 委托极互补，补全决策树）、multipart 字段「存在性 vs 真值」两类语义、模块级配置路径常量必须 patch 的测试 checklist、显式未触碰区 + verb 分析表 + 以 pytest 为准的文档纪律；5 位 H-FF-SS 错误码不搬（方案 3 已落地且更轻）。**
