# sxs.txt REST API 改造：可行性评估与全量测试方案

> 评估对象：`sxs.txt` 的「新建 `/api/v1/*` 并行 REST API」计划。
> 评估依据：实地核查 `app/__init__.py`、`app/routes/*.py`、`tests/conftest.py` 等现状。
> 结论先行：**技术上可做，本轮（基础设施 + videos 模块）风险可控；全量 11 模块是多个会话的工程，真正的成本不在基础设施，而在分页端点的重实现与 `wrap_old_view` 的响应分类。**

---

## 一、现状核查（决定可行性的硬事实）

| 核查项 | 现状 | 对计划的影响 |
|--------|------|--------------|
| `create_app` | 标准工厂，11 个蓝图显式注册，内联 `/` 首页 + `413` handler，**无全局 404/500 handler** | ✅ 计划的 app 级 errorhandler 是纯增量，无冲突 |
| `/api` 端点规模 | **164 个** `@bp.route`，**475 处 jsonify**，11 蓝图（videos 44 / evaluation 43 / alerts 31 / algorithms 16 / auto_annotation 14 / streaming 12 / assistant 11 / review 5 / extract 5 / verification 4 / api_config 4） | ⚠️ 全量 = 大工程；本轮只动 videos（44 个） |
| `videos.py` 返回形态 | `render_template`（3 个页面路由，非 `/api`）、`jsonify`/`(jsonify, status)` 元组（~40）、`send_file_with_cache` 二进制（7 处）、异步任务返回 jsonify（watermark-tasks/concat/package/trim） | ⚠️ `wrap_old_view` 必须正确分类这 4 类返回 |
| `tests/conftest.py` | **已有 `app_client` / `app_ctx` / `db_conn` 三个 fixture** | ✅ 计划里"加 app/client fixture"**已经做完**，测试设置成本被高估 |
| 现有用例 | **52 个**，分布 assistant 9 / core_anomalies 9 / eval_service 16 / metric_anomalies 5 / utils 13 | ✅ 回归基线明确 |
| `run.py` | waitress :8080，16 线程 | ✅ 端到端验证可行 |

---

## 二、可行性分维度评估

### 2.1 基础设施（本轮前半）—— 可做，低风险

`app/api/__init__.py` 的 `register_api(app)` 挂在 `create_app()` 末尾即可；`responses.py`/`errors.py`/`deprecation.py`/`compat.py` 都是标准 Flask 模式。**不碰 `app/routes/*.py`**，回归面极小。唯一要注意：

- `deprecation` 的 `after_request` 钩子**对每个响应都跑**（含二进制下载、页面 HTML）——只能"加 header"，绝不能改 body，且要按 path 前缀精确跳过非旧 API 路径。
- app 级 `errorhandler(404/500)` 按 `request.path.startswith('/api/v1/')` 分流——必须保留现有 `413` handler 和页面 404 的原行为。

### 2.2 `wrap_old_view`（计划核心机制）—— 可做，但要认清边界

包装器要处理的返回形态矩阵（已在 `videos.py` 核实存在）：

| 旧视图返回 | 包装后 | 难点 |
|-----------|--------|------|
| `jsonify({...})` / `(jsonify, 200)` | `{code:0, data:{...}}` | 直接套信封 |
| `(jsonify({'error':...}), 4xx/5xx)` | 错误信封 `{code:status, message:...}` | 需解析旧 body 里的 `error` 字段 |
| `jsonify([...])` 裸列表 | `{code:0, data:{items:[...]}}` | **分页字段 total/has_next 无法靠包装补** |
| `send_file_with_cache` | **原样透传**（二进制不走信封） | 靠 `response.mimetype` 判定 |
| `redirect(...)` / `render_template` | 原样透传 | 仅 `/api/*` 才包装，页面路由天然不进 |

**关键结论：分页补不回来。** 旧的 `/api/all`、`/api/search`、`/api/watermarked`、`/api/watermark-tasks`、`/api/eval-sets`(GET) 这类**列表端点，不能用包装，必须重实现**（带 `LIMIT/OFFSET` 重查）。videos 模块里这类至少 5 个。计划自己也写了"简单 CRUD 抽 service、高风险异步才包装"，但没点明**列表/分页端点必须走重实现路线**——这是真实工时所在。

### 2.3 一个计划内部矛盾（必须先澄清）

计划同时说"不动 `app/routes/*.py`"又说"简单 CRUD 抽 operation 到 `app/services/` 新旧共用"。这两条互斥：抽 service 意味着改旧视图去调新函数。两种自洽选法：

- **A. 不改旧视图、新端点复制查询逻辑**：零回归风险，代价是查询逻辑重复一份。
- **B. 抽 service、旧视图改调 service**：无重复，但动了旧视图，回归风险上升。

**建议选 A**（与"参赛收尾、最小回归"一致），接受查询重复。赛后要清债再合并到 B。

### 2.4 工时与范围现实

- **本轮（基础设施 + videos）**：6 新文件 + 2 改动，外加 videos 里 ~5 个列表端点重实现 + ~35 个 CRUD/二进制端点包装或重写。1～2 个专注会话可完成。
- **全量 11 模块**：164 端点。即便按计划"一轮一模块"，每模块都要重实现列表端点 + 包装/重写 CRUD + 写契约测试，**这是多周工程**。"做完"若指全量，务必拆成 11 个独立 PR/会话推进。

### 2.5 风险热点

1. `wrap_old_view` 响应分类错判 → 返回破损（最需测试矩阵覆盖）。
2. `after_request` 弃用钩子污染二进制/页面响应（只加 header、按前缀跳过）。
3. 全局 errorhandler 与现有 `413`/页面 404 行为冲突。
4. 列表端点分页重实现时 `total` 计数与旧逻辑不一致（口径漂移）。

