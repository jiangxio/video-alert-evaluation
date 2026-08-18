# sxs2.txt（alerts 模块 REST 提案）借鉴分析

> 调研对象：`sxs2.txt`——某人对 alerts 模块做 `/api/v1/alerts/*` REST 改造的设计提案。
> 调研目的：判断哪些值得借鉴、怎么借鉴、出发点是什么，以及 sxs2 之外还有哪些优化方案。
> 调研依据：实地核查 `app/api/v1/responses.py`、`compat.py`、`videos.py`、`app/routes/alerts.py`（1172 行/31 端点）、`docs/rest-api-feasibility-and-test-plan.md`、`tests/` 现有测试与 golden 机制。
> 结论先行：**sxs2 是一份质量不错的「alerts 实现蓝图」，设计决策比 sxs.txt 更细致，但 (1) 它描述的成果在仓库里完全不存在，是未落地提案；(2) 它的错误码模型与现有基础设施有隐性冲突；(3) 它漏掉了仓库已有的 golden 回归护栏。借鉴其「设计决策」，不照搬其「错误码体系」和「已完成」表述。**

---

## 一、先纠正认知：sxs2 是提案，不是成果

`sxs2.txt` 通篇用「本轮改动总结」「24 个端点」「10 测试全绿」的完成态口吻，但实地核查：

| sxs2 声称 | 仓库实际 |
|---|---|
| `app/api/v1/alerts.py` 新增、24 端点 | **不存在**（`app/api/v1/` 下只有 `__init__/responses/compat/videos`） |
| `tests/test_api_v1_alerts.py` 10 测试全绿 | **不存在** |
| `__init__.py` 注册 alerts.bp | 只有 `v1_bp`，未注册 alerts |
| 旧端点「自动加弃用头」 | `deprecation.py` **仍不存在**，弃用头无处生效 |
| 整体 45 测试全绿 | v1 测试只有 `test_api_v1_videos`+`test_api_v1_responses`（实际 35 passed） |

只有 `main` 一个分支、无 stash、无相关提交。**所以 sxs2 的价值是「别人对 alerts 怎么做 REST 的思路」，不是可依赖的已交付代码。**

但它的设计判断大部分经得起源码核对——下面逐条评估。

---

## 二、sxs2 描述属实性核对（它的设计基于真实代码）

| sxs2 的说法 | 核对结果 |
|---|---|
| 复用旧 alerts.py 的 9 个私有函数 | ✅ 准确：`_get_image_size`/`_load_alert_config`/`_log_image_action`/`_set_dataset_algorithm_versions`/`_get_dataset_algorithm_versions`/`_validate_algorithm_versions`/`_extract_archive`/`_find_image_root`/`_parse_id_list`，正好 9 个 |
| eval-sets 详情 `GET /<id>` 是「新增，REST 补全」 | ✅ 旧路由无 `GET /api/eval-sets/<id>`（只有 PUT/DELETE） |
| datasets 详情 `GET /<id>` 是新增 | ✅ 旧路由无 `GET /api/datasets/<id>` |
| eval-sets PUT 实际只改 name | ✅ `alerts.py:659-675` `rename_alert_eval_set` 只 UPDATE name |
| batch-add 去重返回 added_count | ✅ `alerts.py:611-622` 逐个判重、累计 added_count |
| OCR 5 端点依赖 `_ocr_progress` 内存态+后台线程 | ✅ `ocr_batch` 内 `_worker` 线程（`alerts.py:1077`）、`ocr_status`/`ocr_cancel` 走 `_ocr_progress` |
| 旧端点全保留 | ✅ `app/routes/alerts.py` 未动 |

**结论：sxs2 对旧代码的研判是扎实的，设计决策有据。问题只在「错误码模型」和「完成态表述」两处。**

---

## 三、逐条设计决策：出发点 + 借鉴判断

### 决策 1：单字段更新用 PATCH 不用 PUT，且严格对齐旧字段 ✅ 借鉴

**出发点**：REST 规范里 PUT 是「整体替换」，旧版用 PUT 改单字段（`/mode`、`/label`、eval-sets rename）语义不严谨。sxs2 改 PATCH，且每个端点只开放旧版真实支持的那一个字段（datasets 只 mode、images 只 event_label、eval-sets 只 name），不擅自新增可写字段——避免引入未经验证的写入路径。

**借鉴价值**：高，纯收益。videos 未来做 PATCH（sxs.txt 推迟的 rename/video-id 合并）也应遵循。

