# 交接文档：为 od-dataset-manager 增加图像分类评测

> 本文档供另一会话实施。od-dataset-manager 已实现完整的目标检测评测（矩形框标注 + YOLO 预测 + IoU/mAP）。本文档设计如何在其基础上增加**图像分类评测**，与现有功能解耦但复用基础设施。
>
> 代码位置：`od-dataset-manager/`（独立 Flask 服务，端口 5000）

---

## 一、背景与目标

### 为什么加分类检测
当前 od 只支持目标检测（多框 + IoU/mAP）。图像分类是另一类视觉任务——整张图一个类别标签，评测用准确率/F1/混淆矩阵。补上后，od 可覆盖两类视觉评测，参赛评分"视觉类任务类型"可从 6 分（视频事件检测+异常检测+目标检测）提升到 8 分（+图像分类 2 分）。

### 分类 vs 目标检测的本质区别

| 维度 | 目标检测（已实现） | 图像分类（待实现） |
|------|-------------------|-------------------|
| 真值 | 多个矩形框（points 两点坐标 + class） | 整图单个类别标签 |
| 预测 | YOLO txt（class cx cy w h conf） | CSV/JSON（图片名 + 预测类别 + 置信度） |
| 评测 | IoU≥阈值匹配框，按类 tp/fp/fn → mAP | 预测类==真值类 → 准确率/F1/混淆矩阵 |
| 标注 UI | SVG 拖拽画框 | 下拉选整图标签 |

### 设计原则
- **项目级 mode 区分**：projects 表加 `mode` 字段（`detection`/`classification`），一个项目一种模式
- **复用基础设施**：项目管理、图片导入、目录浏览、评估结果保存/加载、静态资源全部复用
- **分类专属**：标注 UI（单图选标签）、评测逻辑（准确率/F1/混淆矩阵）、预测导入（CSV）为新增

---

## 二、现有架构速览（实施前必读）

### 数据库 schema（`app.py` init_db，约 22-101 行）
- `projects`：id, name, classes(JSON), images_dir, labels_dir, labels_format, created_at, updated_at
- `images`：id, project_id, name, filename, image_path, image_width, image_height, created_at
- `labels`：id, project_id, image_id, image_path, image_width, image_height, **shapes**(JSON), updated_at
  - shapes 是 `[{"class_idx":0, "points":[[x1,y1],[x2,y2]], "shape_type":"rectangle"}]`（检测框）
- `label_backups`：标注历史备份
- `eval_results`：id, project_id, name, pred_dir, conf_threshold, iou_threshold, metrics(JSON), images(JSON), created_at

### 关键函数（`app.py`）
| 函数 | 位置 | 作用 |
|------|------|------|
| `init_db()` | 22 | 建表（CREATE IF NOT EXISTS，幂等） |
| `db_get_project`/`db_insert_project`/`db_update_project` | 128/141/163 | 项目 CRUD |
| `db_get_label`/`db_save_label` | 289/305 | 标注读写（shapes JSON） |
| `resolve_context()` | 400 | 从请求取 project_id → 返回 (images_dir, labels_dir, classes, project) |
| `_compute_iou(pts_a, pts_b)` | 933 | IoU 计算 |
| `api_evaluate()` | 945 | 评测主逻辑（YOLO预测+GT，按类tp/fp/fn） |
| `api_eval_save_result` 等 | 1066+ | 评估结果保存/列表/加载/删除 |

### 路由（`app.py`）
- 页面：`GET /`（首页项目列表）、`GET /project/<id>`（标注页 index.html）、`GET /project/<id>/evaluate`（评测页 evaluate.html）
- API：`/api/projects`（GET/POST）、`/api/projects/<id>`（GET/PUT/DELETE）、`/api/images`、`/api/labels/<name>`、`/api/evaluate`、`/api/eval/*`、`/api/browse_dir`、`/api/import/*`、`/api/export/*`