---

## 三、全量测试方案（分层，每层可独立跑）

### Layer 0 — 前置（运行环境，非沙箱）

用户真实环境须有：`pip install -r requirements.txt pytest pytest-cov`（flask/waitress 已随项目安装）。沙箱缺这些只影响我这边静态验证，不影响真实环境跑测试。

### Layer 1 — 单元：信封/分页参数/包装器矩阵

`tests/test_api_v1_responses.py`：
- `ok()/created()/accepted()/no_content()/err()` 信封形状断言。
- 分页参数解析：`page<1→1`、`page_size>100→100`、缺省 `page=1,page_size=20`、非法值容错。
- **`wrap_old_view` 分类矩阵（参数化，最关键）**：

| 输入（模拟旧视图返回） | 期望包装输出 |
|----------------------|------------|
| `jsonify({'video': {...}})` | `{code:0, data:{'video':{...}}}` |
| `jsonify([a,b])` | `{code:0, data:{items:[a,b]}}` |
| `(jsonify({'error':'x'}), 404)` | `{code:404, message:'x'}` |
| `send_file(...)` (mimetype 非 json) | **原样透传**，body 不变 |
| `redirect(location)` | **原样透传** |
| `None` / `(None, 204)` | 204 无 body |

### Layer 2 — 模块契约（以 videos 为模板）

`tests/test_api_v1_videos.py`，对每个新 v1 端点断言：
- 成功信封形状（`code/message/data`）。
- 列表端点：`data` 含 `items/total/page/page_size/has_next`，翻页正确。
- 状态码：`200/201/204/400/404/409` 各覆盖一例。
- HTTP 动词路由：`POST` 创建、`PATCH` 部分更新、`PUT` 替换、`DELETE` 删除、错误动词→405。
- 二进制下载：不走信封、mimetype 正确、Content-Disposition 正确。

### Layer 3 — 错误分发

`tests/test_api_v1_errors.py`：
- `GET /api/v1/不存在` → 404 信封。
- v1 端点内部抛异常 → 500 信封（不让 HTML 500 泄漏）。
- 旧路径 404 → 原行为（非信封）。
- 现有 `413` handler 仍生效。
- `after_request` 弃用 header 只加在旧前缀上。

### Layer 4 — 弃用钩子

`tests/test_deprecation.py`：
- 旧端点响应含 `Deprecation: true` + `Link` 指向新资源。
- v1 端点无该 header。
- 页面 HTML / 二进制下载响应不被破坏（body 逐字节不变，仅多了 header）。

### Layer 5 — 回归（核心，防"改全局而伤旧"）

这是全量测试里**最该重的一层**，因为计划的钩子/errorhandler 是全局性的，可能悄悄改变所有旧响应：

- **5a. 现有 52 用例全绿**：`pytest -q` 不许有回归。
- **5b. 旧端点 golden 快照**：参数化遍历**安全的旧 GET `/api` 端点**，断言 `status + content-type + body` 与基线逐字节一致（专门抓"after_request/errorhandler 误改旧响应"的 bug）。基线可存在测试目录的 fixtures 文件里。
- **5c. 页面冒烟**：对每个 `render_template` 页面路由请求一次，断言 200（钩子没把页面搞挂）。

### Layer 6 — 端到端

`python run.py` 启动后跑 curl 矩阵（计划自带的验证清单）：
```bash
curl -s http://localhost:8080/api/v1/videos          # 信封
curl -s -i http://localhost:8080/videos/api/all       # 旧格式 + Deprecation: true
curl -s -i http://localhost:8080/api/v1/不存在        # 404 信封
```
加一轮完整业务回归（与 P0 文档验证总清单对齐）：视频评测 + 报告 PDF 生成 + 推流任务生命周期，确认旧页面功能无回归。

### Layer 7 — 覆盖率门禁

```bash
pytest --cov=app/api --cov-report=term-missing
```
要求：`app/api/v1/compat.py`、`deprecation.py`、`errors.py`、`responses.py` 行覆盖 ≥ 95%（它们是风险最高、最该被测试钉死的模块）；各资源模块 ≥ 85%。

---

## 四、执行命令速查

```bash
# 前置
pip install -r requirements.txt pytest pytest-cov

# 全量（含回归）
pytest -q

# 只跑新增
pytest tests/test_api_v1_responses.py tests/test_api_v1_videos.py \
       tests/test_api_v1_errors.py tests/test_deprecation.py -v

# 覆盖率门禁
pytest --cov=app/api --cov-report=term-missing

# 端到端
python run.py   # 另开终端跑 curl 矩阵
```

每完成一个模块，重复 Layer 2（该模块契约）+ Layer 5b（旧端点 golden 仍一致）+ Layer 6（curl 冒烟）。

---

## 五、建议

1. **本轮先做"基础设施 + videos"作为模板验证**：把 `wrap_old_view` 矩阵、分页重实现、弃用钩子、errorhandler 分流这套机制在 videos 上跑通并测全。这是"能不能做"的真正试金石——videos 跑通了，其余 10 模块就是重复劳动。
2. **澄清 2.3 的矛盾**：确认选"方案 A（不改旧视图、接受查询重复）"再开工，否则会半路被迫回头改旧视图。
3. **全量推进前，先确认赛题是否有"工程规范性/代码质量"给分项**：若有且权重值得，再按 11 模块逐个推；若无，做到"基础设施 + 1 个样板模块"足可对外展示 REST 能力，不必铺满 164 端点。
4. **测试先行**：Layer 1 的 `wrap_old_view` 矩阵和 Layer 5b 的 golden 快照要在写第一行业务代码前就搭好——这俩是防止"全局钩子悄悄改坏旧响应"的唯一护栏。