**怎么落地**：v1 端点显式取 body 的指定字段，未知字段拒绝。需要配套「字段白名单」机制（见第五节优化 B）。

### 决策 2：`:action` 命名（:batch-add / :batch-remove / :import / :batch-delete）✅ 借鉴

**出发点**：batch-add/remove 是 RPC 语义（「对集合做增量操作」）而非 CRUD，REST 风格用 `:action` 后缀（Google AIP 惯例）表达「自定义方法」，且冒号前缀天然不会与 `<id>` 路径参数撞名。

**借鉴价值**：高。与 sxs.txt 通用约定（「RPC 动词端点用 :action 后缀」）一致，sxs2 给出了具体范例。

**技术验证**：实测 Flask 能正常注册并匹配 `/<int:id>/images:batch-delete`（返回 200 + 正确 JSON），冒号在静态路由段合法。

**怎么落地**：直接采纳命名。注意子资源路径层级——sxs2 把 batch-delete 挂在 `datasets/<id>/images:batch-delete`（集合级动作），符合「动作作用于哪个集合」的直觉。

### 决策 3：download 从 POST 改 GET ✅ 借鉴

**出发点**：打包下载是读操作，该用 GET。旧版 `POST /api/datasets/<id>/download`（`alerts.py:244`）用 POST 多半是历史原因。

**借鉴价值**：中。语义正确。核查旧 `download_dataset(dataset_id)` 只用 path 参数、无需 body，改 GET 无障碍。

**怎么落地**：直接改动词。若未来某下载端点需复杂筛选参数，用 query string 而非 body。

### 决策 4：复用旧 helper 不重写 ✅ 借鉴（但注意分页边界）

**出发点**：与 `docs/rest-api-feasibility-and-test-plan.md` 方案 A（「不改旧视图、接受查询重复」）一致——规避重写带来的未经验证 SQL 风险，零回归。

**借鉴价值**：高。与已落地的 videos 模块思路一致（videos.py 顶部 docstring 明确写「方案 A：不改旧视图、接受重复」）。

**⚠️ sxs2 没点明的边界**：docs 2.2 已警告「**分页补不回来**——列表端点不能用 wrap_old_view 包装，必须重实现」。sxs2 说 alerts 列表「带分页」但没说是内存分页还是 SQL 分页。

- 旧 `list_datasets`/`list_dataset_images`/`list_alert_eval_sets` 都是全量返回。
- videos 的做法（`videos.py:42-48` `_paginate_list`）是**内存分页**：全量取再切片，docstring 标注「服务端 LIMIT/OFFSET 是后续优化」。
- alerts 数据规模小，内存分页可接受，但**必须在 docstring 标注「内存分页」并走 `responses.paginate()`**，不能裸返回 list。

**怎么落地**：import 旧 helper；CRUD/二进制端点走 `wrap_old_view`；列表端点仿 videos 的 `_paginate_list` 模式。

### 决策 5：OCR 5 端点留待下轮 ✅ 借鉴（判断准确）

**出发点**：`ocr_batch` 起后台线程 + `_ocr_progress` 内存态进度，属 docs 2.2「带线程/进程的高风险异步」，应单独用「委托旧视图 + compat.py」处理，不混在本轮。

**借鉴价值**：高，判断与 docs 一致。

**怎么落地**：照做。这 5 个端点（`/ocr`、`/ocr/manual`、`/ocr/batch`、`/ocr/cancel`、`/ocr/status`）下一轮用 `wrap_old_view` 委托，注意 `accepted()`（202）语义匹配异步批处理。

### 决策 6：错误码业务分段（2100/2200/2300/12xx/13xx）❌ 不照搬，需改造

**出发点**：HTTP 状态码粒度粗（400 既可能是「name 为空」也可能是「参数格式错」），业务码能精确定位错误类别，按功能域分段便于排错。

**借鉴价值**：动机合理，但**实现方式与现有基础设施冲突**，是 sxs2 最大的设计缺陷。

**冲突详情**（见第四节）：现有 `responses.py:43-48` 的 `err(code, message)` 把 `code` 同时当 HTTP 状态码（`return jsonify(payload), code`）。若按 sxs2 让 `body.code=2100`，则 HTTP 状态码也变成 2100——这是非标准码（HTTP 标准范围 100-599）。

