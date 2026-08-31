#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
轻量 markdown → HTML 转换器（不依赖 markdown 包），再用 Edge headless 打印成 PDF。
处理本报告用到的语法：标题、表格、列表、代码块、加粗、行内代码、分隔线、引用块。
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SRC = Path(os.environ.get("MD_SRC", "") or (sys.argv[1] if len(sys.argv) > 1 else ""))
if not SRC or not SRC.exists():
    print("用法: python md_to_pdf.py <input.md>  或设置 MD_SRC 环境变量"); sys.exit(1)
HTML_OUT = SRC.with_suffix(".html")
PDF_OUT = SRC.with_suffix(".pdf")
EDGE = shutil.which("msedge") or shutil.which("microsoft-edge") or os.environ.get("EDGE_BIN", "")
if not EDGE:
    print("未找到 Edge(msedge)，请设置 EDGE_BIN 环境变量"); sys.exit(1)


def escape_html(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def inline(s: str) -> str:
    """行内格式：加粗、行内代码、链接。先转义再处理。"""
    s = escape_html(s)
    # 行内代码 `code`
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    # 加粗 **text**
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    # 链接 [text](url)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', s)
    return s


def md_to_html(md: str) -> str:
    lines = md.split("\n")
    out = []
    i = 0
    in_code = False
    code_buf = []
    while i < len(lines):
        line = lines[i]
        # 代码块围栏
        if line.strip().startswith("```"):
            if in_code:
                out.append('<pre><code>' + escape_html("\n".join(code_buf)) + "</code></pre>")
                code_buf = []
                in_code = False
            else:
                in_code = True
            i += 1
            continue
        if in_code:
            code_buf.append(line)
            i += 1
            continue
        # 表格：连续行以 | 开头，且第二行是分隔行 |---|---|
        if line.lstrip().startswith("|") and i + 1 < len(lines) and re.match(r"\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 2  # 跳过分隔行
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                rows.append(cells)
                i += 1
            t = ['<table>']
            t.append("<thead><tr>" + "".join(f"<th>{inline(h)}</th>" for h in header) + "</tr></thead>")
            t.append("<tbody>")
            for r in rows:
                t.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
            t.append("</tbody></table>")
            out.append("\n".join(t))
            continue
        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{inline(m.group(2))}</h{lvl}>")
            i += 1
            continue
        # 分隔线
        if re.match(r"^-{3,}\s*$", line):
            out.append("<hr>")
            i += 1
            continue
        # 引用块
        if line.lstrip().startswith(">"):
            quote_lines = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                quote_lines.append(lines[i].lstrip()[1:].strip())
                i += 1
            out.append("<blockquote>" + inline(" ".join(quote_lines)) + "</blockquote>")
            continue
        # 无序列表
        if re.match(r"^\s*[-*]\s+", line):
            items = []
            while i < len(lines) and re.match(r"^\s*[-*]\s+", lines[i]):
                items.append("<li>" + inline(re.sub(r"^\s*[-*]\s+", "", lines[i])) + "</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        # 空行
        if not line.strip():
            out.append("")
            i += 1
            continue
        # 普通段落
        out.append(f"<p>{inline(line)}</p>")
        i += 1
    return "\n".join(out)


CSS = """
@page { margin: 18mm 16mm; size: A4; }
* { box-sizing: border-box; }
body {
  font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif;
  font-size: 11pt; line-height: 1.6; color: #1a1a1a; margin: 0;
}
h1 { font-size: 20pt; border-bottom: 3px solid #2563eb; padding-bottom: 8px; color: #0f172a; margin-top: 0; }
h2 { font-size: 15pt; border-bottom: 1px solid #cbd5e1; padding-bottom: 4px; color: #1e3a8a; margin-top: 24px; }
h3 { font-size: 12.5pt; color: #1e40af; margin-top: 18px; }
h4 { font-size: 11pt; color: #334155; }
p { margin: 6px 0; }
table { border-collapse: collapse; width: 100%; margin: 10px 0; font-size: 10pt; }
th, td { border: 1px solid #94a3b8; padding: 5px 8px; text-align: left; vertical-align: top; }
th { background: #1e3a8a; color: #fff; font-weight: 600; }
tr:nth-child(even) { background: #f1f5f9; }
code { background: #e2e8f0; padding: 1px 5px; border-radius: 3px; font-family: "Consolas", monospace; font-size: 9.5pt; }
pre { background: #0f172a; color: #e2e8f0; padding: 12px; border-radius: 6px; overflow-x: auto; }
pre code { background: none; color: inherit; padding: 0; }
blockquote { border-left: 4px solid #f59e0b; background: #fef3c7; margin: 10px 0; padding: 8px 12px; color: #78350f; }
hr { border: none; border-top: 1px solid #cbd5e1; margin: 18px 0; }
ul { margin: 6px 0; padding-left: 22px; }
li { margin: 3px 0; }
strong { color: #b91c1c; }
a { color: #2563eb; text-decoration: none; }
"""


def main():
    md = SRC.read_text(encoding="utf-8")
    body = md_to_html(md)
    html = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>视频流泄露与结果完整性审计报告</title>
<style>{CSS}</style></head>
<body>{body}</body></html>"""
    HTML_OUT.write_text(html, encoding="utf-8")
    print(f"HTML 已生成: {HTML_OUT} ({HTML_OUT.stat().st_size//1024} KB)")

    # Edge headless 打印 PDF
    cmd = [
        EDGE, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
        f"--print-to-pdf={PDF_OUT}", HTML_OUT.as_uri(),
    ]
    print("Edge 打印 PDF...")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    if PDF_OUT.exists() and PDF_OUT.stat().st_size > 1000:
        print(f"PDF 已生成: {PDF_OUT} ({PDF_OUT.stat().st_size//1024} KB)")
    else:
        print(f"PDF 生成失败。stderr: {r.stderr[:500]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
