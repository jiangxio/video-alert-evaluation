---
name: benchmark-web-verification
description: |
  Use when modifying Flask routes, HTML templates, static files, or page-related logic in the 41-benchmark project.
  Before claiming the change is complete,启动本地Flask服务，对受影响的页面和接口做基础验证（HTTP 200、关键元素存在、无服务端报错）。
  If Playwright e2e tests exist for the affected area, run them.
---

# Benchmark Web Verification

## Purpose

This project is a Flask web platform for video watermark benchmarking. Changes to `app/routes/`, `app/templates/`, `app/static/`, or any page logic can break the UI silently. This skill ensures those changes are actually verified before completion.

## When to Use

- Modified `app/routes/*.py`
- Modified `app/templates/*.html`
- Modified `app/static/**/*`
- Modified `app/services/*` that is called from routes/templates
- Added or changed endpoints, forms, tables, buttons, or AJAX calls

## Verification Flow

### Step 1: Ensure Flask is Running

Check if the app is already up:
```bash
pgrep -f "python run.py" || echo "not running"
```

If not running, start it in the background and wait until it responds:
```bash
cd /data/41-benchmark && python run.py &
```
Wait loop until `curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/` returns `200` (timeout 15s).

### Step 2: Identify Affected Areas

Use `git diff --name-only` or the files you just edited to decide which routes/pages to verify:

| Changed File Pattern | Likely Affected Route(s) |
|----------------------|--------------------------|
| `app/routes/videos.py`, `app/templates/videos.html`, `app/templates/video_upload.html`, `app/templates/video_annotate.html` | `/videos`, `/videos/upload`, `/videos/annotate/<id>` |
| `app/routes/evaluation.py`, `app/templates/evaluation.html`, `app/templates/eval_task.html`, `app/templates/dataset_detail.html` | `/evaluation`, `/evaluation/tasks/<id>`, `/datasets/<id>` |
| `app/routes/alerts.py`, `app/templates/alerts.html` | `/alerts` |
| `app/templates/base.html`, `app/static/**/*` | All pages (smoke test `/` and one sub-page) |
| `app/routes/*.py` (endpoint changes) | The specific endpoint(s) added or modified |

### Step 3: Run Level-1 Checks (curl + grep)

For every affected route, run:
```bash
curl -s -o /dev/null -w "%{http_code}" http://localhost:8080<ROUTE>
```
Expected: `200`. If `302`, follow redirects and verify final page.

Then verify at least one critical element exists on the page:
```bash
curl -s http://localhost:8080<ROUTE> | grep -q "<key_text>" && echo "OK" || echo "MISSING"
```

Critical elements by route:
- `/videos` — `"视频列表"` or `"上传视频"`
- `/videos/upload` — `"选择视频"` or `"上传"`
- `/evaluation` — `"评测任务"` or `"新建评测"`
- `/evaluation/tasks/<id>` — `"评测结果"` or `"平均误检数"`
- `/alerts` — `"告警图片"`
- `/datasets/<id>` — `"数据集详情"`

### Step 4: Run Level-2 Checks (Playwright) — if available

Check whether there are Playwright tests in the project:
```bash
glob "tests/e2e_*.py" or "tests/test_*.py" or "e2e/*.py"
```

If they exist and match the affected area, run:
```bash
cd /data/41-benchmark && python -m pytest tests/<relevant_file> -v
```

If Playwright is not installed, note it in your response ("Consider adding Playwright for deeper UI coverage") but do not block the task.

### Step 5: Check Server Logs for Errors

Quick scan of recent Flask stderr or any recent exception:
```bash
curl -s http://localhost:8080<ROUTE> >/dev/null
# Then visually check the last few lines of the running process output if accessible.
# If not accessible, rely on HTTP 500 status codes showing up in curl.
```
Any `500` or traceback = fail, must fix before claiming completion.

## Completion Criteria

You may claim the change is complete ONLY when:
1. Flask responds with `200` on every affected route.
2. Critical page elements are present (curl grep passes).
3. No `500` errors in server response.
4. Any existing Playwright tests for the area pass (if they exist).

If any check fails, report the exact failure and do not claim success.
