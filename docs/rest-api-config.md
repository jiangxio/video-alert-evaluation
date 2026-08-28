# /api/v1 config 改造文档

> ⚠️ **错误码已改为方案3**（HTTP 状态即 `code` + 可选 `error_code` 字符串）。下方 5 位 `H FF SS` 码列已废弃，以代码实际行为为准；完整规范见 [错误码文档](./rest-api-error-codes.md)。

> REST API 改造第 6 模块。把 `app/routes/api_config.py` 旧蓝图里的 3 个内联 JSON 端点资源化进 `/api/v1/config`,统一信封 + 5 位错误码(FF=08 config)。旧逻辑为**同步 DB + 文件 I/O**(密钥写 `.env`、非敏感项写 `api_config` 表),无后台线程/锁/进程 → **原位重写 handler**(与 alerts/videos/algorithms 一致),复用 `app.services.api_config_service` 的纯函数 `get_config_for_display` / `save_config` / `test_openai` / `test_claude`,不重复实现。旧端点保留并自动加弃用 header。config 是**单例资源**(`api_config` 表 `CHECK(id=1)`,全局唯一)。

## 1. 背景

旧 `api_config` 蓝图(`/api-config`)把「统一 API Token 配置页」与 3 个 JSON 端点混在一个蓝图:页面路由 `GET /api-config/` 渲染 `api_config.html`;三个 JSON 端点返回裸 JSON + HTTP 码(`{success, config}` / `{ok, msg}`),无统一信封、无结构化错误码。本模块只迁移 3 个 JSON 端点,页面路由不在范围。新旧并行:旧端点保留并自动加弃用 header(`Deprecation: true` + `Link`,deprecation.py 的 `/api-config/api/` → `/api/v1/config` 已就绪),前端继续用旧 URL。

范围:**URL 资源化 + 正确 HTTP 动词(save POST→PATCH)+ 统一信封 + 5 位错误码**,不改交互语义。

## 2. 改动文件清单

| 文件 | 类型 | 作用 |
|---|---|---|
| `app/api/v1/config.py` | 新建 | 3 端点(FF=08):config 单例 GET/PATCH + :test-connection |
| `app/api/v1/__init__.py` | 改 | `BLUEPRINTS` 加 `config.bp`,`import` 加 `config` |
| `tests/conftest.py` | 改 | `app` fixture patch `app.services.api_config_service.ENV_PATH`→tmp |
| `tests/test_api_v1_config.py` | 新建 | 11 用例(全快测无 slow) |

**未触碰**:`app/routes/api_config.py` 旧端点(新旧并行)、`app/services/api_config_service.py` 服务代码(只 import 其函数)、`/api-config/` 页面路由(HTML,不在迁移范围)、`scripts/`。

## 3. 端点详情(3 个)

### `app/api/v1/config.py`(蓝图 `api_v1_config`,url_prefix `/api/v1`,FF=08)

| # | 方法 + 路径 | 旧视图 | 成功响应 | 错误码(5 位) |
|---|---|---|---|---|
| 1 | `GET /config` | `api_get_config` | `ok(get_config_for_display())`(密钥不返回,仅 `*_key_configured` 标记) | — |
| 2 | `PATCH /config` | `api_save` | `ok(save_config(data))`(部分更新,返回当前配置) | `40880` 保存失败(500) |
| 3 | `POST /config:test-connection` | `api_test` | `ok(result)`(`{ok, msg}`,连接失败也是 200) | `10800` 未知的 provider(400) |

> 路径前缀 `/api/v1`。单例资源用名词根 `/config`;测试连接是 RPC 动作,用 `:test-connection` 后缀(旧 `/api/test` 的动作语义保留,URL 资源化)。

## 4. 请求方式(verb)分析

逐端点比对旧→新 HTTP 方法(流程要求:每个端点给动词理由,尤其 PATCH vs PUT):

| 旧端点 | 旧方法 | 新方法 | 变化 | 理由 |
|---|---|---|---|---|
| `api_get_config` | GET | GET | 不变 | 幂等读取 |
| `api_save` | **POST** | **PATCH** | **改** | save_config 是**部分更新**:只改 body 中出现的字段(密钥空串/缺失=不改、未传字段不动);PUT 的整体替换语义与之矛盾。旧版用 POST 是「动作」式写法,REST 化后归 PATCH |
| `api_test` | POST | POST | 不变(加 `:test-connection` 后缀) | RPC 动作 + 带 body `{provider}`;GET 带 body 是反模式。动词不变,URL 从 `/api/test` 资源化为 `:test-connection` |

