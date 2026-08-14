# 接入指南

本文档说明如何将平台对接到公司 AI 产品常态化评测场景：注册算法版本、导入告警数据、导入真值（ground truth），完成算法版本迭代的评测闭环。

## 评测闭环总览

```
算法版本注册 → 视频打水印+标注事件(GT) → 告警数据导入 → 评测任务 → 指标计算 → 报告生成
```

## 一、算法版本注册

平台管理被测算法的版本，便于追溯与对比。通过 Web 界面或 REST API 注册。

### Web 界面
访问「算法管理」页面（`/algorithms/`），新建算法版本，填写算法类型、版本日期、描述，并上传算法配置文件与可执行文件。

### REST API

```bash
# 创建算法版本
curl -X POST http://localhost:8080/algorithms/api/versions \
  -F "algorithm_type=personFallDown" \
  -F "name=v2.3-20260814" \
  -F "version_date=2026-08-14" \
  -F "description=跌倒检测v2.3" \
  -F "config_file=@config.yaml" \
  -F "algorithm_file=@algorithm.zip"

# 查询版本列表
curl http://localhost:8080/algorithms/api/versions
```

### 算法类型（事件类型）

算法类型即事件类型，定义于 `app/event_types.py`（硬编码注册表）与 `config/alert_types.json`（可扩展配置）。每个类型有唯一 ID 与 key，例如：

| ID | key | 说明 |
|----|-----|------|
| 114 | personFallDown | 人员跌倒 |
| 115 | personAction | 人员行为 |
| 116 | inHandDangerTool | 手持危险工具 |
| 117 | fight | 打架 |

新增告警类型：编辑 `config/alert_types.json`，每行格式 `id name`，重启服务自动播种到数据库。

---

## 二、视频打水印与真值标注

评测依赖「水印」实现告警与真值的自动对齐：给视频加水印（含视频 ID + 时间戳），OCR 提取水印即可对齐。

### 1. 上传视频并打水印

通过「视频管理」页面上传视频，设置 **10 位数字视频 ID**，点击打水印。FFmpeg 在左上角添加 `{视频ID} | {HH:MM:SS}` 水印。

CLI 方式：
```bash
python process.py --single video1/0514000003.mp4
# 或批量
python process.py --batch
```

### 2. 标注事件，生成 ground truth

在「视频管理」页面为视频标注事件区间（事件类型 + 起止秒数），系统自动：
- 生成 `ground_truth/{视频ID}.json` 真值文件
- 每秒截取一帧作为 GT 帧用于核对

### ground truth JSON 格式

```json
{
  "file": "0514000003.mp4",
  "id": "0514000003",
  "events": [
    {"type": "rat", "start": 5.0, "end": 8.0},
    {"type": "rat", "start": 8.0, "end": 11.0}
  ]
}
```

| 字段 | 说明 |
|------|------|
| id | 视频 ID（10 位数字） |
| events[].type | 事件类型 key（须在 `alert_types.json`/注册表中） |
| events[].start/end | 事件起止秒数 |

启动时 `app/database.py` 的 `import_ground_truth()` 自动将 `ground_truth/*.json` 导入数据库。

---

## 三、告警数据导入

被测算法对水印视频运行后产出告警图片，导入平台做评测。

### 告警文件名规范

文件名必须编码「告警类型 ID」与「触发时间戳」。**标准格式**：

```
{video_id}_{unix时间戳}_{告警类型ID}.png
```

示例：`402_1774925112_103.png` 表示告警类型 103、时间戳 1774925112。

**格式约束**：
- 三段均为纯数字，以下划线 `_` 分隔
- 告警类型 ID 必须在 `config/alert_types.json` 中登记，否则导入时标记为无效类型（不阻断导入，响应返回 `invalid_type_ids` 警告）
- 扩展名支持 png/jpg/jpeg/gif/bmp

**解析规则**（见 `app/services/verification_service.py` 的 `extract_alert_type_id`）：
- 标准三段式 `^\d+_\d+_(\d+)\.[^.]+$` 优先严格匹配，取第三段
- 兜底1：`[_\-](\d+)\.[^.]+$`，取末尾数字段（兼容 `xxx-105.png`）
- 兜底2：`(\d+)\.[^.]+$`，取扩展名前数字（兼容 `105.png`）

> 建议统一使用标准三段式命名，兜底规则仅为兼容历史数据。