**实测**：Werkzeug 不报错，原样返回 `status=2100`。程序不会崩，但产生非标准状态码：2xx 本是成功语义却用于错误、客户端/代理/监控可能不认、与 HTTP 语义矛盾。属「坏味道但非硬阻断」。

**怎么处理**：采纳「业务码」的**动机**，但不采纳 sxs2 的「把业务码塞进 code 字段当状态码」的**实现**。改用第五节方案 3（HTTP 状态码 + body 内可选 `error_code` 子字段）。

---

## 四、关键冲突：错误码模型三方案对比

这是动 alerts（及任何后续模块）前**必须先拍板**的事，否则各模块错误信封不一致。

| 方案 | body 形态 | HTTP 状态码 | 优点 | 缺点 |
|---|---|---|---|---|
| **方案1（现状）** | `{"code":404,"message":"..."}` | 404 | 简单；`err()` 签名不动；videos 已用 | 400 无法区分「name 空」vs「格式错」；信息粒度粗 |
| **方案2（sxs2）** | `{"code":2100,"message":"..."}` | 2100（非标准） | 业务码精确、按域分段 | 破坏 HTTP 语义；要改 `err()` 签名 + 回归 videos；非标准码 |
| **方案3（推荐折中）** | `{"code":404,"message":"...","error_code":"DATASET_NOT_FOUND"}` | 404 | HTTP 层标准；业务细节在 `error_code`；`err()` 向后兼容 | 字符串码无数字分段直观 |

**方案3 落地**：扩 `err()` 但向后兼容——

```python
# responses.py：新增可选 error_code，不改现有调用
def err(code, message, errors=None, error_code=None):
    payload = {"code": code, "message": message}
    if error_code is not None:
        payload["error_code"] = error_code
    if errors is not None:
        payload["errors"] = errors
    return jsonify(payload), code
```

- 现有 `err(400, "name 不能为空")` 调用零改动（videos 不受影响）。
- alerts 端点可选写 `err(400, "数据集名称不能为空", error_code="DATASET_NAME_EMPTY")`。
- `compat.py` 包装旧视图错误时，`error_code=None`（旧视图本就没有业务码），自然降级。
- 若坚持要数字分段，`error_code` 也可用数字（如 `2200`），但放 body 不放 status，HTTP 仍是 404。

**决策建议**：选方案3。它保留 sxs2「精确错误定位」的好处，又不破坏 HTTP 语义和现有契约。

---

## 五、sxs2 没覆盖的其他优化方案

### 优化 A：先补 `errors.py` + `deprecation.py`（前置依赖）⚠️ 必须

sxs2 通篇假设「旧端点自动加弃用头」「404 返信封」已就绪，但这俩钩子从 sxs.txt 到现在一直没落地（`register_api` docstring 明写「下一轮接入」）。

**真要做 alerts，必须先补这两块基础设施**，否则 sxs2 描述的弃用头和错误信封分流无处生效。这恰好是 sxs.txt 上一轮欠的债，也是 sxs2 的隐性前提。落地要点（docs 2.1 已列风险）：

- `deprecation.py` 的 `after_request` **只加 header、不改 body**，按 path 前缀精确跳过非旧 API；对二进制/页面响应逐字节不变。
- `errors.py` 的 app 级 `errorhandler(404/500)` 按 `request.path.startswith('/api/v1/')` 分流，保留现有 `413` handler 和页面 404 原行为。
- 顺带接住 `responses.py:51` 已定义但无人用的 `ApiError` 类——让 errorhandler 捕获它转信封，端点里可 `raise ApiError(404, "...")` 替代 `return err(...)`，代码更线性。

### 优化 B：字段白名单工具通用化

sxs2 每个 PATCH 端点手写字段检查，错误码分散（1299/1298/1399）。优化为统一工具：

```python
# responses.py 或新 helper
def pick_fields(data, allowed):
    """返回 (已知字段dict, 未知字段列表)。未知字段非空时端点返 400。"""
    known = {k: v for k, v in (data or {}).items() if k in allowed}
    unknown = [k for k in (data or {}) if k not in allowed]
    return known, unknown
```

各 PATCH 端点统一 `known, unknown = pick_fields(body, {"mode"})`，`unknown` 非空即 `err(400, f"不支持的字段: {unknown}", error_code="UNKNOWN_FIELD")`。避免每个端点重复 if-else。

### 优化 C：沿用并强化 golden 回归护栏（sxs2 完全漏了）