**结论:本轮有 1 处动词改动(save POST→PATCH)**,与 algorithms/event_types(无动词改动)不同,与 alerts(单字段 PUT→PATCH)同类:都是把旧版「POST 当动作」改为符合 REST 的部分更新动词。

**请求体格式**:三个端点均用 **JSON body**(`request.get_json() or {}`),无文件上传,不走 multipart。

## 5. 错误码(5 位 `H FF SS`,详见 `docs/rest-api-error-codes.md`)

本模块直接用新码(FF=08 在分配表已预留,本轮启用)。目前仅用 2 个,余码位预留:

| 码 | 含义 | HTTP | 触发场景 |
|---|---|---|---|
| `10800` | 未知的 provider | 400 | `:test-connection` 的 `provider` 非 `openai`/`claude` 或缺失(均落入 else 分支) |
| `40880` | 保存失败 | 500 | `save_config` 抛异常(如 `_write_env` 写 `.env` 失败) |

> H 位与 http_status 严格对应:`10800` H=1↔400(客户端坏输入)、`40880` H=4↔500(服务端/内部错误)。

**连接失败不是 HTTP 错误**:`test_openai`/`test_claude` 返回 `{ok: False, msg: "连接失败:…"}` 时,端点仍 `200 + ok(result)`——连接失败是**测试结果**,只有 provider 未知/缺失才是 `10800/400`。这与旧版语义一致(旧版未知 provider 才 400,连接失败也走 200)。

## 6. 关键设计:原位重写 + 单例资源

### 原位重写(非委托)

旧逻辑纯同步 DB + 文件 I/O(`save_config`→`_write_env` 原子写 `.env` + `_save_db_config` upsert + `_update_key_configured_flags`),无后台线程/锁/进程(OCR 高风险区才委托)→ 照 alerts/videos/algorithms 原位重写 handler,直接调 `api_config_service` 的纯函数,不抽不改旧逻辑:

- `get_config_for_display()`(GET,密钥脱敏,返回 `*_key_configured` 标记)
- `save_config(data)`(PATCH,密钥写 `.env`、非敏感项写 DB、同步标记)
- `test_openai()`/`test_claude()`(POST,发最小请求验连通性,返回 `{ok, msg}`)

### 单例资源 + PATCH + RPC 后缀

- **单例资源**:`api_config` 表 `CHECK(id=1)`,全局唯一 → 用名词根 `/config`(无 `/config/<id>`);GET 即取唯一行,PATCH 即改唯一行。
- **save 用 PATCH 不用 PUT**:`save_config` 的部分更新语义(存在性检测 + 密钥空串=不改 + 未传字段不动)与 PUT 的整体替换矛盾。空体 `{}` 不写 `.env`、不 UPDATE 字段,但仍建空行 + 同步标记 + 返回当前配置(与旧版一致)。
- **:test-connection 后缀**:测试连接是 RPC 动作(对 config 资源执行「测连通」动作),用 `:action` 后缀,与 alerts_ocr 的 `:ocr:batch`/`:ocr-status:cancel` 同规。

### 保留 save 失败的具体 message

新端点用 `try: save_config(data) except Exception as e: raise ApiError(40880, f"保存失败：{e}", 500)`。若不 try/except,`save_config` 抛出的异常会被 app 级 errorhandler 吞成通用错误码,丢失「保存失败:xxx」的具体原因;显式 catch→`ApiError(40880, …)` 保留旧版 message(旧版 `{success: False, error: "保存失败：…"}`,500)。

### 旧端点弃用映射

`app/api/v1/deprecation.py:23` 已预置 `/api-config/api/` → `/api/v1/config` 的 successor 映射,三个旧 JSON 端点(`/api-config/api/config`、`/save`、`/test`)的响应自动加 `Deprecation: true` + `Link` header。`/api-config/` 页面路由不在迁移范围(HTML 页,不加弃用标记)。

## 7. 实现中踩的坑(根因 + 修复)

### 坑1:`ENV_PATH` 必 patch(类比 OCR 的 `DATABASE_PATH` 双绑定、event_types 的 `ALERT_TYPES_CONFIG_PATH`)

