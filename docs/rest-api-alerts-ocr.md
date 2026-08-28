# /api/v1 alerts OCR 系列改造文档

> ⚠️ **错误码已改为方案3**（HTTP 状态即 `code` + 可选 `error_code` 字符串）。下方 5 位 `H FF SS` 码列已废弃，以代码实际行为为准；完整规范见 [错误码文档](./rest-api-error-codes.md)。

> REST API 改造第 4 模块。把 `app/routes/alerts.py` 里 5 个旧 OCR 视图资源化进 `/api/v1/alerts/*`,统一信封 + 5 位错误码。**旧 OCR 逻辑(后台线程 / `_ocr_progress` / `_ocr_lock` / 线程内独立 sqlite 连接)属 CLAUDE.md 警告的高风险区,只委托不改。**

## 1. 背景

旧 OCR 端点混在 `alerts` 蓝图(`/alerts/api/...`)里,返回裸 JSON + HTTP 码,无统一信封、无结构化错误码。本模块把它们改造为独立 `/api/v1/alerts/*` REST 端点,与已完成的 videos / alerts 查询型 CRUD 模块风格一致。新旧并行:旧端点保留并自动加弃用 header(`Deprecation: true` + `Link`),前端继续用旧 URL。

范围:**URL 资源化 + 统一信封 + 5 位错误码**,不改交互语义、不做 schema 校验、不做鉴权。

## 2. 改动文件清单

| 文件 | 类型 | 作用 |
|---|---|---|
| `app/api/v1/compat.py` | 新建 | 委托层:`call_old_view(old_func, *args, **kwargs)` 调旧视图,拆 `(jsonify[, code])` → `(body_dict, status)` |
| `app/api/v1/alerts_ocr.py` | 新建 | 5 个新端点,委托旧视图 + 按 status 分流套信封 / `raise ApiError` |
| `app/api/v1/__init__.py` | 改 | `BLUEPRINTS` 加入 `alerts_ocr.bp` |
| `tests/conftest.py` | 改 | `app` fixture 双 patch `DATABASE_PATH` + teardown 删 tmp 库 + 注册 `slow` marker |
| `tests/test_api_v1_alerts_ocr.py` | 新建 | 9 用例(4 真跑 EasyOCR)+ PIL 生成水印图 + `_reset_ocr_progress` 隔离 fixture |

**未触碰**:`scripts/ocr_easy.py` 的 `preprocess_and_ocr` 生产代码、CLAUDE.md 的 OCR 裁剪区域描述、旧 OCR 视图逻辑。

## 3. 端点详情(5 个)

| # | 方法 + 路径 | 委托旧视图 | 成功信封(200) | 错误码映射 |
|---|---|---|---|---|
| 1 | `POST /alerts/images/<id>/ocr` | `ocr_single` | `ok({success, ocr})` | 404 → `20320` 图片不存在 |
| 2 | `POST /alerts/images/<id>/ocr:manual` | `ocr_save_manual` | `ok({ocr})` | 404 → `20320` 图片不存在 |
| 3 | `POST /alerts/datasets/<id>/ocr:batch` | `ocr_batch` | `ok({total})`(**保持 200,不改 202**) | 404 → `20220` 数据集不存在;400 → `10311` 无可 OCR 的图;409 → `30340` 运行中 |
| 4 | `GET /alerts/datasets/<id>/ocr-status` | `ocr_status` | `ok(progress)`;**无任务 → 200 空进度 + message**(修正旧版 404) | 无(不校验数据集存在性,对齐旧版) |
| 5 | `POST /alerts/datasets/<id>/ocr-status:cancel` | `ocr_cancel` | `ok({cancelled: true})`(幂等,未运行也 200) | 无 |

> 路径前缀均为 `/api/v1`。RPC 动作用 `:action` 后缀(`:manual` / `:batch` / `:cancel`),子资源用名词(`ocr-status`)。

### 保持的语义(不改交互语义原则)

