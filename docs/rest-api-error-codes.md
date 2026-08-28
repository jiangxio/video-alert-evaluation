# REST API 错误码方案

> 适用范围：`/api/v1/*` REST API。本仓 `/api/v1/` 错误响应统一采用 **方案3**（与上游 `origin/main` 对齐）。

## 方案3：HTTP 状态码即错误码

错误信封：

```json
{"code": <HTTP 状态码>, "message": "...", "error_code": "...?", "errors": [...?]}
```

- `code` = **标准 HTTP 状态码**（成功 `0`；错误 `400`/`404`/`409`/`500` 等）。
- `message` = 人类可读描述。
- `error_code`（可选）= 业务码字符串，用于同类 HTTP 状态需精确区分时（如 `"DATASET_NOT_FOUND"`、`"UNKNOWN_FIELD"`、`"STREAM_CONCURRENCY_LIMIT"`）。不传则不出现该键。
- `errors`（可选）= 字段级错误列表 `[{"field","reason"}]`。

实现：`app/api/v1/responses.py`（`err(code, message, errors?, error_code?)`、`ApiError(code, message, errors?, error_code?)`）、`app/api/v1/errors.py`（app 级 errorhandler，按 `request.path` 是否以 `/api/v1/` 开头分流：v1 路径返信封、其余回退 Flask 默认 HTML）。

## 端点如何产生错误

- **重写端点**：`return err(404, "视频不存在", error_code="VIDEO_NOT_FOUND")`，或 `raise ApiError(409, ..., error_code="...")`（errorhandler 转信封）。
- **委托端点**：`wrap_old_view` 自动把旧视图 `(jsonify({"error":...}), 4xx/5xx)` 转 `err(status, message)`；需 peek 旧 status/body 再加工时用 `_extract` + `ok()`/`err()`。

`error_code` 命名约定：`UPPER_SNAKE_CASE`，仅同类 HTTP 状态需区分时传（如 404 下区分「视频不存在」vs「数据集不存在」），无歧义时省略。各模块实际 `error_code` 见各模块代码（`raise ApiError(..., error_code=...)` / `return err(..., error_code=...)`）。

## 成功响应

`ok(data)` → `{"code":0,"message":"ok","data":...}`（200）；`created(data, location)` → 201（`message:"created"`）；`accepted(location)` → 202（`message:"accepted"`）；`no_content()` → 204。**成功 `code` 恒为 0**。

## 列表分页

`ok(paginate(items, total, page, page_size))` → `data:{items,total,page,page_size,has_next}`；`parse_pagination(request.args)` 解析 `?page/&page_size=`（默认 20，上限 100，非法值容错到默认）。委托旧裸列表用 `paginate_old_list(old_call, list_key=)`（内存分页）。

## PATCH 字段白名单

`reject_unknown_fields(data, {"field", ...})` → 有未知字段返 `err(400, "不支持的字段: ...", error_code="UNKNOWN_FIELD")`，否则 `None`。端点用法：`resp = reject_unknown_fields(data, {"mode"}); if resp: return resp`。

---

## 已废弃：5 位 `H FF SS` 方案

本仓早期曾用 5 位业务码 `H FF SS`（H=HTTP 类、FF=资源族、SS=族内），上游 `origin/main` 的 `app/api/v1/alerts_ocr.py` 注释明确拒绝该方案：

> 不引入 sxs 文档的 5 位错误码 / BLUEPRINTS / call_old_view——沿用已落地的 wrap_old_view + v1_bp + 方案3 error_code。

本仓已对齐方案3：5 位业务码全部退役，`ApiError` 签名改为 `ApiError(code=http, message, errors?, error_code?)`（`code` 即 HTTP 状态），各子文档（`rest-api-*.md`）中遗留的 5 位码列仅作历史参考，以代码实际行为为准。
