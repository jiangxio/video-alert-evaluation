# /api/v1 algorithms + event-types 改造文档

> ⚠️ **错误码已改为方案3**（HTTP 状态即 `code` + 可选 `error_code` 字符串）。下方 5 位 `H FF SS` 码列已废弃，以代码实际行为为准；完整规范见 [错误码文档](./rest-api-error-codes.md)。

> REST API 改造第 5 模块。把 `app/routes/algorithms.py` 一个旧蓝图里的**算法版本 CRUD + 算法类型列表 + 事件类型 CRUD** 三组端点资源化进 `/api/v1`,统一信封 + 5 位错误码(FF=06 algorithm-versions / FF=07 event-types)。旧逻辑为**同步 CRUD + 文件 I/O**,无后台线程/锁/进程(与 OCR 高风险区不同)→ **原位重写 handler**(与 alerts/videos 一致),复用 `app.event_types` 的 `get_event_types`/`_sync_alert_types_json`、`app.routes.send_file_with_cache`、`app.services.config_parser.parse_config`,不重复实现。旧端点保留并自动加弃用 header。

## 1. 背景

旧 `algorithms` 蓝图(`/algorithms/api/...`)把算法版本、算法类型、事件类型三组资源混在一起,返回裸 JSON + HTTP 码,无统一信封、无结构化错误码。本模块拆成两个 v1 资源模块,与已完成的 videos / alerts / alerts_ocr 风格一致。新旧并行:旧端点保留并自动加弃用 header(`Deprecation: true` + `Link`,deprecation.py 的 `/algorithms/api/` → `/api/v1/algorithms` 已就绪),前端继续用旧 URL。

范围:**URL 资源化 + 统一信封 + 5 位错误码 + 明显错误状态修正**,不改交互语义(个别状态修正见 §6,均经用户确认)。

## 2. 改动文件清单

| 文件 | 类型 | 作用 |
|---|---|---|
| `app/api/v1/algorithms.py` | 新建 | 8 端点(FF=06):types 列表、versions CRUD+detail、download、batch-download |
| `app/api/v1/event_types.py` | 新建 | 5 端点(FF=07):event-types CRUD + references |
| `app/api/v1/__init__.py` | 改 | `BLUEPRINTS` 加 `algorithms.bp`、`event_types.bp` |
| `tests/conftest.py` | 改 | `app` fixture patch `app.event_types.ALERT_TYPES_CONFIG_PATH`→tmp |
| `tests/test_api_v1_algorithms.py` | 新建 | 20 用例(含 description 修复锁定) |
| `tests/test_api_v1_event_types.py` | 新建 | 16 用例 |

**未触碰**:`app/routes/algorithms.py` 旧端点(新旧并行)、`app/event_types.py` 生产代码(只 import 其函数)、`scripts/`。

## 3. 端点详情(13 个)

### `app/api/v1/algorithms.py`(蓝图 `api_v1_algorithms`,FF=06)

| # | 方法 + 路径 | 旧视图 | 成功响应 | 错误码(5 位) |
|---|---|---|---|---|
| 1 | `GET /algorithms/types` | `list_types` | `ok(get_event_types())`(key 列表) | — |
| 2 | `GET /algorithms/versions` | `list_versions` | `paginated(rows, …)`(每行带 `datasets`) | — |
| 3 | `POST /algorithms/versions` | `create_version`(multipart) | `created({id}, location=/api/v1/algorithms/versions/{id})` | `10600` 类型无效;`10601` 名不能为空;`10602` 日期不能为空 |
| 4 | `GET /algorithms/versions/<id>` | `version_detail`(**去 `/detail` 后缀**,资源 GET 即详情) | `ok({version, datasets, config_info})` | `20600` 不存在 |
| 5 | `PATCH /algorithms/versions/<id>` | `update_version`(multipart) | `ok({id})` | `20600` 不存在;`10600` 类型无效;`10603` 没有要更新的字段 |
| 6 | `DELETE /algorithms/versions/<id>` | `delete_version` | `no_content()`(204) | `20600` 不存在;`30600` 有 N 个数据集使用无法删除(409) |
| 7 | `GET /algorithms/download?path=` | `download_file` | `send_file_with_cache`(二进制) | `10604` 缺 path;`10605` 非法路径(400,旧 403);`20601` 文件不存在 |
| 8 | `POST /algorithms/versions:batch-download` | `batch_download`(body `{ids,type}`) | `send_file_with_cache` ZIP(二进制) | `10606` 请选择版本;`10607` 版本不存在(400);`20602` 没有可下载的文件;`40600` 打包失败(500) |

