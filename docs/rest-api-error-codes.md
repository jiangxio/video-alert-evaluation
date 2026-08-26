# /api/v1 响应信封与错误码规范（方案 3）

> 本规范描述 `/api/v1/*` 的**实际落地**实现（`app/api/v1/responses.py` + `errors.py` + 各模块）。
> 方案 3：**HTTP 状态码作语义层，字符串 `error_code` 作业务细分（可选）**。与 sxs 系列文档（alerts-ocr 第 4、algorithms 第 5 模块）规划的 5 位 H-FF-SS 数字码**不同**——后者未落地，见附录 A。

---

## 1. 统一信封格式

| 类型 | HTTP | body |
|---|---|---|
| 成功（读/同步动作） | 200 | `{"code":0,"message":"ok","data":...}` |
| 创建 | 201 | `{"code":0,"message":"created","data":{...}}` + `Location` 头 |
| 异步受理 | 202 | `{"code":0,"message":"accepted","data":null}` + `Location` 头（指状态查询） |
| 无内容 | 204 | 空体（DELETE / PUT 空返回） |
| 错误 | 4xx/5xx | `{"code":<HTTP码>,"message":"...","error_code":"..."?,"errors":[...]?}` |
| 二进制（下载/缩略图/日志） | 200 | 不走信封，`send_file` 直出；错误仍 `raise ApiError` 由 errorhandler 套信封 |

约定：`code` 字段**始终是标准 HTTP 状态码**（成功 0、错误 4xx/5xx），不是业务码。业务细分由可选 `error_code`（字符串）承担。

## 2. 工具函数（`responses.py`，全模块复用）

| 函数 | 用途 |
|---|---|
| `ok(data=None)` | 成功，200 |
| `created(data=None, location=None)` | 创建，201，可选 `Location` |
| `accepted(location=None)` | 异步受理，202 |
| `no_content()` | 204（DELETE / PUT 空） |
| `err(code, message, errors=None, error_code=None)` | 错误信封，`code`=HTTP 状态码，`return jsonify(payload), code` |
| `paginate(items, total, page, page_size)` | 列表 data：`{items,total,page,page_size,has_next}` |
| `parse_pagination(args)` | 解析 `?page=&page_size=`，容错默认 |
| `pick_fields(data, allowed)` | PATCH 字段白名单，返 `(已知dict, 未知名列表)` |

