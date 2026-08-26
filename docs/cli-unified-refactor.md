# 统一 CLI 入口改造说明

> 改造日期：2026-08-14
> 相关提交：`e581066`（主体改造）、`41ba9f1`（recall 描述修正）
> 状态：本地 `main`，**未 push**

---

## 1. 背景与目标

改造前，项目脚本分散在两处，入口各自为政：

- `scripts/` 目录：水印、OCR、验证、推流、数据库清理等**正式功能脚本**
- 项目根目录：召回率、泄漏审计、报告生成等**分析报告脚本**

`process.py` 当时只覆盖水印三项（`--single` / `--batch` / `--install`），其余脚本需要记住路径单独运行（如 `python scripts/verify_alert.py ...`、`python compute_fight_recall.py`），新用户难以发现项目全部能力，也没有统一的 `--help`。

**目标**：把 `process.py` 改造为**子命令式统一入口**，收编正式功能 + 分析报告脚本，并提供一致的 `--help` 体验。

---

## 2. 设计方案

### 2.1 转发机制：subprocess 透传

`process.py` 改为子命令路由器，维护一张命令注册表。命中子命令后：

```python
subprocess.run([sys.executable, <项目根>/<脚本>, *透传参数])
# 退出码原样回传
```

选择 subprocess 转发而非 import 调用，原因：

1. **沿用项目既有模式**：改造前 `process.py` 就是用 subprocess 调 `scripts/process_single.py`，这是既定模式。
2. **sibling import 天然可用**：subprocess 执行脚本时，脚本所在目录在 `sys.path[0]`，因此 `batch_process → process_single`、`stream_merged → stream_fight_loop`、`verify_alert → ocr_easy` 这些同级 import 不受影响。
3. **零逻辑重复**：不重新实现各脚本的参数解析与业务逻辑，退出码、相对路径行为与改造前完全一致。

### 2.2 脚本文件保持原位（关键约束）

Web 服务**直接 import** 脚本模块：

- `app/services/verification_service.py:12` → `import ocr_easy`
- `app/services/watermark_service.py:19` → `from process_single import ...`

因此 `scripts/` 下的脚本**不能移动、不能改名**，否则破坏 web 导入。统一 CLI 只是在其上新增一层入口，不改动任何脚本文件。

---

## 3. 子命令列表（共 19 项）

运行 `python process.py` 可查看按分组排列的完整列表。

| 分组 | 子命令 | 转发脚本 | 说明 |
|---|---|---|---|
| 环境 | `install` | （内置） | 安装 Python 依赖（pip install -r requirements.txt） |
| 水印 | `watermark <视频>` | scripts/process_single.py | 给单个视频添加水印 |
| 水印 | `watermark-batch` | scripts/batch_process.py | 批量给所有视频添加水印 |
| OCR | `ocr <图片>` | scripts/ocr_easy.py | EasyOCR 识别截图水印 |
| OCR | `ocr-paddle <图片>` | scripts/final_ocr.py | PaddleOCR 识别截图水印 |
| 验证 | `verify [图片] [--batch]` | scripts/verify_alert.py | 验证告警图片是否命中 ground truth |
| 推流 | `stream <视频...> --stream <名>` | scripts/stream_videos.py | 按顺序推流到 MediaMTX（RTSP） |
| 推流 | `stream-fight` | scripts/stream_fight_loop.py | Fight/NonFight 拼接循环推流 |
| 推流 | `stream-merged` | scripts/stream_merged_sources.py | 同源片段合并后推流 |
| 数据库 | `db-fix-duplicates` | scripts/fix_duplicate_video_ids.py | 清理 videos 表重复 video_id |
| 分析报告 | `recall` | compute_fight_recall.py | 计算推流测试集召回率（按视频统计告警命中/漏报，输出汇总 CSV 与 Markdown 报告） |
| 分析报告 | `recall-audit` | independent_recall_audit.py | 独立召回审计 |
| 分析报告 | `leakage` | leakage_audit.py | 泄漏审计 |
| 分析报告 | `leakage-v2` | leakage_audit_v2.py | 泄漏审计 v2 |
| 分析报告 | `detection-report` | gen_detection_report.py | 生成检测报告 |
| 分析报告 | `retest-report` | gen_retest_report.py | 生成复测报告 |
| 分析报告 | `algo-condition` | check_algo_condition.py | 查看 AIBOX 算法 condition 参数 |
| 分析报告 | `annotate-alarms` | annotate_alarm_images.py | 标注告警图片目标框 |
| 分析报告 | `md2pdf` | md_to_pdf.py | Markdown 转 PDF |

> **排除**（临时调试 / 一次性迁移，未收编）：`_diag*.py`、`annotate_compare_104511.py`、`annotate_compare_110441.py`、`migrate_gt_plus5.py`。这些脚本仍可单独运行，只是不进入统一入口。

### 关于 `recall` 的说明

`recall` 转发的脚本文件名是 `compute_fight_recall.py`（当前数据集为 fight），但其功能是**通用的推流测试集召回率统计**：读推流统计 CSV + AIBOX 告警元数据 → 按视频统计告警命中/漏报 → 算召回率 → 输出汇总 CSV 与 Markdown 报告。命令描述已改为通用表述（提交 `41ba9f1`），不限于打架任务。脚本文件名未改（改名会牵连引用，不在本次范围）。

---

## 4. 使用方式

### 4.1 查看帮助

```bash
python process.py                      # 顶层：按分组列出全部子命令
python process.py <命令> --help        # 查看某命令的参数
```

### 4.2 `--help` 三级行为