仓库已有 `tests/test_api_golden.py` + `tests/snapshot.py` + `tests/snapshots/old_api/`，这是 docs Layer 5b 的落地——对旧 `/api/*` GET 端点做逐字节快照比对，专抓「全局钩子悄悄改坏旧响应」。**sxs2 通篇没提这个机制，但它是比 sxs2 测试分层更系统的护栏。**

- 做 alerts 时，先把 alerts 旧 GET 端点（`/api/datasets`、`/api/eval-sets`、`/api/images/<id>` 等）纳入 golden 基线（若尚未纳入），再开发 v1 端点。
- `deprecation.py` 上线后，golden 会捕获「弃用头加了但 body 变了」的 bug——这正是它的价值。

### 优化 D：测试清单三分法模板化

sxs2 把测试分成「自动化覆盖 / 已手测 / 自动化已覆盖但未手测」三类，是好习惯。优化为每个模块 PR 附统一模板：

```
自动化覆盖（test_api_v1_<module>.py，N 项全绿）：...
需手测（curl 矩阵）：...
自动化已覆盖、建议手测：...
不在本轮范围：...（如 OCR 系列）
```

融入交付流程，避免「声称全绿但漏了 404 路径」这类 sxs2 自己也承认的盲区（它列了「各端点 404 路径尚未手测」）。

### 优化 E：测试断言聚焦「信封结构」不绑业务值

已落地的 `test_api_v1_videos.py` 采用了好哲学（文件 docstring）：断言信封形状（`code/data/items/total`），不绑定旧视图具体业务返回值。例如 `test_delete_missing_returns_error_envelope` 只断言 `status>=400` 和 `body["code"]==status`，不写死 404。

- sxs2 的测试若有，应沿用此模式，避免「无运行环境下写易碎断言」。
- 这也让测试在 fresh 空 DB 上可跑（不依赖预置数据），降低 CI 门槛。

---

## 六、推荐落地顺序

```
第0步（前置）补 errors.py + deprecation.py + 接住 ApiError
        → 验证：test_api_v1_errors.py / test_deprecation.py 全绿 + golden 基线不漂移
第1步（决策）拍板错误码方案（建议方案3），扩 err() 但向后兼容
        → 验证：test_api_v1_responses.py 回归全绿（videos 不受影响）
第2步（实现）按 sxs2 蓝图做 alerts，沿用：
        - wrap_old_view 委托 CRUD/二进制
        - _paginate_list 内存分页列表端点（仿 videos）
        - PATCH 严格化 + pick_fields 工具
        - :action 命名（冒号路由已验证可行）
        - download POST→GET
        → 验证：test_api_v1_alerts.py + golden（alerts 旧端点纳入基线）
第3步（异步）OCR 5 端点单独一轮，wrap_old_view + accepted() 语义
```

每步独立可验证、独立可回滚，符合 sxs.txt「一轮一模块」的增量精神。

---

## 七、风险与前置依赖速查

| 风险 | 来源 | 对策 |
|---|---|---|
| 错误码模型不一致 | sxs2 方案2 vs 现状方案1 | 第四节方案3，先拍板再动 |
| 弃用头/错误分流无处生效 | errors.py/deprecation.py 未落地 | 第五节优化 A，第0步先补 |
| 列表分页口径漂移 | 内存分页 total 与旧逻辑不一致 | 走 `responses.paginate()`，docstring 标注内存分页 |
| 全局钩子改坏旧响应 | after_request/errorhandler 全局性 | golden 护栏（优化 C）+ 只加 header 不改 body |
| 冒号路由兼容性 | `:batch-add` 非传统 URL | 已实测 Flask 可匹配；标注供前端/网关确认 |
| 旧 helper 的 get_db 上下文 | v1 蓝图里调旧 helper | get_db 是 app-level，应可用；首个端点验证一次 |

---

## 八、一句话总结

**sxs2 的「设计决策」（PATCH 严格化、`:action` 命名、download 改 GET、复用旧 helper、OCR 隔离）几乎都值得借鉴，且经源码核对成立；但它的「错误码业务分段」要改造为方案3（HTTP 码 + body error_code）而非照搬，它的「已完成」表述要当作未落地提案对待。动 alerts 前，必须先补 sxs.txt 欠下的 `errors.py`+`deprecation.py` 基础设施，并复用仓库已有的 golden 回归护栏——这是 sxs2 漏掉但更关键的一环。**