### 前端
- `templates/home.html`：项目列表 + 新建/编辑 Modal（含图片路径、标注格式 tabs、类别 textarea）
- `templates/index.html` + `static/main.js`：标注页（SVG 画框）
- `templates/evaluate.html` + `static/evaluate.js`：评测页（选预测目录、conf/iou 阈值、跑评估、看指标表+图网格+查看器）
- `static/style.css`：统一样式

### url 风格
**全部硬编码绝对路径**（`/static/style.css`、`/api/projects`、`/project/<id>`），无 url 前缀。分类功能的新路由遵循同样风格。

---

## 三、实施方案（按改动顺序）

### 步骤 1：数据库加 mode 字段
`app.py` init_db 的 projects 建表加 `mode TEXT DEFAULT 'detection'`，并在 `db_insert_project`/`db_update_project` 读写它。

```python
# init_db projects 表加列（CREATE TABLE 内）
mode TEXT DEFAULT 'detection',

# 兼容已有库：表已存在时补列
cursor.execute("ALTER TABLE projects ADD COLUMN mode TEXT DEFAULT 'detection'")
# 用 try/except 忽略"列已存在"错误（项目现有风格，见主项目 database.py）
```

`db_insert_project`：插入时带 `project.get('mode', 'detection')`。
`db_update_project`：支持更新 mode。
`db_get_project`：dict 已含 mode（SELECT *）。

### 步骤 2：项目创建/编辑 UI 加模式选择
`templates/home.html` 新建项目 Modal 加单选：

```html
<div class="form-row">
    <label>评测模式</label>
    <div class="fmt-tabs" id="proj-mode-tabs">
        <button type="button" class="fmt-tab active" data-mode="detection">目标检测</button>
        <button type="button" class="fmt-tab" data-mode="classification">图像分类</button>
    </div>
</div>
```

JS（home.html 内联脚本）保存时 payload 带 `mode: selectedMode`。分类模式下类别语义变成"整图候选标签"（每行一个），与检测共用同一字段，无需改 classes 存储。

### 步骤 3：分类标注页（新增 `templates/classify.html` + `static/classify.js`）
分类标注不画框——选图片 + 下拉选整图标签。可复用 index.html 的图片网格/分页骨架，去掉 SVG 画框，加单选标签下拉 + "保存"按钮。

数据存储复用 labels 表：分类的 shapes 用约定 `[]`（无框），类别存到一个新字段更清晰——**推荐给 labels 表加 `class_label TEXT` 字段**（整图标签），检测项目该字段为 NULL。

```python
# init_db labels 表加列
class_label TEXT,

# 兼容已有库
cursor.execute("ALTER TABLE labels ADD COLUMN class_label TEXT")
```

标注 API：
- 新增 `POST /api/classify/<image_name>`：保存整图标签 `{ "class_label": "cat" }`，写 labels.class_label
- 复用 `GET /api/images` 列图片，响应里带每张图的 class_label（是否已标注）

### 步骤 4：分类评测逻辑（新增 `/api/evaluate_classify`）
新增评测函数，与 `api_evaluate` 并行，不改动原检测评测：

**输入**：project_id、pred_dir（CSV 预测文件目录）
**预测格式**（CSV，每行 `image_name, predicted_class, confidence`）：
```
0040.png, cat, 0.95
0041.png, dog, 0.88
```
> CSV 格式比 YOLO txt 更适合分类（一行一图一标签）。也支持每图一个 `.json`，但 CSV 单文件更简单，推荐 CSV。

**评测算法**：
```python
def api_evaluate_classify():
    # 1. 读 project（含 mode 校验 == 'classification'、classes 列表）
    # 2. 读 pred CSV → {image_name: (pred_class, conf)}
    # 3. 遍历所有图片：GT = labels.class_label，pred = CSV
    #    - 按 conf 阈值过滤（可选）
    # 4. 统计：
    #    - 整体准确率 = 预测正确的图数 / 总图数
    #    - 每类 precision/recall/f1（二分法：该类 vs 非该类）
    #    - 混淆矩阵 N×N（classes 顺序）
    # 5. 返回 {images: [...], metrics: {accuracy, per_class, confusion_matrix}}
```