| 命令类型 | `--help` 行为 | 例子 |
|---|---|---|
| 带 argparse 的命令（7 个） | 透传给脚本，显示**原生完整参数帮助** | `python process.py verify --help` |
| 无 argparse 的命令（11 个） | 由 `process.py` **拦截**，打印入口说明（目标脚本 + 用法提示），**不报错** | `python process.py recall --help` |
| `install`（内置） | 打印安装说明 | `python process.py install --help` |

### 4.3 运行示例

```bash
# 水印
python process.py watermark video1/046-3.30-18:16.mp4
python process.py watermark-batch

# 验证（真实 OCR）
python process.py verify report/402_1774925112_103.png

# 验证（mock OCR，绕过 EasyOCR，用于无 GPU/无模型环境测试链路）
python process.py verify report/402_1774925112_103.png --mock-ocr '{"video_id": "046", "timestamp_seconds": 90}'

# OCR
python process.py ocr report/402_1774925112_103.png
```

### 4.4 向后兼容（旧标志自动映射）

旧接口 `--single` / `--batch` / `--install` 仍可用，自动映射到新子命令并向 stderr 打印一行弃用提示：

| 旧写法 | 等价新写法 |
|---|---|
| `python process.py --single <file>` | `python process.py watermark <file>` |
| `python process.py --batch` | `python process.py watermark-batch` |
| `python process.py --install` | `python process.py install` |

---

## 5. PowerShell 下传 JSON 的注意事项

`verify --mock-ocr` 需要传一个 JSON 参数。PowerShell 对双引号的处理与 bash 不同，bash 的纯单引号写法在 PowerShell 下会被"吃掉"双引号导致参数丢失（报 `argument --mock-ocr: expected one argument`）。

**PowerShell 可靠写法**：单引号包裹 + 双引号转义为 `\"`

```powershell
py process.py verify test_1774925112_117.png --mock-ocr '{\"video_id\":\"046\",\"timestamp_seconds\":90}'
```

**bash / macOS / Linux 写法**（README/CLAUDE.md 中的标准示例）：

```bash
python process.py verify report/402_1774925112_103.png --mock-ocr '{"video_id": "046", "timestamp_seconds": 90}'
```

> 注：本机 `python` 可能是 Windows Store 的 stub（退出码 9009），可用 `py` launcher 代替。

---

## 6. 改动文件清单

仅改动 3 个文件，**未触碰** `scripts/` 与 `app/`：

| 文件 | 改动 |
|---|---|
| `process.py` | 重写为子命令路由器：命令注册表、`run_script()` 转发、subparsers + `parse_known_args` 透传、`--help` 拦截、旧标志兼容 shim；保留原 `install_deps()` |
| `README.md` | 更新「命令行使用」节为子命令示例；新增子命令速查表；更新目录说明中 `process.py` 的描述 |
| `CLAUDE.md` | 更新「Common Commands」的 CLI 示例为子命令；新增「Unified CLI」说明段；修正 mock-ocr 注释（跳过 OCR 分支，与 GPU 无关，EasyOCR 用 `gpu=False`） |

---

## 7. 验证结果

冒烟测试（均通过）：

| 测试项 | 命令 | 预期结果 |
|---|---|---|
| 顶层帮助 | `py process.py` | 按 6 组列出 19 个命令 |
| 非法命令 | `py process.py badcmd` | argparse 报 invalid choice，退出码 2 |
| 透传帮助 | `py process.py verify --help` | 显示 verify_alert.py 原生参数帮助 |
| 拦截帮助 | `py process.py recall --help` | 打印入口说明，退出码 0，不报错 |
| 旧标志兼容 | `py process.py --single <file>` | stderr 打印弃用提示，转发到 watermark |
| 端到端 | `py process.py verify <图> --mock-ocr '{...}'` | 输出验证结果 JSON，退出码 0 |
| 改动范围 | `git show --stat HEAD` | 仅 3 文件，`scripts/`、`app/` 未动 |

### 端到端验证说明

用 `New-Item` 建的空文件 `test_1774925112_117.png` + `--mock-ocr` 跑通整条链路：

- 文件名末尾 `_117.png` → 正则提取告警类型 ID = `117`
- 查 `config/alert_types.json` → `117` 对应事件类型 `fight`
- mock 提供 `video_id=046`、`timestamp_seconds=90`，**跳过真实 EasyOCR**（不 import、不下载模型）
- 找 `ground_truth/046.json` 不存在 → `verdict: unknown`（预期，因无 ground truth）

> **`fight` 来自文件名里的 `117`（查表），不是图片内容识别。** 真实使用时需有真实告警截图 + 对应 `ground_truth/{video_id}.json`，才会得到 `correct` / `incorrect` 判定。

---

## 8. Git 提交记录与回滚

改造前先建立了可回滚的存档提交点（遵守 CLAUDE.md「提交前征得同意」规则）：

| commit | 类型 | 内容 |
|---|---|---|
| `239b264` | chore | 改造前存档点（`git add -u` 提交当时已跟踪文件改动） |
| `e581066` | feat | 统一 CLI 入口主体改造（process.py + README.md + CLAUDE.md） |
| `41ba9f1` | fix | 修正 recall 子命令描述为通用「推流测试集召回率」 |

以上提交均在本地 `main`，**未 push**。

### 回滚方式

- 若要回退统一 CLI 改造（保留存档点 `239b264`）：
  ```bash
  git revert e581066 41ba9f1     # 新建反向提交
  # 或硬回退（丢弃这两个提交）：
  # git reset --hard 239b264
  ```
- `process.py` / `README.md` / `CLAUDE.md` 在存档点 `239b264` 是干净的 HEAD 版本，改造前的原始状态可随时恢复。
