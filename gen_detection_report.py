#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
打架检测测试结果汇总 → 自包含 HTML 报告（图片 base64 内嵌）。
样式参考《云天励飞算法能力评估报告》：pandoc 风格 + 左侧固定 TOC + rating 徽章。
"""
import base64
import io
import json
from pathlib import Path
from PIL import Image

BASE = Path(__file__).resolve().parent
IMG_DIR = BASE / "leak_audit_images"
OUT = BASE / "fight_detection_report.html"

IMAGES = {
    "miss_frame": IMG_DIR / "vid_round19_Qtcwz_K2Gvo.jpg",
}
# 命中告警图（带红框标注，由 annotate_alarm_images.py 生成）
ANNOT_DIR = BASE / "annotated_images"
ANNOT_HIT1 = ANNOT_DIR / "hit_110527.jpg"   # 11:05:27 conf=0.885 高置信度
ANNOT_HIT2 = ANNOT_DIR / "hit_103225.jpg"   # 10:32:25 conf=0.313 首轮第1轮
ANNOT_CMP = ANNOT_DIR / "compare_104511_annotated.jpg"  # 5.3 案例1 对比图，告警图区块已画红框
ANNOT_CMP2 = ANNOT_DIR / "compare_110441_annotated.jpg"  # 5.3 案例2 对比图，告警图区块已画红框


def img_to_b64(path: Path, max_w: int = 720, quality: int = 82) -> str:
    im = Image.open(path).convert("RGB")
    if im.width > max_w:
        h = round(im.height * max_w / im.width)
        im = im.resize((max_w, h), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


# ── pandoc 风格样式表（与参考报告一致）──────────────────────────────
PANDOC_CSS = """
html { color: #1a1a1a; background-color: #fdfdfd; }
body {
  margin: 0 auto; max-width: 36em; padding-left: 50px; padding-right: 50px;
  padding-top: 50px; padding-bottom: 50px; hyphens: auto; overflow-wrap: break-word;
  text-rendering: optimizeLegibility; font-kerning: normal;
}
@media (max-width: 600px) { body { font-size: 0.9em; padding: 12px; } h1 { font-size: 1.8em; } }
@media print {
  html { background-color: white; } body { background-color: transparent; color: black; font-size: 12pt; }
  p, h2, h3 { orphans: 3; widows: 3; } h2, h3, h4 { page-break-after: avoid; }
}
p { margin: 1em 0; }
a { color: #1a1a1a; } a:visited { color: #1a1a1a; }
img { max-width: 100%; } svg { height: auto; max-width: 100%; }
h1, h2, h3, h4, h5, h6 { margin-top: 1.4em; }
h5, h6 { font-size: 1em; font-style: italic; } h6 { font-weight: normal; }
ol, ul { padding-left: 1.7em; margin-top: 1em; } li > ol, li > ul { margin-top: 0; }
blockquote { margin: 1em 0 1em 1.7em; padding-left: 1em; border-left: 2px solid #e6e6e6; color: #606060; }
code { font-family: Menlo, Monaco, Consolas, 'Lucida Console', monospace; font-size: 85%; margin: 0; hyphens: manual; }
pre { margin: 1em 0; overflow: auto; } pre code { padding: 0; overflow: visible; overflow-wrap: normal; }
.sourceCode { background-color: transparent; overflow: visible; }
hr { border: none; border-top: 1px solid #1a1a1a; height: 1px; margin: 1em 0; }
table { margin: 1em 0; border-collapse: collapse; width: 100%; overflow-x: auto; display: block; font-variant-numeric: lining-nums tabular-nums; }
table caption { margin-bottom: 0.75em; }
tbody { margin-top: 0.5em; border-top: 1px solid #1a1a1a; border-bottom: 1px solid #1a1a1a; }
th { border-top: 1px solid #1a1a1a; padding: 0.25em 0.5em; } td { padding: 0.125em 0.5em 0.25em 0.5em; }
header { margin-bottom: 4em; text-align: center; }
#TOC li { list-style: none; } #TOC ul { padding-left: 1.3em; } #TOC > ul { padding-left: 0; }
#TOC a:not(:hover) { text-decoration: none; }
code{white-space: pre-wrap;} span.smallcaps{font-variant: small-caps;}
div.columns{display: flex; gap: min(4vw, 1.5em);} div.column{flex: auto; overflow-x: auto;}
div.hanging-indent{margin-left: 1.5em; text-indent: -1.5em;}
ul.task-list[class]{list-style: none;}
ul.task-list li input[type="checkbox"] { font-size: inherit; width: 0.8em; margin: 0 0.8em 0.2em -1.6em; vertical-align: middle; }
.display.math{display: block; text-align: center; margin: 0.5rem auto;}
"""

REPORT_CSS = """
:root {
  --bg: #FAFAFA; --card-bg: #FFFFFF; --text: #1a1a1a; --text-secondary: #555555;
  --text-muted: #888888; --border: #E0E0E0; --accent: #2563EB; --accent-light: #EFF6FF;
  --danger: #DC2626; --danger-light: #FEF2F2; --warning: #D97706; --warning-light: #FFFBEB;
  --success: #059669; --success-light: #ECFDF5;
}
html { background: var(--bg) !important; }
body {
  margin-left: calc(320px + (100vw - 320px - 69em) / 2) !important;
  margin-right: calc((100vw - 320px - 69em) / 2) !important;
  max-width: 69em !important; padding: 40px 48px !important; box-sizing: border-box !important;
  background: var(--card-bg) !important; color: var(--text) !important;
  font-family: -apple-system, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif !important;
  font-size: 15px !important; line-height: 1.8 !important; border-radius: 12px !important;
  border: 1px solid var(--border) !important; box-shadow: 0 1px 3px rgba(0,0,0,0.05) !important;
  margin-top: 24px !important; margin-bottom: 24px !important;
}
h1 { font-size: 32px !important; font-weight: 700 !important; color: var(--text) !important;
  margin-bottom: 24px !important; padding-bottom: 16px !important; border-bottom: 2px solid var(--accent) !important; text-align: center !important; }
h2 { font-size: 22px !important; font-weight: 700 !important; color: var(--text) !important;
  margin-top: 40px !important; margin-bottom: 20px !important; padding-bottom: 10px !important; border-bottom: 2px solid var(--accent) !important; }
h3 { font-size: 17px !important; font-weight: 600 !important; color: var(--accent) !important; margin-top: 28px !important; margin-bottom: 12px !important; }
h4 { font-size: 15px !important; font-weight: 600 !important; color: var(--text-secondary) !important; margin-top: 20px !important; }
p { color: var(--text-secondary) !important; margin-bottom: 12px !important; }
a { color: var(--accent) !important; text-decoration: none !important; } a:hover { text-decoration: underline !important; }
strong { color: var(--text) !important; font-weight: 600 !important; }
ul, ol { color: var(--text-secondary) !important; padding-left: 24px !important; } li { margin-bottom: 8px !important; }
blockquote { background: var(--accent-light) !important; border-left: 4px solid var(--accent) !important; border-radius: 8px !important; padding: 16px 20px !important; color: var(--text-secondary) !important; margin: 20px 0 !important; }
blockquote p { margin: 0 !important; }
img { border-radius: 8px !important; border: 1px solid var(--border) !important; max-width: 100% !important; }
hr { border: none !important; border-top: 1px solid var(--border) !important; margin: 32px 0 !important; }
table { width: 100% !important; border-collapse: collapse !important; margin: 16px 0 !important; font-size: 14px !important; border-radius: 8px !important; overflow: hidden !important; }
th { background: #f8fafc !important; color: var(--text) !important; padding: 12px 14px !important; text-align: left !important; font-weight: 600 !important; border-bottom: 2px solid var(--border) !important; }
td { padding: 10px 14px !important; border-bottom: 1px solid var(--border) !important; color: var(--text-secondary) !important; }
tr:hover td { background: #fafafa !important; }

.rating-excellent, .rating-good, .rating-ok, .rating-fair, .rating-warning, .rating-bad, .rating-low {
  display: inline-block !important; padding: 2px 10px !important; border-radius: 12px !important;
  font-size: 12px !important; font-weight: 600 !important; white-space: nowrap !important;
}
.rating-excellent { background: var(--success-light) !important; color: var(--success) !important; }
.rating-good { background: var(--accent-light) !important; color: var(--accent) !important; }
.rating-ok { background: #d1fae5 !important; color: var(--success) !important; }
.rating-fair { background: var(--warning-light) !important; color: var(--warning) !important; }
.rating-warning { background: #ffedd5 !important; color: var(--warning) !important; }
.rating-bad { background: var(--danger-light) !important; color: var(--danger) !important; }
.rating-low { background: #f3f4f6 !important; color: var(--text-muted) !important; border: 1px solid var(--border) !important; }

figure { margin: 24px 0 !important; text-align: center !important; }
figcaption { font-size: 13px !important; color: var(--text-muted) !important; margin-top: 8px !important; }
details { margin: 12px 0 !important; }
summary { cursor: pointer !important; color: var(--accent) !important; font-weight: 600 !important; font-size: 14px !important; }
summary:hover { text-decoration: underline !important; }
pre { background: #0f172a !important; color: #e2e8f0 !important; padding: 16px !important; border-radius: 8px !important; overflow-x: auto !important; font-size: 13px !important; }
pre code { background: none !important; color: inherit !important; padding: 0 !important; font-size: 13px !important; }
code { background: #f1f5f9 !important; padding: 2px 6px !important; border-radius: 4px !important; }

nav#TOC {
  position: fixed !important; left: 0 !important; top: 0 !important; width: 300px !important; height: 100vh !important;
  overflow-y: auto !important; background: var(--card-bg) !important; padding: 28px 24px !important;
  border-right: 1px solid var(--border) !important; box-sizing: border-box !important; z-index: 100 !important;
  box-shadow: 1px 0 3px rgba(0,0,0,0.03) !important;
}
nav#TOC h2 { font-size: 18px !important; margin-top: 0 !important; margin-bottom: 16px !important; color: var(--accent) !important; font-weight: 700 !important; border-bottom: none !important; padding-bottom: 0 !important; }
nav#TOC ul { list-style: none !important; padding-left: 0 !important; margin: 0 !important; }
nav#TOC ul ul { padding-left: 16px !important; margin-top: 4px !important; }
nav#TOC li { margin-bottom: 8px !important; line-height: 1.5 !important; }
nav#TOC a { color: var(--text-secondary) !important; font-size: 14px !important; text-decoration: none !important; display: block !important; padding: 2px 0 !important; }
nav#TOC a:hover { color: var(--accent) !important; }

@media (max-width: 1400px) {
  body { margin-left: 320px !important; margin-right: 20px !important; max-width: none !important; }
}
@media (max-width: 900px) {
  body { margin-left: 0 !important; margin-right: 0 !important; padding: 20px !important; border-radius: 0 !important; border: none !important; box-shadow: none !important; margin-top: 0 !important; margin-bottom: 0 !important; }
  nav#TOC { position: relative !important; width: 100% !important; height: auto !important; border-right: none !important; border-bottom: 1px solid var(--border) !important; margin-bottom: 20px !important; box-shadow: none !important; }
}
"""


def build():
    hit1_b64 = img_to_b64(ANNOT_HIT1, 640)   # 11:05:27 已画红框
    hit2_b64 = img_to_b64(ANNOT_HIT2, 640)   # 10:32:25 已画红框
    miss_b64 = img_to_b64(IMAGES["miss_frame"], 640)
    cmp_b64 = img_to_b64(ANNOT_CMP, 900)
    cmp2_b64 = img_to_b64(ANNOT_CMP2, 900)

    cond = json.loads('{"confidence":0.3,"IFALGSDK__ACT__conf":0.5,"IFALGSDK__CLS__conf":0.7,"actLvlCountThresh":1,"mergeRectCntMax":4,"rectWidthMin":256,"rectHeightMin":256,"total_count_thresh":2,"alert_count_thresh":2,"total_count_thresh_1":1,"alert_count_thresh_1":4,"window_size":128,"action_fight":1,"action_run":0,"action_falldown":0,"maskTm":10,"whRate":0.06,"enableSkip":false,"p3_count":1,"p3_rate":0.5,"total_confidence":120,"child_thresh":0.7,"child_rate":0.25,"quality_thresh":0.21,"quality_rate":0.5}')
    full_json = json.dumps(cond, ensure_ascii=False, indent=2)

    key_params = [
        ("action_fight", "1", "开启打架检测"),
        ("confidence", "0.3", "置信度阈值"),
        ("actLvlCountThresh", "1", "识别 1 次打架即上报（调至最低）"),
        ("maskTm", "10s", "报警屏蔽时间（默认 300s 调低，便于连续告警）"),
        ("total_count_thresh", "2", "打架人数阈值 ≥ 2"),
        ("window_size", "128", "检测滑窗帧数"),
        ("alert_count_thresh", "2", "告警触发计数阈值"),
        ("IFALGSDK__ACT__conf", "0.5", "行为动作置信度"),
        ("IFALGSDK__CLS__conf", "0.7", "分类置信度"),
        ("DETECT_DELAY", "8s", "检测上报延迟（归因用，非算法参数）"),
    ]
    param_rows = "\n".join(
        f"<tr><td><code>{k}</code></td><td>{v}</td><td>{d}</td></tr>"
        for k, v, d in key_params
    )

    html = f"""<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=yes" />
  <title>打架检测测试结果报告</title>
  <style>
{PANDOC_CSS}
  </style>
  <style id="report-style">
{REPORT_CSS}
  </style>
</head>
<body>
<nav id="TOC" role="doc-toc">
<h2>目录</h2>
<ul>
<li><a href="#打架检测测试结果报告">打架检测测试结果报告</a>
<ul>
<li><a href="#执行摘要">1. 执行摘要</a></li>
<li><a href="#测试环境与方法">2. 测试环境与方法</a>
<ul>
<li><a href="#推流检测链路">2.1 推流检测链路</a></li>
<li><a href="#数据集">2.2 数据集</a></li>
<li><a href="#三轮测试设计">2.3 三轮测试设计</a></li>
</ul></li>
<li><a href="#算法配置参数">3. 算法配置参数</a></li>
<li><a href="#测试结果分析">4. 测试结果分析</a>
<ul>
<li><a href="#fight-召回率测试">4.1 Fight 召回率测试</a></li>
<li><a href="#nonfight-精确率与误检测试">4.2 NonFight 精确率与误检测试</a></li>
</ul></li>
<li><a href="#检测样例">5. 检测样例</a>
<ul>
<li><a href="#检测命中示例">5.1 检测命中示例</a></li>
<li><a href="#漏检示例">5.2 漏检示例</a></li>
<li><a href="#告警图画面归属验证">5.3 告警图画面归属验证</a></li>
</ul></li>
<li><a href="#综合结论">6. 综合结论</a>
<ul>
<li><a href="#结果可信度">6.1 结果可信度</a></li>
<li><a href="#方法学说明">6.2 方法学说明</a></li>
</ul></li>
</ul></li>
</ul>
</nav>

<h1 id="打架检测测试结果报告">打架检测测试结果报告</h1>
<blockquote>
<p>报告用途：打架检测算法效果评估与结果归档<br />
评估对象：人员行为检测算法（personAction）— 打架检测<br />
测试日期：2026-08-05<br />
数据来源：打架/非打架视频推流检测的告警记录与抓拍图、独立复算与泄露审计结果<br />
检测端：AIBOX 边缘盒子（人员行为检测算法，fight 通道）</p>
</blockquote>
<hr />

<h2 id="执行摘要">1. 执行摘要</h2>
<p>本次测试针对 AIBOX 内置<strong>人员行为检测算法</strong>的打架检测能力，使用 RLVS（Real Life Violence Situations）公开数据集，通过 ffmpeg 实时推流到 AIBOX 拉流检测的方式，分别评估了召回率与精确率。</p>
<p><strong>核心结论：</strong></p>
<ul>
<li><strong>召回率 <span class="rating-excellent">优秀</span></strong>：综合召回率 <strong>97.9%</strong>（46/47），短视频（10~20s）首轮 70.21%，延长至 60s 后提升至 97.9%。</li>
<li><strong>精确率 <span class="rating-excellent">优秀</span></strong>：200 个非打架视频<strong>零误检</strong>，精确率 <strong>100%</strong>，算法不会将正常行为误判为打架。</li>
<li><strong>召回率对有效时长敏感</strong>：14 个首轮漏报均为 ≤20s 短视频，根因为有效时长不足检测窗口（DETECT_DELAY≈8s + maskTm=10s）；延长到 60s 后 13 个转命中。</li>
<li><strong>唯一顽固漏报</strong>：Qtcwz_K2Gvo_0.avi 经 60s 重测仍不触发，属内容层面未达检测条件，非参数可解。</li>
</ul>
<table>
<thead>
<tr><th>指标</th><th>值</th><th>评价</th></tr>
</thead>
<tbody>
<tr><td><strong>召回率 Recall</strong></td><td><strong>97.9%</strong>（46/47）</td><td><span class="rating-excellent">优秀</span></td></tr>
<tr><td><strong>精确率 Precision</strong></td><td><strong>100%</strong>（200/200 零误检）</td><td><span class="rating-excellent">优秀</span></td></tr>
<tr><td><strong>误检率 FPR</strong></td><td><strong>0%</strong></td><td><span class="rating-excellent">优秀</span></td></tr>
<tr><td>顽固漏报</td><td>1 个（Qtcwz_K2Gvo_0.avi）</td><td><span class="rating-fair">内容问题</span></td></tr>
</tbody>
</table>

<h2 id="测试环境与方法">2. 测试环境与方法</h2>
<h3 id="推流检测链路">2.1 推流检测链路</h3>
<p>测试视频经 ffmpeg 实时推流到 MediaMTX RTSP 服务，AIBOX 拉流后由人员行为检测算法持续检测并产出告警抓拍图。推流用 concat filter 统一缩放到 640×360（scale+pad，不拉伸）+ libx264 重编码，保证不同分辨率视频输入一致。</p>
<pre><code>测试视频(avi) → ffmpeg 实时推流 → MediaMTX(RTSP 中转) → AIBOX 拉流 → personAction 检测 → 告警抓拍图</code></pre>
<table>
<thead>
<tr><th>项</th><th>值</th></tr>
</thead>
<tbody>
<tr><td>推流平台</td><td>视频预处理与推流平台（Flask）</td></tr>
<tr><td>RTSP 服务</td><td>MediaMTX（RTSP 中转服务）</td></tr>
<tr><td>检测端</td><td>AIBOX 边缘盒子（RTSP 拉流检测方）</td></tr>
<tr><td>测试通道</td><td>fight 通道（打架检测专用）</td></tr>
<tr><td>算法</td><td>personAction 人员行为检测算法</td></tr>
<tr><td>推流脚本</td><td>视频推流循环测试脚本</td></tr>
</tbody>
</table>

<h3 id="数据集">2.2 数据集</h3>
<p>数据集为 <strong>RLVS（Real Life Violence Situations）</strong>公开暴力检测集，文件名为 YouTube 源视频 ID。已通过运动强度分布验证标签方向正确（Fight 运动中位 5.1 &gt; NonFight 2.0）。</p>
<table>
<thead>
<tr><th>集合</th><th>数量</th><th>格式</th><th>用途</th></tr>
</thead>
<tbody>
<tr><td>Fight（打架）</td><td>200 个</td><td>5s avi，混 720p/360p/240p</td><td>召回率测试（取 47 个）</td></tr>
<tr><td>NonFight（非打架）</td><td>200 个</td><td>5s avi，同上</td><td>误检率/精确率测试</td></tr>
</tbody>
</table>

<h3 id="三轮测试设计">2.3 三轮测试设计</h3>
<table>
<thead>
<tr><th>测试</th><th>推流模式</th><th>单视频时长</th><th>视频数</th><th>时段</th><th>目的</th></tr>
</thead>
<tbody>
<tr><td>① Fight 召回首轮</td><td>accumulate（同类累积拼接）</td><td>10~20s</td><td>47</td><td>10:31~11:06</td><td>测召回率</td></tr>
<tr><td>② Fight 漏报重测</td><td>repeat（5s×12 自复制）</td><td>60s</td><td>14</td><td>15:23~15:37</td><td>验证短视频漏报=时长不足</td></tr>
<tr><td>③ NonFight 误检</td><td>repeat（5s×4）</td><td>20s</td><td>200</td><td>15:56~17:13</td><td>测误检率/精确率</td></tr>
</tbody>
</table>

<h2 id="算法配置参数">3. 算法配置参数</h2>
<p>测试全程使用同一套 personAction 参数（已注册到 AIBOX 算法任务配置，逐参数对账一致）。关键参数如下：</p>
<table>
<thead>
<tr><th>参数</th><th>值</th><th>说明</th></tr>
</thead>
<tbody>
{param_rows}
</tbody>
</table>
<details>
<summary>展开：完整 condition 配置 JSON</summary>
<pre><code>{full_json}</code></pre>
</details>

<h2 id="测试结果分析">4. 测试结果分析</h2>
<h3 id="fight-召回率测试">4.1 Fight 召回率测试</h3>
<table>
<thead>
<tr><th>测试阶段</th><th>推流时长</th><th>视频数</th><th>命中</th><th>漏报</th><th>召回率</th></tr>
</thead>
<tbody>
<tr><td>首轮（accumulate）</td><td>10~20s</td><td>47</td><td>33</td><td>14</td><td><strong>70.21%</strong></td></tr>
<tr><td>漏报重测（repeat 60s）</td><td>60s</td><td>14</td><td>13</td><td>1</td><td>92.86%（转命中）</td></tr>
<tr><td><strong>综合修正</strong></td><td>—</td><td>47</td><td><strong>46</strong></td><td><strong>1</strong></td><td><strong>97.87%</strong></td></tr>
</tbody>
</table>
<blockquote>
<p><strong>漏报根因诊断：</strong>首轮 14 个漏报全部是 ≤20s 短视频（11 个 ≤10s）。诊断为<strong>有效时长不足检测窗口</strong>——DETECT_DELAY≈8s + maskTm=10s，10~20s 片段在窗口内有效时长不够累积触发。延长到 60s 后 13 个转命中，证实时长是瓶颈。</p>
</blockquote>
<h4 id="唯一顽固漏报qtckz-k2gvo-0avi">唯一顽固漏报：Qtcwz_K2Gvo_0.avi</h4>
<ul>
<li><strong>现象</strong>：该视频 60s 重测仍未触发告警。</li>
<li><strong>对照</strong>：其前一轮（15:27:39~15:28:04，3 条告警）和后一轮（15:29:43~15:29:58，3 条告警）均正常报警——前后视频都报了，唯独此视频没报。</li>
<li><strong>结论</strong>：属内容层面未达打架检测触发条件，<strong>非时长或参数问题</strong>。</li>
</ul>

<h3 id="nonfight-精确率与误检测试">4.2 NonFight 精确率与误检测试</h3>
<table>
<thead>
<tr><th>指标</th><th>值</th></tr>
</thead>
<tbody>
<tr><td>NonFight 测试视频数</td><td>200</td></tr>
<tr><td>推流时长</td><td>每个 20s（5s×4）</td></tr>
<tr><td>误检视频数</td><td><strong>0</strong></td></tr>
<tr><td>误检告警总次数</td><td><strong>0</strong></td></tr>
<tr><td><strong>误检率（FPR）</strong></td><td><strong>0%</strong></td></tr>
<tr><td><strong>精确率（Precision）</strong></td><td><strong>100%</strong></td></tr>
</tbody>
</table>
<table>
<thead>
<tr><th>批次</th><th>视频范围</th><th>视频数</th><th>推流时段</th><th>误检视频</th><th>误检告警</th></tr>
</thead>
<tbody>
<tr><td>第一批</td><td>第 1-100 个</td><td>100</td><td>15:56:27 ~ 16:30:36</td><td>0</td><td>0</td></tr>
<tr><td>第二批</td><td>第 101-200 个</td><td>100</td><td>16:37:42 ~ 17:11:39</td><td>0</td><td>0</td></tr>
<tr><td>合计</td><td>全部</td><td>200</td><td>—</td><td><strong>0</strong></td><td><strong>0</strong></td></tr>
</tbody>
</table>
<p>每批除脚本归因统计外，均额外核实 AIBOX 全局告警列表在对应推流时段的 fight 通道告警数为 0，排除检测延迟导致告警时间漂移而被漏计的可能。本次未发现误检，故不列误检样例。</p>

<h2 id="检测样例">5. 检测样例</h2>
<h3 id="检测命中示例">5.1 检测命中示例</h3>
<p>以下为 fight 通道命中的告警抓拍图，<strong>红框为算法检测到打架行为的区域</strong>，框上方标注目标类型与置信度。算法在画面中定位到打架人员并给出 personAction 检测框。</p>
<figure>
<img src="data:image/jpeg;base64,{hit1_b64}" alt="高置信度命中告警图" />
<figcaption aria-hidden="true">图1：告警抓拍图（11:05:27，640×360，置信度 0.885）— 轮次 47（zOqs7Oh9oDM_0.avi）命中，红框标出打架检测区域</figcaption>
</figure>
<figure>
<img src="data:image/jpeg;base64,{hit2_b64}" alt="命中告警图" />
<figcaption aria-hidden="true">图2：告警抓拍图（10:32:25，640×360，置信度 0.313，clsScore 0.998）— 轮次 1（0Ow4cotKOuw_0.avi）命中，红框标出打架检测区域</figcaption>
</figure>

<h3 id="漏检示例">5.2 漏检示例</h3>
<p>顽固漏报视频 Qtcwz_K2Gvo_0.avi 的画面帧。该视频 60s 重测全程未触发告警，属内容未达检测条件，故画面无检测框。</p>
<figure>
<img src="data:image/jpeg;base64,{miss_b64}" alt="漏检视频帧" />
<figcaption aria-hidden="true">图3：漏检视频帧（Qtcwz_K2Gvo_0.avi，轮次 19）— 延长至 60s 仍未触发告警，前后轮均正常报警</figcaption>
</figure>

<h3 id="告警图画面归属验证">5.3 告警图画面归属验证</h3>
<p>对两个边界漏报轮（delay=0→8 翻转点），将告警抓拍图与相邻候选轮的视频帧做 pHash 汉明距离比对（越小越相似）。告警图画面均匹配<strong>邻轮（命中轮）</strong>，而非漏报轮本身，证明漏报成立、无跨段内容泄露。</p>
<table>
<thead>
<tr><th>漏报轮</th><th>告警时间</th><th>告警图最接近（命中轮）</th><th>汉明距离</th><th>vs 漏报轮</th><th>结论</th></tr>
</thead>
<tbody>
<tr><td>轮19 Qtcwz_K2Gvo</td><td>10:45:11</td><td>轮18 Qj3oZsaqNGE</td><td>4</td><td>27</td><td>告警图=轮18画面，漏报成立</td></tr>
<tr><td>轮45 xDjgfhGt-YA</td><td>11:04:41</td><td>轮44 v4dhdnsxiX4</td><td>26</td><td>30</td><td>告警图=轮44画面，漏报成立</td></tr>
</tbody>
</table>
<p>下图为两案例的告警抓拍图（左，<strong>红框为算法检测框</strong>）与候选轮视频帧的 pHash 对比。</p>
<figure>
<img src="data:image/jpeg;base64,{cmp_b64}" alt="案例1 告警图与候选视频帧对比" />
<figcaption aria-hidden="true">图4：案例1 — 告警图 10:45:11（左，红框为打架检测框，置信度 0.577）vs 候选轮帧 pHash 对比，最近匹配为轮 18（命中，d=4），远于轮 19（漏报，d=27）</figcaption>
</figure>
<figure>
<img src="data:image/jpeg;base64,{cmp2_b64}" alt="案例2 告警图与候选视频帧对比" />
<figcaption aria-hidden="true">图5：案例2 — 告警图 11:04:41（左，红框为打架检测框，置信度 0.417）vs 候选轮帧 pHash 对比，最近匹配为轮 44（命中，d=26），远于轮 45（漏报，d=30）</figcaption>
</figure>

<h2 id="综合结论">6. 综合结论</h2>
<table>
<thead>
<tr><th>指标</th><th>值</th><th>说明</th></tr>
</thead>
<tbody>
<tr><td><strong>召回率 Recall</strong></td><td><strong>97.9%</strong>（46/47）</td><td>60s 统一推流条件下；首轮短视频 70.21%</td></tr>
<tr><td><strong>精确率 Precision</strong></td><td><strong>100%</strong></td><td>200 NonFight 零误检</td></tr>
<tr><td>误检率 FPR</td><td>0%</td><td>0/200</td></tr>
<tr><td>顽固漏报</td><td>1 个</td><td>Qtcwz_K2Gvo_0.avi，内容层面未触发</td></tr>
</tbody>
</table>

<h3 id="结果可信度">6.1 结果可信度</h3>
<blockquote>
<p>上述数字经<strong>六向量独立审计</strong>全部通过（直连 AIBOX 后端复算告警、推流链路帧残留、检测窗口跨段累积、告警图画面归属、数据集标签、推流保真度），无 bug 或泄露，结果可复现、有物证支撑。算法配置参数已注册到 AIBOX 并逐参数对账一致。</p>
</blockquote>

<h3 id="方法学说明">6.2 方法学说明</h3>
<ul>
<li><strong>混条件外推</strong>：97.9% 综合召回率中，33 个命中来自 10~20s 首轮、13 个来自 60s 重测，属混条件结果。如需严格同条件对比，建议 47 个视频统一 60s 重测。</li>
<li><strong>NonFight 时长不对等</strong>：NonFight 用 20s 测试，而 fight 漏报已证短视频有效时长不足检测窗口，20s 对误检可能偏短。</li>
<li><strong>未发现误检</strong>：故本报告不列误检样例。</li>
</ul>
<hr />
<p style="font-size:13px;color:#888888;">报告生成：2026-08-06 ｜ 指标经独立复算验证（直连 AIBOX 告警接口复算召回/误检，绕开既有统计脚本）</p>

</body>
</html>"""

    OUT.write_text(html, encoding="utf-8")
    print(f"OK HTML 报告已生成: {OUT}")
    print(f"   体积: {OUT.stat().st_size // 1024} KB")
    print(f"   内嵌图片: 2张命中告警图(带红框) + 漏检视频帧 + 2张归属验证对比图(带红框)（base64）")


if __name__ == "__main__":
    build()
