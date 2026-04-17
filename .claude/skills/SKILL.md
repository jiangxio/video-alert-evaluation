---
name: git-uploader
description: |
  帮助用户将代码上传到Git仓库的智能助手。当你需要执行git提交、推送代码、或整理变更时，使用此技能。
  触发词："上传代码"、"提交到git"、"git commit"、"push代码"、"整理git变更"
  此技能会分析变更文件，区分哪些应该提交（源代码、配置），哪些不应该提交（数据文件、临时文件、敏感信息），
  向用户展示建议并等待确认后再执行操作。
compatibility: |
  需要git命令行工具
---

# Git 智能上库助手

## 快速流程（一步完成）

### 步骤1：分析并展示（自动执行）

运行以下命令获取完整信息：
```bash
git status --porcelain | python3 /path/to/skill/scripts/classify_files.py
```

基于分类结果，直接生成完整提交方案：

---

## Git 提交方案

### 📊 变更概览
- **待提交文件数**: X个
- **建议忽略**: X个（数据/临时文件）
- **需要关注**: X个

### ✅ 将提交的文件
| 序号 | 文件路径 | 操作 | 说明 |
|-----|---------|-----|------|
| 1 | app/main.py | 修改 | Python源代码 |
| 2 | config.json | 新增 | 配置文件 |
| ... | ... | ... | ... |

### ❌ 将忽略的文件
| 文件路径 | 原因 |
|---------|------|
| data/video.mp4 | 媒体文件 |
| temp.log | 日志文件 |

### ⚠️ 需要关注的文件
| 文件路径 | 建议 |
|---------|------|
| .env | 可能包含敏感信息，本次不提交 |

### 📝 建议的提交信息
```
更新XXX功能
```

---

### 步骤2：一次性确认

**使用 AskUserQuestion 一次性询问**：
- 是否同意上述方案并直接执行提交？
- 提交信息是否需要修改？（可选输入框）
- 是否有文件需要调整？（可选输入框，如 "也提交docs目录" 或 "不要提交config.py"）

### 步骤3：直接执行（无需再次确认）

用户同意后，立即执行以下操作：

```bash
# 1. 添加所有建议的文件
git add <file1> <file2> ...

# 2. 创建提交
git commit -m "<提交信息>"

# 3. 推送到远程
git push origin $(git branch --show-current)

# 4. 输出结果摘要
```

---

## 文件分类规则

### 自动提交（代码文件）
- **源代码**: `.py`, `.js`, `.ts`, `.jsx`, `.tsx`, `.java`, `.go`, `.rs`, `.cpp`, `.c`, `.h`
- **配置**: `.json`, `.yaml`, `.yml`, `.toml`, `.ini`, `.conf`, `.gitignore`
- **Web**: `.html`, `.css`, `.scss`, `.less`
- **文档**: `.md`, `.txt`, `.rst`
- **脚本**: `.sh`, `.bash`, `.zsh`, `.ps1`, `.bat`
- **依赖**: `requirements.txt`, `package.json`, `Cargo.toml`, `go.mod`

### 自动忽略（数据/本地文件）
- **数据库**: `*.db`, `*.sqlite`, `*.sqlite3`
- **媒体**: `*.mp4`, `*.avi`, `*.jpg`, `*.png`, `*.gif`, `*.zip`, `*.tar`
- **日志**: `*.log`, `logs/`
- **临时**: `*.tmp`, `*.temp`, `tmp/`, `temp/`
- **缓存**: `__pycache__/`, `*.pyc`, `node_modules/`
- **IDE**: `.vscode/`, `.idea/`
- **系统**: `.DS_Store`, `Thumbs.db`
- **敏感**: `.env`, `*.key`, `*.pem`, `secrets/`
- **生成目录**: `output/`, `dist/`, `build/`, `generated_*`

### 标记审查（需要关注）
- 大文件 (>10MB)
- 可能的敏感信息文件
- 测试数据文件
- 未知类型文件

---

## 提交信息建议

根据变更内容自动生成提交信息：

| 变更类型 | 建议提交信息 |
|---------|-------------|
| 新增功能 | `feat: 新增XXX功能` |
| 修复bug | `fix: 修复XXX问题` |
| 修改配置 | `config: 更新XXX配置` |
| 重构代码 | `refactor: 重构XXX模块` |
| 更新文档 | `docs: 更新XXX文档` |
| 混合变更 | `更新XXX功能和配置` |

---

## 辅助脚本

使用 `scripts/classify_files.py` 自动分类文件：

```bash
git status --porcelain | python3 scripts/classify_files.py
```

输出格式：
```json
{
  "to_commit": [{"path": "file.py", "status": "修改", "reason": "源代码"}],
  "to_ignore": [{"path": "data.mp4", "status": "未跟踪", "reason": "媒体文件"}],
  "to_review": [{"path": ".env", "status": "未跟踪", "reason": "敏感信息"}],
  "summary": {"total": 10, "to_commit": 7, "to_ignore": 2, "to_review": 1}
}
```

---

## 执行后输出模板

```
## ✅ 提交完成

| 项目 | 内容 |
|------|------|
| 提交哈希 | `<commit_hash>` |
| 提交信息 | `<message>` |
| 推送分支 | `<branch>` |
| 新增文件 | X个 |
| 修改文件 | X个 |
| 删除文件 | X个 |

### 提交的文件列表
1. ✅ `file1.py` (修改)
2. ✅ `file2.html` (新增)
...

### 未提交的文件（已忽略）
- `data/video.mp4` (媒体文件)
- `temp.log` (日志文件)
```

---

## 特殊场景

### 场景1：存在合并冲突
先解决冲突，冲突解决后再运行此技能。

### 场景2：大文件(>10MB)
在"需要关注"区域高亮警告，默认不提交，用户明确要求后才提交。

### 场景3：首次提交无.gitignore
询问是否创建 `.gitignore` 模板，然后继续提交流程。

### 场景4：敏感信息检测
在"需要关注"区域警告，**默认不提交**，需要用户明确要求才提交。