- **ocr_single 的 OCR 失败仍 HTTP 200**:`success:false, ocr:{error}` 在 data 里(业务成功与否在 data,不在 HTTP)。
- **ocr_batch 保持 200**:旧视图当场同步起 daemon 线程(非排队),故 200 而非 202。
- **ocr_status 无任务返 200 空进度**:修正旧版 404,让前端轮询统一按 data 解析,无需区分 404/200。
- **ocr_cancel 幂等**:未运行也返 200 `cancelled:true`,对齐旧版。

## 4. 错误码(5 位 `H FF SS`,详见 `docs/rest-api-error-codes.md`)

OCR 直接用新码(FF=03 alert-images / FF=02 datasets),不走旧的 4 位码迁移。

| 码 | 含义 | HTTP | 触发场景 |
|---|---|---|---|
| `20320` | 图片不存在 | 404 | `ocr_single` / `ocr_save_manual` 找不到 image_id |
| `20220` | 数据集不存在 | 404 | `ocr_batch` 找不到 dataset_id |
| `10311` | 没有需要 OCR 的图片 | 400 | `ocr_batch` 查不到待 OCR 的图(空数据集 或 全已成功) |
| `30340` | OCR 正在运行中 | 409 | `ocr_batch` 重复启动,`_ocr_progress[...].running` 为真 |

> 边界:`20220`(数据集不存在,404)vs `10311`(数据集存在但无可 OCR 的图,400)。

## 5. 关键设计:委托而非重写

旧 OCR 视图依赖内存态 `_ocr_progress` + `_ocr_lock` + 后台 daemon 线程 + 线程内独立 sqlite 连接——属 CLAUDE.md 警告的高风险区。**只委托不改逻辑**:

```
新端点 (alerts_ocr.py)
  → compat.call_old_view(旧视图函数, id)     # 同一个 request context 内直接调用
    → 旧视图 (app/routes/alerts.py)          # request/get_db/current_app 可用
  ← (body_dict, status)                       # 拆 jsonify 返回
  → 按 status 分流:404/400/409 → raise ApiError;200 → ok(...)
```

- `ocr:manual` / `ocr:batch` 的请求体(`video_id`/`force_all` 等)**由旧视图自己 `request.get_json()` 读**,新端点只透传 + 套信封,不重复解析。
- `ocr_status` 的 404 分支特殊:不 raise,改为 `ok(空进度, message=...)`,这是唯一改了语义的点(修正旧版轮询痛点)。

## 6. 测试方案

### 真跑 EasyOCR(不 mock)

用户要求:OCR 不能 mock,否则会漏委托链 bug。测试用 PIL 运行时生成带水印的告警图,真跑 EasyOCR 识别。

- 水印图:`_make_watermarked_png()` 用 PIL 画 640×360 暗底 + 左上 32px 白字 `"{video_id} | {hhmmss}"`,复用 `scripts/process_single.find_font()` 取系统字体。
- 真实返回结构:`run_ocr` → `{image, raw_ocr_text, video_id, timestamp, timestamp_seconds, success}`;`parse_watermark_text` 解析出 10 位 video_id + `HH:MM:SS.000`。
- slow 用例标 `@pytest.mark.slow`,EasyOCR CPU 约 7.6s/张,首次加模型加载 ~10s。

### 用例(9 个)

| 用例 | slow | 验证点 |
|---|---|---|
| `test_ocr_single_real` | ✓ | 真识别出 `0460000001` + ocr_results 落库 |
| `test_ocr_single_not_found` | | 404 + 码 `20320` |
| `test_ocr_save_manual` | | 手动保存落库 + 读回 |
| `test_ocr_save_manual_not_found` | | 404 + 码 `20320` |
| `test_ocr_batch_real` | ✓ | 批量跑完,轮询到 done==total |
| `test_ocr_batch_conflict` | ✓ | 重复启动 → 409 `30340` |
| `test_ocr_batch_no_images` | | 空数据集 → 400 `10311` |
| `test_ocr_status_empty` | | 无任务 → 200 空进度 + message |
| `test_ocr_cancel` | ✓ | cancel 后 cancelled=true、线程退出、第 2 张被跳过 |

### 隔离 fixture(`_reset_ocr_progress`)

