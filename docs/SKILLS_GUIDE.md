# Claude Code Skills &amp; Agents 使用指南

本文档记录当前安装的所有 skills 和 agents，包括使用场景、调用方式和注意事项。

---

## 安装概述

**安装时间**: 2026-04-10  
**来源**: obra/superpowers (GitHub) + anthropic/skills (官方)  
**Skill 总数**: 34

---

## 目录

1. [Skill 总数统计](#skill-总数统计)
2. [Superpowers Skills 核心工作流](#superpowers-skills-核心工作流)
3. [完整 Skill 列表 - Superpowers](#完整-skill-列表---superpowers)
4. [完整 Skill 列表 - Anthropic](#完整-skill-列表---anthropic)
5. [Code-Reviewer Agent](#code-reviewer-agent)
6. [使用注意事项](#使用注意事项)

---

## Skill 总数统计

### Superpowers Skills (14个)
1. brainstorming
2. test-driven-development
3. systematic-debugging
4. using-git-worktrees
5. writing-plans
6. executing-plans
7. subagent-driven-development
8. dispatching-parallel-agents
9. requesting-code-review
10. receiving-code-review
11. verification-before-completion
12. finishing-a-development-branch
13. using-superpowers
14. writing-skills

### Anthropic Skills (17个)
1. docx (官方版)
2. xlsx (官方版)
3. pdf
4. pptx
5. frontend-design
6. claude-api
7. mcp-builder
8. doc-coauthoring (官方版)
9. skill-creator (官方版)
10. algorithmic-art
11. brand-guidelines
12. canvas-design
13. internal-comms
14. slack-gif-creator
15. theme-factory
16. webapp-testing
17. web-artifacts-builder

### 其他 Skills (3个)
1. tech-research (原安装)
2. simplify (原安装)
3. loop (原安装)

---

## Superpowers Skills 核心工作流

```
brainstorming → writing-plans → (using-git-worktrees) 
    ↓
subagent-driven-development / executing-plans
    ↓
(requesting-code-review)
    ↓
verification-before-completion
    ↓
finishing-a-development-branch
```

---

## 完整 Skill 列表 - Superpowers

### 1. brainstorming - 头脑风暴与设计

**何时使用**: 
- 任何创意工作前（创建功能、构建组件、添加功能、修改行为）
- 开始编码前必须使用

**核心功能**:
- 探索用户意图和需求
- 提出 2-3 种方案并给出建议
- 分节展示设计并获取用户批准
- 保存设计文档到 `docs/superpowers/specs/`

**注意事项**:
- ⚠️ **硬约束**: 在获得设计批准前，不能调用任何实现技能、编写代码或采取任何实现行动
- 每个项目都需要这个流程，无论多简单
- 一次只问一个问题
- 完成后必须调用 `writing-plans`

**触发词**: "brainstorm", "想个方案", "设计一下", "帮我规划"

---

### 2. test-driven-development - 测试驱动开发

**何时使用**:
- 实现任何功能或 bug 修复时
- 在编写实现代码之前

**核心流程 (RED-GREEN-REFACTOR)**:
1. **RED**: 编写失败的测试
2. **Verify RED**: 看着它失败
3. **GREEN**: 编写最小代码让测试通过
4. **Verify GREEN**: 看着它通过
5. **REFACTOR**: 清理代码，保持测试通过

**铁律**:
```
没有失败的测试 → 不能写生产代码
```

**注意事项**:
- 先写测试再写代码，顺序不能反
- 如果先写了代码，必须删除并从头开始
- 异常：一次性原型、生成代码、配置文件（需询问用户）
- 测试必须真实失败（不能是拼写错误导致的失败）

**触发词**: "TDD", "写个测试", "测试驱动"

---

### 3. systematic-debugging - 系统化调试

**何时使用**:
- 遇到任何 bug、测试失败或意外行为时
- 提出修复方案之前

**四个阶段**:

#### 阶段 1: 根因调查（必须）
- 仔细阅读错误信息和堆栈跟踪
- 可复现吗？精确步骤是什么？
- 检查最近的更改（git diff, 提交记录）
- 多组件系统：在每个组件边界添加诊断日志

#### 阶段 2: 模式分析
- 找到代码库中类似的正常工作代码
- 与参考实现对比
- 识别差异
- 理解依赖关系

#### 阶段 3: 假设与测试
- 形成单一假设："我认为 X 是根因，因为 Y"
- 做最小改动测试假设
- 一次只改变一个变量

#### 阶段 4: 实现
- 创建失败的测试用例（使用 TDD skill）
- 实现单一修复
- 验证修复有效
- 如果尝试 3 次修复都失败 → 质疑架构

**铁律**:
```
没有根因调查 → 不能提出修复方案
```

**触发词**: "debug", "修 bug", "调试", "为什么失败"

---

### 4. using-git-worktrees - Git Worktree 管理

**何时使用**:
- 开始需要与当前工作区隔离的功能工作时
- 执行实施计划之前
- 需要并行开发多个分支时

**目录选择优先级**:
1. 检查现有目录：`.worktrees/`（隐藏）或 `worktrees/`
2. 检查 `CLAUDE.md` 中的偏好
3. 询问用户

**安全验证**:
- 项目本地目录必须验证已被 `.gitignore` 忽略
- 如果未忽略，添加到 `.gitignore` 并提交

**创建步骤**:
1. 检测项目名称
2. 创建 worktree 和新分支
3. 运行项目设置（npm install, cargo build 等）
4. 验证干净基线（运行测试）
5. 报告位置

**触发词**: "worktree", "隔离开发", "并行分支"

---

### 5. writing-plans - 编写实施计划

**何时使用**:
- 有规格或需求的多步骤任务
- 碰代码之前

**核心原则**:
- 假设工程师对代码库零上下文
- 记录他们需要知道的一切：每个任务碰哪些文件、代码、测试、文档
- 每个步骤是一个行动（2-5 分钟）
- DRY, YAGNI, TDD, 频繁提交

**保存位置**: `docs/superpowers/plans/YYYY-MM-DD-<feature-name>.md`

**执行移交选项**:
1. **Subagent-Driven（推荐）** - 每个任务派生子代理，任务间审查
2. **Inline Execution** - 本会话中批量执行，带检查点

**触发词**: "写计划", "实施计划", "分解任务"

---

### 6. executing-plans - 执行计划

**何时使用**:
- 有书面实施计划
- 在单独会话中执行（不使用子代理）

**流程**:
1. 加载并审查计划
2. 执行每个任务（精确遵循步骤）
3. 完成后使用 `finishing-a-development-branch`

**注意事项**:
- 必须先用 `using-git-worktrees`
- 受阻时停止询问，不要猜测

**触发词**: "执行计划", "按计划实施"

---

### 7. subagent-driven-development - 子代理驱动开发

**何时使用**:
- 有实施计划
- 任务基本独立
- 保持在本会话中

**流程**:
```
读取计划 → 提取所有任务 → 创建 TodoWrite
    ↓
每个任务:
  → 派实施子代理
  → 回答问题
  → 子代理实施、测试、提交、自审
  → 派规格审查子代理
  → 派代码质量审查子代理
  → 标记任务完成
    ↓
派最终代码审查子代理
    ↓
使用 finishing-a-development-branch
```

**注意事项**:
- 必须先用 `using-git-worktrees` 设置隔离工作区
- 两个阶段审查：先规格合规，再代码质量

**触发词**: "子代理开发", "并行执行", "派代理"

---

### 8. dispatching-parallel-agents - 调度并行代理

**何时使用**:
- 面临 2+ 个独立任务，无共享状态或顺序依赖
- 3+ 个测试文件因不同根因失败
- 多个子系统独立损坏

**触发词**: "并行处理", "多代理", "同时调查"

---

### 9. requesting-code-review - 请求代码审查

**何时使用**:
- 子代理驱动开发中每个任务后
- 完成主要功能后
- 合并到 main 之前

**触发词**: "代码审查", "请求审查", "审查一下"

---

### 10. receiving-code-review - 接收代码审查

**何时使用**:
- 接收代码审查反馈时
- 实施建议前（特别是反馈不清晰或技术上有疑问时）

**禁止响应**:
- ❌ "You're absolutely right!"
- ❌ "Great point!" / "Excellent feedback!"

**正确响应**:
- ✅ "Fixed. [简要描述改变了什么]"
- ✅ [直接修复并在代码中展示]

**触发词**: "审查反馈", "处理评论", "修复审查问题"

---

### 11. verification-before-completion - 完成前验证

**何时使用**:
- 声称工作完成、修复或通过时
- 提交或创建 PR 之前
- 任何成功/完成声明之前

**铁律**:
```
没有新鲜验证证据 → 不能做出完成声明
```

**触发词**: "完成了", "通过了", "验证一下", "确认"

---

### 12. finishing-a-development-branch - 完成开发分支

**何时使用**:
- 实施完成，所有测试通过
- 需要决定如何集成工作时

**四个选项**:
1. 本地合并回基分支
2. Push 并创建 Pull Request
3. 保持分支原样（稍后处理）
4. 丢弃此工作

**触发词**: "完成开发", "合并分支", "创建 PR"

---

### 13. using-superpowers - 使用 Superpowers

**何时使用**:
- 开始任何对话时 - 介绍如何找到和使用 skills

**触发词**: "superpowers 帮助", "如何使用"

---

### 14. writing-skills - 编写新 Skills

**何时使用**:
- 创建新 skills，编辑现有 skills，或验证 skills 在部署前能工作

**触发词**: "写 skill", "创建 skill", "编辑 skill"

---

## 完整 Skill 列表 - Anthropic

### 15. docx - Word 文档处理（官方版）

**何时使用**:
- 用户想要创建、读取、编辑或操作 Word 文档（.docx 文件）时
- 提到 "Word doc"、"word document"、".docx" 时
- 请求制作专业文档（带目录、标题、页码、信头）时

**核心功能**:
- 读取/分析内容：使用 pandoc 或解包原始 XML
- 从模板创建新文档：使用 docx-js
- 编辑现有文档：解包 → 操作幻灯片 → 编辑内容 → 清理 → 打包

**关键规则**:
- 总是显式设置页面大小（docx-js 默认为 A4）
- 永远不要手动插入项目符号字符（使用 LevelFormat.BULLET）
- 表格需要双宽度（columnWidths 数组 AND 单元格 width）
- 总是使用 WidthType.DXA（百分比在 Google Docs 中会破坏）

**依赖**:
- pandoc: 文本提取
- docx: `npm install -g docx`（新文档）
- LibreOffice: PDF 转换

**触发词**: "Word", "docx", "生成报告", "创建文档", "编辑 Word"

---

### 16. xlsx - Excel 表格处理（官方版）

**何时使用**:
- 电子表格文件是主要输入或输出时
- 打开、读取、编辑、修复现有的 .xlsx, .xlsm, .csv, .tsv 文件
- 从头创建新电子表格
- 转换表格文件格式

**核心原则**:
- **使用公式，而不是硬编码值** - 始终使用 Excel 公式而不是在 Python 中计算值
- 零公式错误 - 每个 Excel 模型必须零公式错误交付
- 金融模型颜色编码标准：
  - 蓝色文字 (RGB: 0,0,255): 硬编码输入
  - 黑色文字 (RGB: 0,0,0): 所有公式和计算
  - 绿色文字 (RGB: 0,128,0): 同一工作簿内链接
  - 红色文字 (RGB: 255,0,0): 外部文件链接

**关键规则**:
- 使用公式重新计算：使用 `scripts/recalc.py` 脚本
- 验证并修复任何错误：#REF!, #DIV/0!, #VALUE!, #N/A, #NAME?

**依赖**:
- pandas: 数据分析
- openpyxl: 公式和格式化
- LibreOffice: 公式重新计算

**触发词**: "Excel", "xlsx", "spreadsheet", "电子表格", "生成表格"

---

### 17. pdf - PDF 文档处理

**何时使用**:
- 用户想要对 PDF 文件做任何事情时
- 读取或提取 PDF 中的文本/表格
- 合并或拆分多个 PDF
- 旋转页面、添加水印
- 创建新 PDF、填充 PDF 表单
- 加密/解密 PDF、提取图像
- 对扫描的 PDF 进行 OCR 使其可搜索

**Python 库**:
- **pypdf**: 基本操作（合并、拆分、旋转、元数据）
- **pdfplumber**: 文本和表格提取（布局保留）
- **reportlab**: 创建 PDF

**命令行工具**:
- **pdftotext (poppler-utils)**: 文本提取
- **qpdf**: 合并、拆分、旋转、解密
- **pdfimages**: 提取图像

**触发词**: "PDF", "pdf", "读取 PDF", "合并 PDF", "创建 PDF"

---

### 18. pptx - PowerPoint 演示文稿处理

**何时使用**:
- .pptx 文件以任何方式涉及时 - 作为输入、输出或两者都是
- 创建幻灯片、推销或演示文稿
- 读取、解析或从任何 .pptx 文件提取文本
- 编辑、修改或更新现有演示文稿
- 合并或拆分幻灯片文件
- 使用模板、布局、演讲者备注或评论

**设计理念**:
- **选择大胆的、内容相关的调色板** - 一个颜色占主导（60-70% 视觉权重）
- **主导优于平等** - 永远不要给所有颜色相等的权重
- **深色/浅色对比** - 标题+结论幻灯片深色背景，内容幻灯片浅色（"三明治"结构）
- **提交视觉主题** - 选择一个独特的元素并重复它

**快速参考**:
- 读取/分析内容: `python -m markitdown presentation.pptx`
- 编辑或从模板创建: 读取 editing.md
- 从头创建: 读取 pptxgenjs.md

**依赖**:
- `pip install "markitdown[pptx]"` - 文本提取
- `npm install -g pptxgenjs` - 从头创建
- LibreOffice (`soffice`) - PDF 转换

**触发词**: "PowerPoint", "pptx", "slides", "deck", "演示文稿"

---

### 19. frontend-design - 前端界面设计

**何时使用**:
- 用户要求构建 Web 组件、页面、人工制品、海报或应用程序时
- 示例包括：网站、登录页面、仪表板、React 组件、HTML/CSS 布局
- 或在样式化/美化任何 Web UI 时

**设计思维**:
- **目的**: 这个界面解决什么问题？谁使用它？
- **基调**: 选择一个极端：极简主义、极繁主义混乱、复古未来主义、有机/自然、奢华/精致、好玩/玩具般、编辑/杂志、野兽派/原始、装饰艺术/几何、柔和/粉彩、工业/实用主义等
- **约束**: 技术要求（框架、性能、可访问性）
- **差异化**: 什么让这个令人难忘？

**关注领域**:
- **排版**: 选择美丽、独特、有趣的字体。避免通用字体如 Arial 和 Inter
- **颜色与主题**: 提交到连贯的美学。使用 CSS 变量保持一致性
- **动效**: 对效果和微交互使用动画。优先考虑 CSS 仅有的解决方案
- **空间构图**: 意外的布局、不对称、重叠、对角线流动
- **背景与视觉细节**: 创建氛围和深度而不是默认为纯色

**触发词**: "frontend", "前端", "设计界面", "构建页面", "UI 设计"

---

### 20. claude-api - Claude API 应用开发

**何时使用**:
- 代码导入 `anthropic`/`@anthropic-ai/sdk` 时
- 用户要求使用 Claude API、Anthropic SDK 或托管代理时
- 构建、调试和优化 Claude API 应用程序

**输出要求**:
- 官方 Anthropic SDK（推荐）
- 原始 HTTP（仅当用户明确要求 cURL/REST/raw HTTP 时）

**默认设置**:
- 模型: Claude Opus 4.6 (`claude-opus-4-6`)
- 思考: 自适应思考 (`thinking: {type: "adaptive"}`)
- 流式: 对于长输入、长输出或高 `max_tokens` 的请求默认使用流式

**当前模型**:
| 模型 | 模型 ID | 上下文 | 输入 $/1M | 输出 $/1M |
|-----|---------|--------|-----------|------------|
| Claude Opus 4.6 | `claude-opus-4-6` | 200K (1M beta) | $5.00 | $25.00 |
| Claude Sonnet 4.6 | `claude-sonnet-4-6` | 200K (1M beta) | $3.00 | $15.00 |
| Claude Haiku 4.5 | `claude-haiku-4-5` | 200K | $1.00 | $5.00 |

**语言检测**:
- `*.py`, `requirements.txt` → **Python**
- `*.ts`, `*.tsx`, `package.json` → **TypeScript**
- `*.java`, `pom.xml` → **Java**
- `*.go`, `go.mod` → **Go**

**触发词**: "Claude API", "Anthropic SDK", "构建 Claude 应用", "托管代理"

---

### 21. mcp-builder - MCP 服务器开发指南

**何时使用**:
- 构建 MCP（模型上下文协议）服务器，使 LLM 能够通过精心设计的工具与外部服务交互时
- 构建 MCP 服务器以集成外部 API 或服务时

**四个主要阶段**:

#### 阶段 1: 深度研究和规划
- 理解现代 MCP 设计（API 覆盖 vs 工作流工具）
- 研究 MCP 协议文档
- 研究框架文档
- 规划你的实现

#### 阶段 2: 实现
- 设置项目结构
- 实现核心基础设施
- 实现工具

#### 阶段 3: 审查和测试
- 代码质量检查
- 构建和测试

#### 阶段 4: 创建评估
- 创建 10 个复杂的、真实的问题
- 测试 MCP 服务器的有效性

**推荐技术栈**:
- **语言**: TypeScript（高质量 SDK 支持）
- **传输**: 远程服务器用流式 HTTP，本地服务器用 stdio

**触发词**: "MCP", "Model Context Protocol", "构建 MCP 服务器", "MCP 工具"

---

### 22-34. 其他 Anthropic Skills

| Skill | 用途 |
|-------|------|
| **doc-coauthoring** | 结构化文档协作工作流（官方版） |
| **skill-creator** | 创建有效技能的指南（官方版） |
| **algorithmic-art** | 使用 p5.js 创建算法艺术 |
| **brand-guidelines** | 应用 Anthropic 官方品牌颜色和排版 |
| **canvas-design** | 使用设计哲学创建美丽的视觉艺术（.png 和 .pdf） |
| **internal-comms** | 编写各种内部沟通资源 |
| **slack-gif-creator** | 为 Slack 创建优化的 GIF 动画的知识和工具 |
| **theme-factory** | 使用主题样式化工件的工具包（10 个预设主题） |
| **webapp-testing** | 使用 Playwright 与本地 Web 应用交互和测试的工具包 |
| **web-artifacts-builder** | 使用现代前端技术创建复杂的 claude.ai HTML 工件套件 |

---

## Code-Reviewer Agent

### Agent: code-reviewer

**描述**: 高级代码审查者，专长于软件架构、设计模式和最佳实践。在主要项目步骤完成后使用，对照原始计划和编码标准审查。

**何时使用**:
- 主要项目步骤完成时
- 逻辑代码块编写后
- 对照原始计划验证实现时

**审查内容**:

#### 1. 计划对齐分析
- 对照原始计划文档或步骤描述比较实现
- 识别任何偏离计划方法、架构或需求
- 评估偏离是合理改进还是有问题的背离
- 验证所有计划功能已实现

#### 2. 代码质量评估
- 审查代码是否遵循已建立的模式和约定
- 检查适当的错误处理、类型安全和防御性编程
- 评估代码组织、命名约定和可维护性
- 评估测试覆盖和测试实现质量
- 寻找潜在安全漏洞或性能问题

#### 3. 架构和设计审查
- 确保实现遵循 SOLID 原则和已建立的架构模式
- 检查适当的关注点分离和松耦合
- 验证代码与现有系统良好集成
- 评估可扩展性和可扩展性考虑

#### 4. 文档和标准
- 验证代码包含适当的注释和文档
- 检查文件头、函数文档和内联注释是否存在且准确
- 确保遵循项目特定的编码标准和约定

#### 5. 问题识别和建议
- 清楚分类问题为：Critical（必须修复）、Important（应该修复）、Suggestions（锦上添花）
- 对每个问题，提供具体示例和可操作建议
- 当识别计划偏离时，解释是有问题还是有益
- 有用时建议具体改进和代码示例

**如何调用**:
```
Agent("code-reviewer", prompt="...")
```

---

## 使用注意事项

### 一般原则

1. **技能自动触发**: 大多数 skills 在你说相关关键词时自动激活，不需要手动调用 `skill:`
2. **顺序很重要**: 遵循工作流顺序（brainstorm → plan → implement → verify → finish）
3. **没有捷径**: 不要跳过验证、测试或审查步骤
4. **证据优先**: 在声称成功之前，运行命令并显示输出

### 常见陷阱

❌ 不要做:
- "这个太简单了，不需要设计" → 每个项目都需要 brainstorming
- "我先写代码，之后补测试" → TDD 顺序不能反
- "应该能工作了" → 先运行验证命令
- "快速修复一下，之后再调查" → 系统化调试更快

✅ 要做:
- 说"让我先调查一下"，然后遵循 systematic-debugging
- 说"让我设计一下"，然后遵循 brainstorming
- 运行验证命令，然后说"[输出显示] 所有测试通过"

### 查看已安装 Skills

```bash
ls ~/.claude/skills/
```

---

## 快速参考卡片

| 场景 | 使用哪个 Skill |
|------|---------------|
| 开始新项目/功能 | **brainstorming** → writing-plans |
| 实现功能 | **test-driven-development** |
| 遇到 bug | **systematic-debugging** |
| 需要隔离工作区 | **using-git-worktrees** |
| 有计划要执行 | **subagent-driven-development** 或 **executing-plans** |
| 声称完成前 | **verification-before-completion** |
| 完成开发后 | **finishing-a-development-branch** |
| 需要审查代码 | **requesting-code-review** + code-reviewer agent |
| 接收审查反馈 | **receiving-code-review** |
| 生成 Word/Excel/PDF/PPTX | **docx** / **xlsx** / **pdf** / **pptx** |
| 设计前端界面 | **frontend-design** |
| 构建 Claude API 应用 | **claude-api** |
| 构建 MCP 服务器 | **mcp-builder** |

---

*文档最后更新: 2026-04-10*