### `app/api/v1/event_types.py`(蓝图 `api_v1_event_types`,FF=07)

| # | 方法 + 路径 | 旧视图 | 成功响应 | 错误码 |
|---|---|---|---|---|
| 9 | `GET /event-types` | `list_event_types` | `paginated(rows, …)`(`tags` 已 json.loads) | — |
| 10 | `POST /event-types` | `create_event_type` | `created({id,key}, location=/api/v1/event-types/{id})` + `_sync` | `10700` 标识空;`10701` 中文名空;`10702` 标识格式非法;`10703` 标签非数组;`10704` ID 非整数;`30700` 标识已存在(409);`30701` ID 已存在(409) |
| 11 | `PATCH /event-types/<id>` | `update_event_type` | `ok({id})` + `_sync` | `20700` 不存在;`10705` 字段格式错误;`10703` 标签非数组;`10706` 没有要更新的字段 |
| 12 | `GET /event-types/<id>/references` | `get_event_type_references` | `ok({key,total,refs})` | `20700` 不存在 |
| 13 | `DELETE /event-types/<id>` | `delete_event_type` | `no_content()`(204) + `_sync` | `20700` 不存在;`30702` 有 N 处引用无法删除(409) |

> 路径前缀均为 `/api/v1`。RPC 动作用 `:action` 后缀(`:batch-download`),子资源用名词(`references`)。

## 4. 请求方式(verb)分析

逐端点比对旧→新 HTTP 方法(流程要求:每个端点给动词理由,尤其 PATCH vs PUT):

| 旧端点 | 旧方法 | 新方法 | 变化 | 理由 |
|---|---|---|---|---|
| `list_types` | GET | GET | 不变 | 幂等读取 |
| `list_versions` | GET | GET | 不变 | 幂等列表 |
| `create_version` | POST | POST | 不变 | 创建(含文件上传,multipart) |
| `version_detail` | GET | GET | 不变 | 读取(URL 去 `/detail`,**方法不变**) |
| `update_version` | PATCH | PATCH | 不变 | 部分更新——旧已 PATCH(非 PUT),无需像 alerts 把单字段 PUT 修 PATCH |
| `delete_version` | DELETE | DELETE | 不变 | 删除(响应 200→204,方法不变) |
| `download_file` | GET | GET | 不变 | 幂等下载——旧已 GET;alerts 的 download POST→GET 修正在此**不适用** |
| `batch_download` | POST | POST | 不变 | RPC 动作 + 带 body `{ids,type}`;GET 带 body 是反模式 |
| `list_event_types` | GET | GET | 不变 | 幂等列表 |
| `create_event_type` | POST | POST | 不变 | 创建 |
| `update_event_type` | PATCH | PATCH | 不变 | 部分更新——旧已 PATCH |
| `get_event_type_references` | GET | GET | 不变 | 幂等读取引用计数 |
| `delete_event_type` | DELETE | DELETE | 不变 | 删除(响应 200→204) |

**结论:本轮无动词改动。** 旧端点动词均已 REST 正确,没有「明显错误动词」可修——这与 alerts(download POST→GET、单字段 PUT→PATCH)不同。

**请求体格式**:`create/update_version` 保留 **multipart form**(`request.form` + `request.files`),因含 config/algo 文件上传,不能改 JSON;其余端点保留 JSON body。

## 5. 错误码(5 位 `H FF SS`,详见 `docs/rest-api-error-codes.md`)

本模块直接用新码(FF=06/07 在分配表已预留,本轮启用)。

### FF=06 algorithm-versions

| 码 | 含义 | HTTP | 触发场景 |
|---|---|---|---|
| `10600` | 算法类型无效 | 400 | `create/update_version` 的 `algorithm_type` 不在 `get_event_types()` |
| `10601` | 算法名不能为空 | 400 | `create_version` 缺 name |
| `10602` | 算法日期不能为空 | 400 | `create_version` 缺 version_date |
| `10603` | 没有要更新的字段 | 400 | `update_version` 一个字段都没提供(修 description 怪癖后可达) |
| `10604` | 缺少 path 参数 | 400 | `download` 无 `?path=` |
| `10605` | 非法路径 | 400 | `download` 的 path 经 `relative_to(upload_dir)` 失败(防穿越,旧 403) |
| `10606` | 请选择要下载的版本 | 400 | `batch_download` 的 `ids` 为空 |
| `10607` | 选中的版本不存在 | 400 | `batch_download` 查不到 ids(旧 400,保留) |
| `20600` | 算法版本不存在 | 404 | `update/delete/get_version` 找不到 id |
| `20601` | 文件不存在 | 404 | `download` path 在 uploads 内但文件不在 |
| `20602` | 没有可下载的文件 | 404 | `batch_download` 打包后 added=0 |
| `30600` | 有 N 个数据集使用无法删除 | 409 | `delete_version` 检测到 `dataset_algorithm_versions … is_active=1`(旧 400) |
| `40600` | 打包失败 | 500 | `batch_download` 打包异常 |