`tests/test_api_v1_alerts_ocr.py` 的 `autouse` fixture,依赖 `app`(保证 teardown 顺序在 `app`/`tmp_path` 之前):
1. 取消所有运行中任务(`cancelled=True`);
2. 轮询等后台线程退出(30s 兜底,线程退出即关闭 sqlite 连接、释放 Windows 文件锁);
3. 清空模块级 `_ocr_progress`(避免跨用例串进度——dataset_id 每用例从 1 重来)。

## 7. 实现中踩的坑(根因 + 修复)

### 坑 1:错误码元组索引错位

- **现象**:错误分支返回 status `0`,响应 status 字符串变成 `"0 图片不存在"`(body 对、HTTP 状态 0)。
- **根因**:常量定义为 `(code, message, http_status)`,但 `_raise_from_legacy` 按 `(code, http_status, default_message)` 取 → `mapping[1]`(message 串)被当作 http_status 传入。
- **修复**:统一为 `(code, http_status, default_message)`,与取值函数索引语义对齐。

### 坑 2:DATABASE_PATH 双绑定

- **现象**:批量 OCR 测试的后台线程写真实 `benchmark.db`,污染线上库且测试读不到结果。
- **根因**:`app/routes/alerts.py` 顶部 `from app.database import DATABASE_PATH` 是**导入期捕获的模块内名字**,与 `database.DATABASE_PATH` 是两个独立名字。`get_db()` 运行时查后者(conftest patch 生效,旧测试过),但 `ocr_batch` 的 `_worker` 用前者 → 未 patch。
- **修复**:`tests/conftest.py` 的 `app` fixture 额外加 `monkeypatch.setattr("app.routes.alerts.DATABASE_PATH", db_path)`。凡涉及 `from X import CONST` 的模块都要单独 patch 该模块内绑定。

### 坑 3:Windows 文件锁 + 模块级进度串用例

- **现象**:OCR 后台线程的 sqlite 连接不释放 → tmp 库删不掉(Windows 文件锁);`_ocr_progress` 不清空 → 跨用例串进度。
- **修复**:`_reset_ocr_progress` autouse fixture(见上 §6)+ conftest `app` fixture teardown 显式 `unlink` tmp 库(连带 `-wal`/`-shm`)。两者配合 → 用例后临时库删除、进度清空、无残留线程/连接 = 恢复原状。

### 坑 4:OCR 裁剪区域

- **现象**:测试水印图被 EasyOCR 读成乱码(`'oacono 10.01.0 00'`)。
- **根因**:`run_ocr` → `ocr_easy.preprocess_and_ocr` 只裁左上 `min(540,w)×min(50,h)`(540×50),水印文字超出该区域被截掉。
- **修复**:测试水印图文字画在 `(24,10)` 32px、黑底框 `(16,4)-(525,46)` 内,落在 540×50 裁剪区。修正后 EasyOCR 完美识别 `0460000001 / 00:01:30.000`。
- **约束(用户要求)**:不修正 CLAUDE.md 的 380×100 描述、不改 `preprocess_and_ocr` 生产代码,测试基于实际 540×50 画水印图即可。

## 8. 测试结果

| 命令 | 结果 |
|---|---|
| `py -m pytest tests/test_api_v1_alerts_ocr.py -m "not slow"` | **5 passed**, 4 deselected |
| `py -m pytest tests/test_api_v1_alerts_ocr.py -m "slow"` | **4 passed**(真跑 EasyOCR,~13s) |
| `py -m pytest tests/test_api_v1_alerts_ocr.py` | **9 passed** |
| 全套 v1 回归(videos+alerts+errors+alerts_ocr) | **29 passed, 0 failed** |
| 全项目非 slow | **65 passed, 12 failed**(12 个全是 `test_eval_service.py` 既有失败,与本次无关) |

> 运行环境:`python` 是 Windows Store stub(exit 49 无输出),用 `py` 启动器(Python 3.13.14, pytest 9.1.1, easyocr 1.7.2 + torch 2.13.0+cpu)。

## 9. 状态

- **未 git commit**(按 CLAUDE.md 规则,等用户授权)。
- 下一个模块:**algorithms/event_types**,仍走 plan mode → 批准 → 实现。