- **现象**:`save_config`→`_write_env` 写 `app.services.api_config_service` 模块级 `ENV_PATH`(= 真实 `.env`)。不 patch → save 测试改写仓库 `.env`,违反「恢复原状」。
- **根因**:`ENV_PATH = BASE_DIR / '.env'` 在导入期绑定到 service 模块命名空间,不随 `database.DATABASE_PATH` 或 `Config` 属性变;`_write_env`/`load_dotenv` 均读这个模块级常量。
- **修复**:`tests/conftest.py` 的 `app` fixture 加 `monkeypatch.setattr("app.services.api_config_service.ENV_PATH", tmp_path / ".env")`。`_write_env` 写 tmp 文件,`load_dotenv` 读 tmp 文件注入 `os.environ`。
- **副作用隔离(patch 文件挡不住 `os.environ`)**:patch `ENV_PATH`→tmp 只挡住 `_write_env` 写「哪个文件」;`save_config` 紧接着 `load_dotenv(ENV_PATH, override=True)`(`api_config_service.py:130`)会把 `.env` 内容**直接注入进程级 `os.environ`**。`os.environ` 是跨用例共享、且无自动回滚的全局状态(不像 `tmp_path` 用完即弃、不像 `monkeypatch` 自动还原),`load_dotenv` 绕过 monkeypatch 直写,注入即赖着不走。
  - 受污染的是 `is_openai_configured()`/`is_claude_configured()`(`:80-85`,读 `os.environ`)。
  - 具体失败链:用例 `test_update_config_writes_env_key` 传 `openai_api_key=sk-test-123` → `_write_env` 写 tmp `.env` → `load_dotenv` 注入 `os.environ["OPENAI_API_KEY"]=sk-test-123`;下一条用例 `test_update_config_returns_config` 断言 `openai_key_configured is False`(未传密钥),却读到残留 key → `True` → 断言失败。`test_get_config_defaults` 在重跑/乱序时同理被击穿。
  - 修复=config 测试模块 autouse `_isolate_env_keys` fixture 双向堵洞:**setup** `monkeypatch.delenv` 清 6 个键(`OPENAI_API_KEY`/`OPENAI_BASE_URL`/`OPENAI_MODEL`/`ANTHROPIC_AUTH_TOKEN`/`ANTHROPIC_BASE_URL`/`ANTHROPIC_MODEL`)→ 每用例从干净基线起步,既免疫上一用例残留、也免疫开发者本机真实 `.env`/shell 的 key(否则有真实 key 的开发机上 `test_get_config_defaults` 永失败);**teardown** monkeypatch 按原始值还原→抹掉 `load_dotenv` 注入、还原开发者真实 key。
  - 一句话:patch `ENV_PATH` 隔离「写哪个文件」,`_isolate_env_keys` 隔离 `load_dotenv` 把文件内容回灌进进程全局 `os.environ` 的二次副作用。前者挡文件 I/O,后者挡进程级环境变量,缺一不可。
- **注意区分(别把 OCR 的「双绑定 patch」套到 config 的 DB 上)**:OCR 坑2 是 `app/routes/alerts.py` 顶部 `from app.database import DATABASE_PATH`——导入期拷贝,与 `database.DATABASE_PATH` 成两个名字,后台线程读陈旧拷贝,须单独 patch `app.routes.alerts.DATABASE_PATH`。别下意识给 `api_config_service` 也补 DB patch,config 不需要,两层原因:
  - **service 无 module 级 DB 绑定,走 `get_db()` 间接**:`api_config_service` 顶部没有 `from app.database import DATABASE_PATH`,访问库全经 `get_db()`(`_load_db_config` `:186`、`_save_db_config` `:217`、`_update_key_configured_flags` `:254`)。`get_db()` 运行时读 `app.database.DATABASE_PATH`(非导入期拷贝),conftest 已 patch 该常量→tmp(`conftest.py:74`),故 `get_db()` 返回 tmp 库——单 patch 自动覆盖 service,无需额外绑定。
  - **硬编码直连是死分支,测试够不着**:`_load_db_config` 的 `except RuntimeError` 兜底(`:195-209`)直连 `sqlite3.connect(str(BASE_DIR / 'benchmark.db'))`(硬编码真实库)。该分支只在「无应用上下文」(`get_db()` 抛 `RuntimeError`)触发,即后台线程。config 测试全在 Flask test client 请求上下文内(app context 已 push),`get_db()` 正常返回→兜底不执行→硬编码路径碰不到→不用 patch。
  - 对比:OCR 的 `ocr_batch` 跑后台 daemon 线程(无 app context)→ 落入该兜底→写真实 `benchmark.db`,故 OCR 必须双 patch;config 无后台线程,同一兜底对它是死代码。模式相似、触发条件不同,不能照抄。

### 坑2:test-connection 须 mock(外部 LLM API 不可控)

