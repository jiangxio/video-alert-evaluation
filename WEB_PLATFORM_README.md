# 网页平台使用说明

## 启动应用

### 1. 安装依赖

```bash
# 安装Flask依赖
pip install -r requirements-flask.txt

# 如果还没有安装基础依赖
pip install -r requirements.txt
```

### 2. 启动Web服务器

```bash
python run.py
```

或者：

```bash
./run.py
```

### 3. 访问平台

打开浏览器访问以下地址：

- **首页**: http://localhost:5000/
- **视频管理**: http://localhost:5000/videos/
- **告警图片**: http://localhost:5000/alerts/

## 功能说明

### 视频管理页面

1. **上传视频**: 拖拽或点击选择视频文件上传
2. **查看视频列表**: 显示所有已上传的视频
3. **添加水印**: 点击"添加水印"按钮对视频进行处理
4. **查看水印视频**: 处理完成的水印视频会显示在原视频下方

### 告警图片页面

1. **上传告警图片**: 支持批量上传多个图片文件
2. **查看图片列表**: 显示所有告警图片及其最新状态
3. **OCR识别**: 单独对某张图片进行OCR识别
4. **验证告警**: 单独对某张图片进行验证（使用Mock OCR数据）
5. **批量验证**: 对所有图片进行批量验证

## 数据库

SQLite数据库文件位置: `/data/41-benchmark/benchmark.db`

首次启动时会自动：
- 创建所有数据表
- 导入 `ground_truth/` 目录下的现有标注数据

## 目录结构

```
/data/41-benchmark/
├── app/                      # Flask应用
│   ├── routes/              # API路由
│   │   ├── videos.py        # 视频相关API
│   │   ├── alerts.py        # 告警图片相关API
│   │   └── verification.py  # OCR和验证API
│   ├── services/            # 业务逻辑服务
│   │   ├── watermark_service.py
│   │   └── verification_service.py
│   ├── templates/           # HTML模板
│   ├── static/              # 静态文件 (JS/CSS)
│   ├── database.py          # 数据库管理
│   ├── config.py            # 配置文件
│   └── __init__.py          # 应用工厂
├── uploads/                 # 用户上传文件
│   ├── videos/
│   └── alerts/
├── benchmark.db             # SQLite数据库
└── run.py                   # 应用启动脚本
```