### FF=07 event-types

| 码 | 含义 | HTTP | 触发场景 |
|---|---|---|---|
| `10700` | 英文标识不能为空 | 400 | `create` 缺 key |
| `10701` | 中文名不能为空 | 400 | `create` 缺 name |
| `10702` | 英文标识格式非法 | 400 | `create` 的 key 含非字母/数字/下划线 |
| `10703` | 标签必须是数组 | 400 | `create/update` 的 tags 不是 list |
| `10704` | ID 必须是整数 | 400 | `create` 显式 id 非整数(含 TypeError 兜底) |
| `10705` | 字段格式错误 | 400 | `update` 的 sort_order 等转换失败 |
| `10706` | 没有要更新的字段 | 400 | `update` 空更新 |
| `20700` | 事件类型不存在 | 404 | `update/references/delete` 找不到 id |
| `30700` | 英文标识已存在 | 409 | `create` 的 key 已占用(旧 409,保留) |
| `30701` | ID 已存在 | 409 | `create` 显式 id 已占用(旧 409,保留) |
| `30702` | 有 N 处引用无法删除 | 409 | `delete` 检测到 5 表有引用(旧 400) |

## 6. 关键设计:原位重写 + 语义修正

### 原位重写(非委托)

旧逻辑纯同步 CRUD + 文件 I/O,无后台线程/锁/进程(OCR 高风险区才委托)→ 照 alerts/videos 原位重写 handler,复用旧模块的纯函数:

- `app.event_types.get_event_types`(算法类型校验 + types 列表)
- `app.event_types._sync_alert_types_json`(事件类型 create/update/delete 后同步 `config/alert_types.json`)
- `app.routes.send_file_with_cache`(download / batch-download 二进制响应)
- `app.services.config_parser.parse_config`(`version_detail` 懒导入,无 config_file → None)
- `werkzeug.utils.secure_filename`(文件上传命名)

二进制响应(download / batch-download)**不走信封**,错误仍 `raise ApiError`(errorhandler 套信封)。

### 经用户确认的语义修正(新端点专属,旧端点不变)

5 位码要求 **H 位与 http_status 严格对应**,规范无 403 的 H、冲突按设计归 409(H=3),故:

- **DELETE→204**(`delete_version`/`delete_event_type`,对齐 alerts/videos 既有 DELETE 模式,旧返 `{ok:true}` 200)
- **冲突 400→409**:`30600`(算法版本被引用)/`30702`(事件类型有引用)
- **非法路径 403→400**:`10605`(规范无 403 的 H;`download` 防穿越拒绝)
- **`version_detail` 去 `/detail` 后缀**:资源 GET 即详情(旧 `/versions/<id>/detail` 保留并弃用)
- **`update_version` description 怪癖修复**:见 §7 坑2

`create_event_type` 的 `id` 缺省 `COALESCE(MAX(id),0)+1` 或显式(校验整数+唯一)——原样保留(event_types 表 `id INTEGER PRIMARY KEY` 无 AUTOINCREMENT,id 有业务含义,被 alert 文件名与 `config/alert_types.json` 引用)。

### 为什么冲突 400→409、非法路径 403→400

5 位错误码 `H FF SS` 的首位 **H 必须与 http_status 对应**(H=1↔400/405、H=2↔404、H=3↔409、H=4↔500、H=5↔500/202),这是方案「看码知语义」的基石——读码首位即知 HTTP 类。旧端点的 400/403 塞不进这个对应关系,故新端点修正(旧端点不动):