**指标**（与检测的区别）：
- **准确率（Accuracy）**：预测类==真值类的图占比（分类主指标）
- **每类 Precision/Recall/F1**：对每个类 c，把"是否 c"当二分类算
- **混淆矩阵**：`matrix[gt][pred] = count`，前端渲染热力表

### 步骤 5：分类评测页面（新增 `templates/evaluate_classify.html` + 复用/扩展 `static/evaluate.js`）
复用 evaluate.html 的左侧配置面板骨架（选预测目录、选类别、跑评估、保存/历史结果），右侧改为：
- 准确率大数字 + 每类 P/R/F1 表
- 混淆矩阵表（行=GT，列=预测，对角线高亮）
- 错分图片网格（可按"全部/正确/错误"过滤，点开看图）

页面路由：`GET /project/<id>/evaluate-classify`（与检测的 `/evaluate` 并行）。

### 步骤 6：标注页按 mode 路由
`app.py` 的 `project_page` 按 project.mode 渲染不同模板：
```python
@app.route('/project/<project_id>')
def project_page(project_id):
    p = db_get_project(project_id)
    if not p: abort(404)
    if p.get('mode') == 'classification':
        return render_template('classify.html', ...)
    return render_template('index.html', ...)  # 原检测标注页
```
评测入口同理按 mode 跳 `/evaluate` 或 `/evaluate-classify`（在标注页的"检测结果评估"按钮处判断）。

### 步骤 7：项目卡片显示 mode
`templates/home.html` 项目卡片加 mode 标签（目标检测/图像分类），让用户区分。

---

## 四、改动文件清单

| 文件 | 改动 |
|------|------|
| `app.py` | init_db 加 mode/class_label 列（含 ALTER 兼容）；db_insert/update_project 支持 mode；新增 classify 标注 API、`api_evaluate_classify`、分类页路由；project_page 按 mode 分流 |
| `templates/home.html` | 新建 Modal 加模式单选；项目卡片显示 mode 标签 |
| `templates/classify.html` | **新增**：分类标注页 |
| `static/classify.js` | **新增**：分类标注交互 |
| `templates/evaluate_classify.html` | **新增**：分类评测页 |
| `static/evaluate.js` | 扩展：分类评测结果渲染（准确率/混淆矩阵/错分网格），或新增 `evaluate_classify.js` |
| `config.py` | 无需改（classes 通用） |

> 不改动：`index.html`、`main.js`（检测标注）、`evaluate.html`（检测评测）、`api_evaluate`、导入导出（COCO/YOLO 是检测专用，分类用 CSV）。

---

## 五、关键设计决策（已定，实施时遵循）

1. **项目级 mode**：一个项目一种模式，不混用。mode=detection 走原流程，mode=classification 走新流程。
2. **labels 表加 class_label 字段**：整图标签独立存储，不塞进 shapes（语义清晰，检测项目该字段 NULL）。
3. **预测用 CSV**（`image_name, class, conf`）：比 YOLO txt 更适合分类，单文件易导入。
4. **新 API 并行不替换**：`/api/evaluate_classify` 与 `/api/evaluate` 并存，检测逻辑零改动。
5. **评估结果复用 eval_results 表**：metrics JSON 存分类指标（accuracy/per_class/confusion），与检测结果同表，用 project 的 mode 区分语义。pred_dir/conf_threshold 字段复用，iou_threshold 对分类无意义存 0。
6. **混淆矩阵**：N×N，`matrix[i][j]` = GT 是 classes[i] 且预测是 classes[j] 的图数。

---

## 六、验证方式