- **现象**:`test_openai`/`test_claude` 真发 OpenAI/Anthropic API,CI 环境无凭据/无网络/计费不可控。
- **修复**:`test_test_connection_openai`/`test_test_connection_claude_failure` 用 `monkeypatch.setattr(api_config_service, "test_openai"/"test_claude", lambda: {…})` mock 返回值,只验端点的信封分流(200 + `{ok, msg}`)与 provider 校验(10800/400)。get/save 则**真走 service**(本地 DB + 文件 I/O,可控,不 mock)。

## 8. 测试方案

### 用例(11 个,全快测无 slow)

`tests/test_api_v1_config.py`,分三组:

- **获取配置(2)**:`test_get_config_defaults`(未保存时返回默认值,env 已清空+无 DB 行,断言 `openai_base_url` 默认 `https://api.openai.com/v1`、`openai_model` 默认 `gpt-4o-mini`、`*_key_configured` 为 False);`test_get_config_envelope`(信封 `code:0/message:"ok"/data` 存在)。
- **保存配置 PATCH(5)**:`test_update_config_partial`(PATCH 只传 `openai_base_url`,上次设的 `openai_model` 不被清空);`test_update_config_returns_config`(返回含 `openai_model`+`openai_key_configured:False`);`test_update_config_writes_env_key`(传 `openai_api_key`→写入 tmp `.env` 且 `key_configured:True`);`test_update_config_blank_key_no_change`(先写 key 再传空串,`.env` 不被清空);`test_update_config_failure`(monkeypatch `_write_env` 抛 `OSError("disk full")`→500/40880 + message 含「保存失败」)。
- **测试连接(4)**:`test_test_connection_unknown_provider`(provider="xxx"→400/10800);`test_test_connection_missing_provider`(无 provider→400/10800);`test_test_connection_openai`(mock `test_openai`→200 + `ok:True`);`test_test_connection_claude_failure`(mock `test_claude` 返回失败→200 + `ok:False`,验证连接失败非 HTTP 错误)。

### 隔离

- **DB**:conftest `app` fixture 已 patch `DATABASE_PATH`→tmp;`create_app`→`init_db` 建全 schema 含 `api_config` 表,但**只建表不预插行**——首行由 `save_config`→`_save_db_config` 的 `INSERT OR IGNORE` 创建,故 `test_get_config_defaults` 能测「无 DB 行」分支。
- **.env**:本轮新增 `ENV_PATH`→tmp patch(坑1);autouse `_isolate_env_keys` 清空+恢复 6 个凭据键(隔离 `load_dotenv` 副作用)。
- **test-connection**:monkeypatch mock `test_openai`/`test_claude`(坑2)。
- 无 EasyOCR / 无后台线程 → 无 slow marker、无 `_reset_ocr_progress`。

## 9. 测试结果

| 命令 | 结果 |
|---|---|
| `py -m pytest tests/test_api_v1_config.py -q` | **11 passed in 4.72s** |
| 全套 v1 回归 `py -m pytest tests/ -k "api_v1" -q` | **76 passed, 52 deselected, 64 warnings in 68.40s** |
| 隔离验证 `git status --short .env benchmark.db` | 空(真实 `.env`/库未被触碰) |

> - 76 passed = 前几轮 65(videos/alerts/OCR/algorithms/event_types)+ 本轮 config 11,无失败、无回归。
> - 52 deselected:`-k "api_v1"` 过滤掉的非 v1 测试(含 `test_eval_service.py` 既有失败,属 bug-audit 范畴,与本次无关)。
> - 64 warnings:`sqlite3` 默认 timestamp converter 的 `DeprecationWarning`(Python 3.12+),既有、纯提示,非本次引入。
> - 运行环境:`py` 启动器(Python 3.13.14, pytest 9.1.1);`python` 是 Windows Store stub(exit 49 无输出)勿用。

## 10. 状态

- **未 git commit**(按 CLAUDE.md 规则,等用户授权)。连同前几轮累积的 `app/api/` 整目录均未上库。
- **配套待更新**:`docs/rest-api-error-codes.md` 的 FF 分配表第 37 行 `| 08 | config | 待做 |` 仍标「待做」,本轮已启用 `10800`/`40880` 两个码,应改为「已用(10800/40880)」——此为遗留的小一致性问题,可单独修。
- **项目记忆待更新**:`rest-api-migration.md` 快照仍停在 2026-08-18 的「下一步 config」,本轮 config 已完成,应标记 ✅ 并把下一步改为 `streaming/tasks`。
- 下一个模块:**streaming/tasks**(异步 `:start`/`:stop`,Windows PID 存活检测 + MediaMTX/ffmpeg 进程管理,属高风险区,可能需委托旧视图)。仍走 plan mode → 批准 → 实现。后续顺序见项目记忆 `rest-api-migration.md`。