### alert_types.json 格式

每行一条记录，`id key` 格式：

```
117 fight
```

**约束**：
- `id`：3 位数字，唯一
- `key`：英文标识，与 `event_types` 表 key 一致，唯一
- 顺序无关，启动时按 id 排序播种到数据库 `event_types` 表
- 新增类型：编辑此文件，重启服务自动播种；或通过「事件类型」页面操作（会回写此文件）

### 算法配置格式

算法版本注册时上传的配置文件，支持 JSON / YAML 两种格式（由 `app/services/config_parser.py` 解析）：

```yaml
# YAML 示例
model: personFallDown
version: "2.3"
threshold: 0.5
min_duration: 2.0
```

**约束**：
- 字段由被测算法自定义，平台仅存储与版本绑定，不做强校验
- 同一算法类型每版本一个配置文件，便于追溯
- 下载：通过「算法管理」页面或 `/algorithms/api/versions/<id>/detail`

### 批量导入

通过「告警数据集」页面创建数据集，支持从 ZIP/tar/tar.gz 批量导入告警图片。系统从文件名自动识别事件类型，无效类型在响应中标记。

---

## 四、评测任务

在「评测」页面（`/evaluation/`）创建评测任务：

1. **创建任务**：命名，选择评测视频集与告警评测集，配置参数（合并间隔、事件起止、触发率等）
2. **执行评测**：系统对告警图片 OCR 提取水印（视频 ID + 时间戳），与真值事件按时间窗口（±5s 容差）匹配，判定命中/误检
3. **确认结果**：人工确认/调整命中判定（`confirmed_count` 等参数）
4. **完成评测**：计算精确率、召回率、平均误检数/小时等指标
5. **生成报告**：导出自包含 HTML 或 PDF 算法验证报告

### 核心指标

| 指标 | 计算口径 |
|------|----------|
| 精确率（Precision） | 有效状态=correct 的告警数 / 有效状态≠ignored 的告警总数 |
| 召回率（Recall） | 各事件类型召回率的算术平均（不加权） |
| 平均误检数/小时 | 误检告警数 / 评测视频总时长（小时） |

详细计算逻辑见 `CLAUDE.md` 的「评测核心指标计算逻辑」章节。

---

## 五、数据集模式

数据集支持两种模式：
- **normal**：普通告警图片数据集
- **realtime**：实时采集模式

模式影响标注判定逻辑，详见「使用指南」。

---

## 六、目标检测评测（od 模块，独立服务）

平台另集成一个**独立的目标检测评测服务** `od-dataset-manager`，与视频流评测完全解耦（独立 Flask app / 独立数据库 annotations.db / 独立端口 5000），补足图像级目标检测评测能力。

### 访问与部署

- 访问：`http://<主机>:5000/`，或从主平台导航「目标检测」入口跳转
- 部署：`docker compose up -d` 同时启动 web(8080) + od(5000) 两个服务
- 解耦：停 od 不影响视频评测，反之亦然；数据卷独立（od_db / od_datasets）

### 评测流程

1. **创建项目**：配置类别列表（如 `call_phone, not_call_phone, other`）与图片目录
2. **标注真值**：上传图片，SVG 拖拽画矩形框，标注真实目标
3. **导入预测**：被测算法输出 YOLO 格式预测文件（`.txt`，每行 `class cx cy w h conf`），指定预测目录
4. **评测计算**：`/api/evaluate` 按 IoU 阈值匹配预测框与真值框，按类统计 tp/fp/fn，输出精确率/召回率/mAP

### 支持格式

| 格式 | 用途 |
|------|------|
| 内部 JSON | 标注存储（`shapes[].points` 矩形两点） |
| COCO | 导入/导出数据集 |
| YOLO | 预测文件格式 + 导出（归一化坐标） |

### 与视频评测的区别

| 维度 | 视频流评测（web） | 目标检测评测（od） |
|------|------------------|-------------------|
| 输入 | 视频告警图片 | 任意图片 |
| 真值 | 时间区间事件 | 矩形框坐标 |
| 对齐 | OCR 时间戳 ↔ GT 区间 | IoU 框匹配 |
| 指标 | 精确率/召回率/误检小时 | mAP/精确率/召回率 |

> od 模块代码位于 `od-dataset-manager/`，可独立运行 `python app.py`（DB_DIR 默认与代码同目录）。