- **冲突 400→409(`30600`/`30702`)**:「被引用/有引用,无法删除」的请求本身合法(`version_id` 存在、参数对),拒绝源于资源**当前状态**——这是 HTTP 语义上的 **409 Conflict**,不是 400 Bad Request(400 是「请求坏/参数非法」)。放进 5 位方案:若保留 400,H 须取 1,码落入族内**参数段(00-19)**,把冲突误归类成参数错误,违背「族内同类聚拢」;改 409 则 H=3 落入**冲突段(40-59)**,码与 status 自洽。
- **非法路径 403→400(`10605`)**:方案 H 表**无 403 这一档**(只有 400/405、404、409、500、500/202),组不出 H 位与 status 一致的 5 位码。重新归类:非法 `path` 参数属客户端坏输入 → **400 Bad Request**(H=1,族内参数段)。403/400 对「非法路径」都说得通(403=禁止、400=坏请求),但方案只能选 400(无 403 的 H)。

**本质**:若保留旧 status,冲突码会被迫取 H=1(码首位谎称参数错误)、403 则无 H 可用——都会让码与 status 矛盾,破坏方案自洽。改后二者自洽,且这些都是**新端点**(旧端点原样保留并加弃用 header),不影响现有前端。该选择经用户 AskUserQuestion 确认(「按错误码规范修正(409/400)」)。

## 7. 实现中踩的坑(根因 + 修复)

### 坑1:`ALERT_TYPES_CONFIG_PATH` 必 patch(类比 OCR 的 DATABASE_PATH 双绑定)

- **现象**:`create/update/delete_event_type` 末尾调 `_sync_alert_types_json()`,它写到 `app.event_types` 模块级 `ALERT_TYPES_CONFIG_PATH`(= 真实 `config/alert_types.json`)。不 patch → 测试改写仓库配置,违反「恢复原状」。
- **根因**:`_sync` 用模块级常量定位写文件,与 `app.event_types` 命名空间绑定,不随 `database.DATABASE_PATH` 变。
- **修复**:`tests/conftest.py` 的 `app` fixture 加 `monkeypatch.setattr("app.event_types.ALERT_TYPES_CONFIG_PATH", tmp_path / "alert_types.json")`。`_sync` 内部 `_load_event_types_from_db()`→`get_db()`(请求上下文 tmp 库)读已提交数据再写 tmp 文件。
- **注意区分**:`init_db`→`_seed_event_types`(database.py)只**读** `config/alert_types.json`(line 37 硬编码路径,不读 `ALERT_TYPES_CONFIG_PATH`),故播种不写、不污染;`app.event_types.DATABASE_PATH`(直连兜底,line 97)无需 patch——端点内走 `get_db()`(请求上下文),兜底分支不触发。

### 坑2:`update_version` 的 description 怪癖(已修)

- **现象**:旧 `update_version` 用 `description = request.form.get("description", "").strip()` + `if description is not None:`。默认 `""` 使 `is not None` **永真** → 每次 PATCH 都重写 description(即便不传也置 `""`,会误清空)→ `10603` 经 multipart **不可达**。
- **根因**:multipart 表单用 `.get("field", "")` 默认值丢失了「字段是否提供」的存在性语义;旧版 `is not None` 判断被默认 `""` 击穿。
- **修复**(用户授权):description 改 `if "description" in request.form` 存在性检测(未传不动、传空显式清空),`10603` 重新可达。`name`/`version_date`/`algorithm_type` 维持 truthy 语义(非空才更新——它们 NOT NULL,不应被空串清空)。测试 `test_update_version_description_not_blanked`(PATCH 只传 name → description 不被清空,旧代码上会失败)+ `test_update_version_no_fields`(空表单→10603)锁定。**只改新 v1 端点,旧 `app/routes/algorithms.py` 不动**。
- **对比**:event_types 用 JSON `if field in data` 存在性检测,`10706` 本就可达。

### 坑3:手动测试 PowerShell 5.1 + curl.exe 的引号/编码坑

- **现象**:`curl.exe -d '{"key":"rat","name":"鼠"}'` 在 PS 5.1 里内嵌双引号被嚼坏 → curl 发非法 JSON → 服务端 `request.get_json()` 失败 → `code:1000`(Werkzeug 400,在端点之前拦截,**不会写入**)。
- **根因**:Windows PowerShell 5.1 给 native exe 传带内嵌双引号的参数有已知缺陷。
- **修复**:
  - 带 JSON body 的 POST/PATCH 用 `Invoke-RestMethod -Body ([System.Text.Encoding]::UTF8.GetBytes($json))`(自带引号处理 + UTF-8 + 响应自动解码)。
  - 纯 ASCII body 用 `curl.exe --% ... -d "{\"key\":\"x\"}"`(`--%` 停止 PS 解析,`\"` 由 curl 还原)。
