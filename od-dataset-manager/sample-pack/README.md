# 数据集样例包（打电话场景）

一个自包含样例，用于演示/测试 **od-dataset-manager** 平台的目标检测与图像分类评估功能。同一批 564 张图片，同时提供检测标注和分类标注，以及基于真值故意改错的「假预测」——可直接跑评估、验证指标计算。

## 场景

监控画面中「打电话」行为识别。原始标注是单类目标检测（`call_phone`）。分类侧把「无目标框的图」视为 `not_call_phone`，构成二分类（`call_phone` / `not_call_phone`）。

## 目录结构

```
sample-pack/
├── images/                      # 564 张图片（检测/分类共用，文件名即 base name）
├── detection/                   # 目标检测
│   ├── gt/                      # 真值 YOLO 标注（564 txt + classes.txt）
│   └── pred/                    # 假预测 YOLO（564 txt）
└── classification/              # 图像分类
    ├── gt/                      # 真值 CSV（labels.csv）
    └── pred/                    # 假预测 CSV（pred.csv）
```

## 文件格式

### 检测（YOLO）

- 每张图对应一个同名 `.txt`（`0002.jpg` ↔ `0002.txt`）。
- 每行一个框：`class_id cx cy w h [confidence]`，坐标均归一化到 `[0,1]`。
- `class_id` 按 `classes.txt` 顺序索引（本项目 `0 → call_phone`）。
- `gt/` 不带 confidence；`pred/` 带 confidence（评估时低于阈值会被滤除）。

### 分类（CSV）

- `image_name,class_label[,confidence]`，首行表头。
- `image_name` 为 base name（无扩展名），与 `images/` 文件名对齐。
- `class_label` 为空 = 未标注；第三列 confidence 对评估可选。

## 真值 vs 假预测（用于验证评估逻辑）

假预测是在真值基础上**故意改错**生成的，因此预期指标已知，可反向核验评估功能是否正确。

### 检测

| 项 | 真值(GT) | 假预测(pred) |
|---|---|---|
| 图数 | 564 | 564 |
| 框数 | 235（call_phone） | — |
| 改错方式 | — | 每 5 张有框图漏检 1 张（FN）、每 10 张无框图误检 1 张（FP） |
| 预期结果 | — | TP=188, FP=32, FN=47 → **precision=0.855, recall=0.800** |

### 分类

| 项 | 真值(GT) | 假预测(pred) |
|---|---|---|
| 图数 | 564 | 564 |
| 类别分布 | call_phone=235, not_call_phone=329 | — |
| 改错方式 | — | 每 10 条翻转 1 条类别（共 57 条改错） |
| 预期结果 | — | correct=507 → **accuracy=0.899**；混淆矩阵 `[211,24],[33,296]` |

## 如何评估

评估从平台数据库读取 GT，因此需先把 `gt/` 导入对应项目（检测项目 mode=detection，分类项目 mode=classification）。

1. **导入真值**：
   - 检测：页面上传图片后，「导入」浏览选 `detection/gt/` → 调 `POST /api/import/yolo_dir {src_dir}`。
   - 分类：上传图片后，「导入」浏览选 `classification/gt/` → 调 `POST /api/import/classify_csv {src_dir}`。
2. **跑评估**：
   - 检测：进 `/version/<version_id>/evaluate`，「预测目录」浏览选 `detection/pred/`，IoU=0.5、conf=0.25 → 调 `POST /api/evaluate {version_id, pred_dir, conf_threshold, iou_threshold}`。
   - 分类：进 `/version/<version_id>/evaluate-classify`，「预测目录」浏览选 `classification/pred/` → 调 `POST /api/evaluate_classify {version_id, pred_dir}`。
3. **核对指标**：对照上表预期值。一致即评估逻辑正确。
