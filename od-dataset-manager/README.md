# 目标检测数据集管理平台

一个基于 Flask 的 Web 应用，用于管理和标注目标检测数据集。支持图片查看、矩形框标注、多类别管理，以及 YOLO 格式的导入导出。

## 功能特性

- **图片管理**：显示数据集中的所有图片，区分已标注和未标注状态
- **标注编辑**：
  - 鼠标拖拽绘制矩形框
  - 支持多类别选择
  - 选中和删除标注框
  - 实时保存标注到 JSON 文件
- **数据格式兼容**：
  - 内部使用自定义 JSON 格式（兼容现有数据集）
  - 支持 COCO 格式导入/导出
- **用户界面**：
  - 响应式设计
  - 左侧图片列表，右侧标注工具
  - 实时状态显示

## 项目结构

```
project/
├── app.py                 # Flask 后端应用
├── config.py              # 配置文件（类别、路径等）
├── templates/
│   └── index.html         # 前端页面模板
├── static/
│   ├── style.css          # CSS 样式
│   └── main.js            # JavaScript 交互逻辑
└── datasets/
    └── calling/
        ├── images/        # 图片文件
        ├── labels/        # 标注 JSON 文件
        │   └── backup/    # 备份目录
        └── scripts/       # 现有处理脚本
```

## 安装依赖

确保您有 Python 3.7+ 环境。

```bash
# 安装依赖包
pip install flask pillow

# 可选：创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# 或 .venv\Scripts\activate  # Windows
```

## 配置

编辑 `config.py` 文件：

```python
# 自定义类别列表
CLASSES = ["call_phone", "not_call_phone", "other"]

# 数据路径（默认）
IMAGES_DIR = "datasets/calling/images"
LABELS_DIR = "datasets/calling/labels"
```

## 运行应用

```bash
# 启动服务器
python app.py

# 浏览器访问
# http://localhost:5000
```

服务器将在 `0.0.0.0:5000` 启动，支持远程访问。

## 使用说明

### 基本操作

1. **浏览网格**：页面显示图片网格，每张图片显示缩略图和标注框（如果有）。
2. **选择图片**：点击网格中的图片选中它（边框高亮）。
3. **标注图片**：点击"标注选中图像"按钮，进入详细标注模式。
4. **绘制标注**：
   - 选择类别（下拉菜单）
   - 在图片上拖拽鼠标绘制矩形
5. **编辑标注**：
   - 点击已有的框选中它
   - 点击"删除选中框"删除
6. **保存**：点击"保存标注"按钮
7. **返回网格**：点击"返回网格"按钮，继续浏览其他图片
8. **分页**：使用上一页/下一页按钮浏览更多图片

### 过滤和统计

- 点击"未标注"按钮，只显示未标注的图片
- 点击"刷新列表"重新加载所有图片
- 统计信息显示总图片数、已标注数、未标注数

### 导入导出

- **导出 COCO**：点击"导出 COCO"，生成 `datasets/calling/coco_export.json`
- **导入 COCO**：点击"导入 COCO"，选择 COCO JSON 文件上传
- **导出 YOLO**：点击"导出 YOLO"，生成 `datasets/calling/yolo_export/` 目录
- **导入 YOLO**：选择图片后，点击"导入 YOLO"，上传对应的 `.txt` 文件

### 数据格式

内部标注格式（JSON）：

```json
{
  "imagePath": "0040.png",
  "imageHeight": 480,
  "imageWidth": 640,
  "shapes": [
    {
      "label": "call_phone",
      "points": [[97.7, 61.1], [129.1, 97.2]],
      "group_id": null,
      "shape_type": "rectangle",
      "flags": {}
    }
  ]
}
```

## 注意事项

- 图片和标注文件通过文件名关联（不含扩展名）
- 支持的图片格式：`.jpg`, `.jpeg`, `.png`
- 保存时会自动备份旧标注到 `labels/backup/` 目录
- 文件名安全校验：仅允许字母、数字、下划线、点和横线

## 开发说明

- 后端：Flask + PIL
- 前端：原生 JavaScript + SVG
- 数据处理：YOLO 格式转换

## 许可证

MIT License

## 贡献

欢迎提交 Issue 和 Pull Request。