`err()` 向后兼容：旧的两参/三参调用（`err(404, "..."））零改动；需细分才加 `error_code`。

### `ApiError` 异常

`responses.ApiError(code, message, errors=None, error_code=None)`——端点内可 `raise ApiError(...)` 替代 `return err(...)`，由 app 级 errorhandler（`errors.py`）转成错误信封，让代码更线性。

> **现状**：`ApiError` 已定义但端点内**零调用**，当前各端点统一用 `return err(...)`。新端点可选改用 `raise ApiError(...)`，两者最终信封一致。

## 3. HTTP 状态码使用规范

| 码 | 场景 | 备注 |
|---|---|---|
| 200 | 读取、同步动作成功 | 默认成功码 |
| 201 | 资源创建（POST） | 带 `Location` 指新资源 |
| 202 | 异步动作受理 | 带 `Location` 指状态查询；当前模块使用有限 |
| 204 | DELETE、PUT 空返回 | 无 body |
| 400 | 客户端坏输入：缺字段、非法值、未知字段、校验失败 | 当前直接产生的主要错误码 |
| 404 | 资源不存在 | errorhandler 兜底 `/api/v1/*` 404 |
| 409 | 冲突（被引用无法删除 / 资源状态冲突） | **预留**：当前 v1 端点未直接 `err(409)` 产生；委托端点可能透传旧视图 409 |
| 500 | 服务器内部错误 | errorhandler 兜底，防 HTML 500 泄漏给 API 客户端 |

> **无 403**：v1 不用 403——禁止类场景（如非法路径防穿越）归 400（客户端坏输入）。这与 5 位码提案「H 位无 403 档」的取舍一致。

## 4. `error_code` 业务码（字符串）

### 设计

- **HTTP 码 = 语义层（粗）**：客户端只看 HTTP 码即可决定重试/告警策略。
- **`error_code` = 业务细分（可选）**：同类 HTTP 码需精确定位时传字符串码，便于排错与前端分支。
- 不传 `error_code` 时该键不出现——与旧调用完全兼容。

### 命名约定

`SCREAMING_SNAKE_CASE`，形如 `<RESOURCE>_<REASON>`（`DATASET_NOT_FOUND`、`UNKNOWN_FIELD`）。新增码遵循：资源在前、原因在后、大写蛇形。

### 已用枚举（代码核查，截至本文档）

| `error_code` | HTTP | 触发场景 | 出现端点 |
|---|---|---|---|
| `DATASET_NOT_FOUND` | 404 | 数据集 id 不存在 | `alerts.py` 数据集详情/改字段 |
| `EVAL_SET_NOT_FOUND` | 404 | 评测集 id 不存在 | `alerts.py` 评测集详情/增删成员 |
| `UNKNOWN_FIELD` | 400 | PATCH 传了未开放字段（`pick_fields` 拒绝） | `alerts.py` 各 PATCH 端点 |
| `VALIDATION_ERROR` | 400 | 业务校验失败（如未选数据集就批量加/移） | `alerts.py` batch-add/remove |

### 兜底（无 `error_code`）

| HTTP | message | 出处 |
|---|---|---|
| 404 | `资源不存在` | `errors.py` errorhandler 兜底（未匹配到的 v1 404） |
| 500 | `服务器内部错误` | `errors.py` errorhandler 兜底 |

## 5. 委托端点的降级（`compat.py`）

委托旧视图的端点（`videos.py`、`alerts_ocr.py` 经 `wrap_old_view`）转换旧视图返回值为信封：

- 旧视图成功 → `ok(data)` / `paginate(...)`
- 旧视图错误 → `err(status, _extract_message(data))`，**不带 `error_code`**

**边界**：委托端点的错误只有 HTTP 码 + message，**无业务码**（旧视图本就没有）。需细分时应在 v1 handler 里显式判 `status` 并补 `error_code`，而非依赖委托透传。`alerts_ocr.py` 的 `ocr-status` 无任务 404→200 即此类显式修正范例。

## 6. `errorhandler` 分流（`errors.py`，`register_error_handlers(app)`）

| 触发 | `/api/v1/*` | 其余路径 |
|---|---|---|
| 404 | `err(404,"资源不存在")` 信封 | 复刻 Flask 默认 HTML 404（页面原行为不变） |
| 500 | `err(500,"服务器内部错误")` 信封 | 回退默认 |
| `ApiError` | `err(e.code, e.message, e.errors, e.error_code)` | 同 |

注册于 `create_app` 末尾，不覆盖现有 413 handler。

## 7. 新增端点的错误码清单

写新 v1 端点时，按此核对：
1. 选 HTTP 码（§3）：坏输入→400、不存在→404、冲突→409、内部→500。
2. 同类需细分才加 `error_code`（§4 命名约定），无必要则省略。
3. PATCH 端点用 `pick_fields` 拒绝未知字段→`err(400, ..., error_code="UNKNOWN_FIELD")`。
4. 委托旧视图的端点，错误走 `compat` 降级（无 `error_code`）；需细分则 handler 内显式判 `status` 补码。
5. 二进制响应不走信封，错误 `raise ApiError`。
6. 成功：读/同步→`ok`、创建→`created`+`Location`、异步→`accepted`+`Location`、删除→`no_content`。

---

## 附录 A：未采纳提案——5 位 H-FF-SS 数字码

`sxs-rest-api-alerts-ocr.md`（第 4 模块）、`sxs-rest-api-algorithms-event-types.md`（第 5 模块）规划了 5 位码 `H FF SS`（H=HTTP 族、FF=功能族、SS=序号，如 `10600`/`20700`/`30340`），声称「H 与 http_status 严格对应」「看码知语义」。

**代码核查结论：未落地。**
- `grep '\b[1-5][0-9]{4}\b' app/api/v1/` **零命中**，无任何 5 位码。
- `responses.py` 实现的是方案 3（本规范），`alerts.py` 已用字符串 `error_code`。
- `alerts_ocr.py` docstring 明写「方案3 error_code」。
- 第 5 模块的 `app/api/v1/algorithms.py`、`event_types.py` 及对应测试**不存在**（文档的「已交付 36 测试全绿」是完成态表述的提案）。

**关系**：5 位码提案的动机（精确业务定位、码与 HTTP 自洽）已被方案 3 以更轻的方式满足（HTTP 码 + 字符串 `error_code`）。5 位码用数字首位编码 HTTP 族，方案 3 用 HTTP 码本身作语义层、字符串作细分——后者自文档化（`DATASET_NOT_FOUND` 比 `20400` 易读）且零迁移成本。

**若未来要采纳 5 位码**：需改 `err()`/`ApiError` 签名（`code` 从 HTTP 码改 5 位业务码，另传 HTTP 状态）、回改 `alerts.py` 现有调用、调整 `errors.py` errorhandler、`compat.py` 委托转换，并回归全 v1 测试。属全局改动，建议单独立项。

## 附录 B：与 sxs 文档表述的出入

| sxs 文档表述 | 代码实际 |
|---|---|
| 第 5 模块「已交付，36 测试全绿」 | `algorithms.py`/`event_types.py`/测试均不存在，未落地 |
| 第 4/5 模块用 5 位 H-FF-SS 码 | 零命中；实际方案 3（HTTP 码 + 字符串 `error_code`） |
| `ApiError` 端点内使用 | 已定义但零调用；端点统一 `return err()` |