1. **建分类项目**：首页新建项目，选"图像分类"模式，填类别（如 cat/dog/bird），导入图片
2. **标注**：进入标注页（应为分类 UI），给每张图选标签，保存
3. **准备预测**：写一个 CSV（image_name,class,conf），或用简单脚本对测试图生成预测
4. **评测**：进评测页，选预测 CSV 目录，跑评估，看：
   - 准确率数字正确（手动核对几张）
   - 混淆矩阵行列对齐、对角线=正确
   - 错分图片网格能点开看图
5. **保存/加载**：保存评估结果，刷新后能从历史加载
6. **回归**：原目标检测项目（mode=detection）标注/评测功能不受影响
7. **Docker**：`docker compose up -d --build od` 重新构建，分类功能可用

## 七、参考：分类指标计算伪代码

```python
def evaluate_classify(project_id, pred_csv_path, conf_threshold=0.0):
    project = db_get_project(project_id)
    classes = project['classes']  # ['cat','dog','bird']
    cls_to_idx = {c: i for i, c in enumerate(classes)}
    N = len(classes)

    # 读预测
    preds = {}  # image_name -> (pred_class, conf)
    with open(pred_csv_path) as f:
        for row in csv.reader(f):
            name, pred_cls, conf = row[0], row[1], float(row[2])
            if conf >= conf_threshold and pred_cls in cls_to_idx:
                preds[name] = (pred_cls, conf)

    # 读 GT + 统计
    images = db_list_images(project_id)
    correct = 0
    total = 0
    confusion = [[0]*N for _ in range(N)]  # [gt_idx][pred_idx]
    per_class_tp = [0]*N; per_class_fp = [0]*N; per_class_fn = [0]*N

    for img in images:
        gt = get_class_label(project_id, img['name'])  # labels.class_label
        if gt is None: continue  # 未标注，跳过
        total += 1
        pred = preds.get(img['name'], (None, 0))[0]
        gi = cls_to_idx[gt]
        pi = cls_to_idx.get(pred, -1) if pred else -1
        confusion[gi][pi] += 1 if pi >= 0 else ...  # pi<0 当未预测
        if pred == gt:
            correct += 1
            per_class_tp[gi] += 1
        else:
            if pred: per_class_fp[cls_to_idx[pred]] += 1
            per_class_fn[gi] += 1

    accuracy = correct / total if total else 0
    per_class = {}
    for i, c in enumerate(classes):
        tp, fp, fn = per_class_tp[i], per_class_fp[i], per_class_fn[i]
        p = tp/(tp+fp) if tp+fp else 0
        r = tp/(tp+fn) if tp+fn else 0
        f1 = 2*p*r/(p+r) if p+r else 0
        per_class[c] = {'precision': p, 'recall': r, 'f1': f1, 'tp': tp, 'fp': fp, 'fn': fn}

    return {'accuracy': accuracy, 'per_class': per_class,
            'confusion_matrix': confusion, 'images': ...}
```

---

## 八、注意事项

- **ALTER TABLE 兼容**：od 用 `CREATE TABLE IF NOT EXISTS` 幂等建表，但加列要 `try: ALTER TABLE ADD COLUMN; except: pass`（列已存在时忽略），参考主项目 `app/database.py` 的风格。
- **CSV 解析**：用 `csv` 标准库，处理逗号/引号；图片名可能带空格用引号包裹。
- **未标注/未预测图片**：评测时 GT 缺失的跳过，预测缺失的算漏预测（计入 FN 或单独统计，建议单独 `unpredicted` 计数避免混淆 FN 语义）。
- **不要破坏检测流程**：所有新代码走 mode 分支，检测 mode 下不触发分类逻辑。
- **前端复用**：style.css 的 `.eval-metrics-table`、`.filter-tabs`、`.eval-image-grid`、`.modal-overlay` 等类可直接复用，保持视觉一致。
- **独立运行**：改完后 `cd od-dataset-manager && python app.py` 仍可独立跑（DB_DIR 默认同目录），Docker 内通过 OD_DB_DIR 指向卷。