- **教训**:终端 curl 测试受 shell 引号/编码干扰大,**以 pytest 为准**(test client 不受影响),curl 仅作信封/弃用 header 的肉眼核对。

## 8. 测试方案

### 用例(36 个,全快测无 slow)

`tests/test_api_v1_algorithms.py`(20):types 列表非空;create(multipart+algorithm_file)→201;create 类型无效→10600;create 缺名→10601;create 缺日期→10602;list 含新建+datasets 字段;detail(config_info=None+algorithm_file_path);detail 不存在→20600;patch name→ok+读回;patch 不存在→20600;**patch 空更新→10603**;**patch 只传 name→description 不被清空**;delete→204+读回404;delete 被引用→30600(409);download→200 二进制;download 缺 path→10604;download 非法路径→10605(400);download 不存在→20601;batch-download→200 zip(PK 头);batch-download 空 ids→10606。

`tests/test_api_v1_event_types.py`(16):list 非空(播种~17)+tags 数组;create 新 key→201;create 重复 key(rat)→30700(409);create 重复 id→30701(409);create 缺 key→10700;create 缺 name→10701;create 非法 key→10702;create tags 非数组→10703;patch name+bg_color→ok+读回;patch 不存在→20700;patch 空更新→10706;references(建版本引用后 total>=1)+不存在→20700;delete→204+读回无;delete 不存在→20700;delete 有引用→30702(409)。

### 隔离

- DB:conftest `app` fixture 双 patch `DATABASE_PATH` + 本轮新增 `ALERT_TYPES_CONFIG_PATH`→tmp;`create_app`→`init_db` 建全 schema + `_seed_event_types` 播种 ~17 条(故 `algorithm_type="rat"` 合法、重复 key/id 可测)。每用例 fresh tmp DB,互不污染。
- multipart:`data={表单字段 + (BytesIO, filename) 元组}` + `content_type="multipart/form-data"`。
- 无 EasyOCR / 无后台线程 → 无 slow marker、无 `_reset_ocr_progress`(OCR 专用,不影响本轮)。

## 9. 测试结果

| 命令 | 结果 |
|---|---|
| `py -m pytest tests/test_api_v1_algorithms.py` | **20 passed** |
| `py -m pytest tests/test_api_v1_event_types.py` | **16 passed** |
| 两文件合跑 | **36 passed** |
| 全套 v1 回归(algorithms+event_types+alerts+videos+errors,不含 OCR) | **56 passed** |
| 含 OCR 全套 v1(4 slow 真跑 EasyOCR ~13s) | **65 passed** |
| 抽查修复点(description_not_blanked + no_fields + dup_key) | **3 passed** |
| 隔离验证 `git status --short config benchmark.db` | 空(真实配置/库未被触碰) |

### 手动 curl 验证(live 服务,真实 `benchmark.db`)

| 路径 | 结果 |
|---|---|
| `GET /api/v1/event-types?page_size=100` | ✅ `code:0`,分页 `{items,total,page,page_size,has_next}`,tags 已解析;真实库 19 条(17 播种 + fight 117 + manual_test 118) |
| `POST /api/v1/event-types` key=rat(重复) | ✅ `409 / code:30700`,`英文标识 'rat' 已存在` |
| `POST /api/v1/event-types` key=manual_test(curl `--%`) | ✅ `201 / created`,id=118 落库 |
| `PATCH /api/v1/event-types/118` 改名(Invoke-RestMethod) | ✅ `200 / code:0 / data:{id:118}`,name 变「改名后」 |
| `GET /algorithms/api/versions`(旧端点) | ✅ 响应头含 `Deprecation: true`(+ `Link`) |

> 运行环境:`py` 启动器(Python 3.13.14, pytest 9.1.1);`python` 是 Windows Store stub(exit 49 无输出)勿用。live 服务 `py run.py`(Flask/Waitress 0.0.0.0:8080);**改代码后须重启 run.py** 否则新蓝图不进路由表(表现为 `/api/v1/...` 返 `code:2004` 资源不存在)。

## 10. 状态

- **未 git commit**(按 CLAUDE.md 规则,等用户授权)。连同前几轮累积的 `app/api/` 整目录均未上库。
- 下一个模块:**config**(仍走 plan mode → 批准 → 实现)。后续顺序见项目记忆 `rest-api-migration.md`。
