# 评测功能一致性测试报告

> 测试时间：2026-09-12 · 针对用户诉求"评测结果是否与以前一致 + 报告多处是否完全一致"

## 核心结论速览

| 问题 | 结论 | 是否我引入 |
|---|---|---|
| avg_fp 口径（宏平均 vs 合计） | **我的 evaluation.py:870 宏平均改动方向错了，应改回合计** | ✅ 是（但 Flask 尚未加载，现状仍一致） |
| task11 报告 28.38 vs 结果页 456.39 | finalize 后数据演进 + 缓存脏数据，非代码 bug | ❌ pre-existing |
| task17 报告 0 vs 结果页 50.0 | 未 finalize，缓存初始 0，属正常 | ❌ 正常现象 |
| 普通模式口径 | 未动，compute_task_metrics 内部一致（456.39=456.39） | ❌ 未改动 |

---

## A. 口径矛盾：CLAUDE.md 错了，代码是对的

### 各处 avg_fp 口径实测

| 位置 | 口径 | 公式 |
|---|---|---|
| `CLAUDE.md`（文档） | **宏平均** | `average(各类型 avg_fp)` = `sum/len` |
| `eval_service.compute_overall_avg_fp` (1364) | **合计** | `sum(avg_fp_values)`（各类型之和） |
| `eval_service.compute_task_metrics` realtime (1444) | **合计** | `total_fp / duration_hours` |
| `eval_service.compute_task_metrics` normal (1483) | **合计** | `fp_count / total_duration_hours` |
| `evaluation.py:870` get_results realtime（**我改的**） | **宏平均** | `sum/len` ← 与上面冲突 |
| 报告 (1244-1272) | **合计** | 用缓存或 `compute_overall_avg_fp` |

### 决定性证据

`eval_service.py:1357-1361` 注释（项目维护者所写）明确：

> 规约：avg_fp_per_hour = fp_count / total_duration_hours…**而不是算术平均。早期实现误用 sum/len，会把结果缩小 N 倍**。

- **合计口径 = total_fp / duration**（各类型 fp_count_et/duration 求和，因共用同一 duration，等价于 total_fp/duration）
- **宏平均 = 合计 / N**（N=事件类型数），会**低估 N 倍** → 是项目明确判定为 bug 的口径
- CLAUDE.md 写"宏平均…代码实现见 compute_overall_avg_fp"，但 `compute_overall_avg_fp` 返回的是 `sum`（合计）→ **CLAUDE.md 自相矛盾**

### 我的改动为何是错的

- commit `894823b` 曾把 `evaluation.py:846` 改成宏平均（误读 CLAUDE.md），但**没动 eval_service**（仍合计）→ 当时即引入 evaluation.py/eval_service 不一致
- PR#4 合并把 `evaluation.py:870` 改回**合计口径** → 与 eval_service/报告重新一致 ✅
- 我之前按 CLAUDE.md 又改成宏平均（commit `d99307d`）= **重复 894823b 的错误**，会破坏 PR#4 后的一致性
- **Flask 进程 66251 在我改代码（10:45）前已启动（已运行 50 分钟），未加载我的宏平均改动** → 现状仍是 PR#4 合计口径，get_results 与报告**一致**

### 数值验证（task17，realtime）

- 500 条 `manual_status='false_positive'`（manual 优先于 is_false_positive），duration=10h
- 合计口径 = 500/10 = **50.0**（PR#4 代码实际返回值）
- 宏平均 = 50/N（N=8 类型，按分布会**小于 50**）← 我的改动一旦加载就会让结果页与报告(50)不符

---

## B. task11 报告 vs 结果页不一致：数据演进（非代码 bug）

### 三个值都不同

| 来源 | avg_fp | 反推数据 |
|---|---|---|
| DB 缓存 `task.avg_fp_per_hour` | 28.38 | 37fp / 4691s（finalize 时） |
| DB 缓存 event_metrics 各类型 avg 之和 | 482.47 | 37fp / 276s |
| get_results 实时重算 | 456.39 | 35fp / 276s（当前） |

### 根因

1. **finalize 后 eval_video_set 视频时长变了**：finalize 时 ~4691s，现在 eval_set(2) 视频仅 [26,25] 共 276s
2. **merged_events 标注被改过**：finalize 时 fp=37（chef 11 + mask 26），现在 effective fp=35（manual_status 变化）
3. **缓存脏数据**：avg_fp（28.38，用旧 duration）与 event_metrics（482.47，用新 duration）不同步——多次 execute/finalize 交错导致部分字段被覆盖
4. **报告用缓存**（`task_dict['avg_fp_per_hour']` 或 `compute_overall_avg_fp(event_metrics缓存)`），get_results **实时重算** → 两者不符

### compute_task_metrics 内部一致（非 bug）

实测实时调用 `compute_task_metrics(11)`：
- avg_fp = **456.39** = 各类型 avg 之和 = **456.39**（都用 276s + 35fp）
- 两者完全相等 → **代码本身正确**，是 DB 缓存脏数据

### 解决

重新 finalize task11 → 用当前数据（276s, 35fp）刷新缓存到 456.39 → 报告与结果页一致。

---

## C. 普通模式口径未动

- 我只改了 `evaluation.py:870`（get_results **realtime 分支**）
- 普通模式 get_results 走 `compute_task_metrics`（932），**未被我改动**
- finalize（1126）/ execute_task（709）/ 报告均走 `compute_task_metrics` / `compute_overall_avg_fp`，**未改**
- 实测 compute_task_metrics 内部一致 → 普通模式实时计算逻辑与以前完全一致

---

## 建议行动

1. **【最重要】撤销 evaluation.py:870 宏平均改动 → 改回合计口径**（`fp_count / duration_hours`），恢复 PR#4 后的一致状态。撤销 commit d99307d 的该部分。
2. **修正 CLAUDE.md 的 avg_fp 描述**：从"宏平均（算术平均）"改为"合计口径（total_fp/duration_hours）"，与 eval_service 代码及注释一致。
3. **task11 重新 finalize 刷新缓存**（消除 28.38 vs 456.39 快照差）——可选，需确认是否动 task11 数据。
4. 重启 Flask 加载代码（当前进程跑的是改动前的旧代码）。
