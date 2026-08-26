"""/api/v1/evaluation 资源族端点（评测任务 + 测前分析 + 报告）。

委托 app/routes/evaluation.py 的 13 个指标邻接/状态变更/报告端点（execute/finalize/
confirm/unconfirm/get_results/get_event_metrics/get_report_image/detailed-report×4/sync-gt/
eval-status）：起后台线程 / 调 compute_task_metrics 等指标函数 / 生成报告 / 调 LLM——
**绝不抽改指标算法**（命中判定三条件±5s、is_fp、min(actual,confirmed) 封顶、confirmed==0→
{1,1}、算术平均、compute_task_metrics/get_effective_status 函数体只调不改）。get_results 含
内联 realtime 指标公式（旧 792-843），按 D3 委托避转录风险。

纯查询/CRUD（23 个：list 系列/get_task/delete/create/clone/update PATCH/analyze/
manual-status PATCH×2/gt-counts PATCH/check-updates/serve-gt-frame/pre-analysis CRUD/
eval-sets/chat-sessions CRUD）原位重写，复用 get_db + _legacy helper（_enrich_task_algo_versions/
_is_realtime_task/_run_pre_analysis/_get_all_event_types）+ service 纯函数 analyze_merged_events。
列表用 SQL 层 LIMIT/OFFSET + COUNT(*) 真分页。

语义修正（新端点专属，旧不动，H 位对齐 http_status）：PUT→PATCH（update_task/
manual-status/batch/gt-counts）、DELETE→204（delete_task/pre-analysis/chat-session）、
400→409（finalize/unconfirm/detailed-report「请先完成评测」）、400→404（clone 源视频集
不存在/sync-gt）、create→201/clone→201。成功状态保 200（对齐先例）。

委托边界盲区（bug-audit 另修）：#8 前端 sum/len（后端已修）、#18 service TypeError（路由
级已 guard）、#19 字体、#20 LLM 无 timeout、低危 evaluated_at 恒 null。委托端点不断言这些。
"""
import json
from pathlib import Path
from datetime import datetime

from flask import Blueprint, request, Response, send_file

from app.database import get_db
from app.routes import evaluation as _legacy
from app.routes import send_file_with_cache, send_image_with_thumbnail
from app.services.eval_service import analyze_merged_events
from ._helpers import parse_pagination, paginate, raise_msg
from .compat import call_old_view
from .responses import ok, created, no_content, paginated, ApiError

bp = Blueprint("api_v1_evaluation", __name__, url_prefix="/api/v1")

# 服务端错误兜底（FF=13 SS=80）
_EVAL_FALLBACK = (41300, 500, "操作失败")

# eval_tasks 详情/列表的统一列
_TASK_COLS = (
    "id, name, notes, dataset_id, alert_eval_set_id, eval_set_id, merge_interval_sec, "
    "event_start_sec, event_end_sec, event_interval_sec, trigger_rate, min_event_duration_sec, "
    "status, created_at, finalized, accuracy, recall, avg_fp_per_hour, event_metrics, confirmed_at"
)


# ── 委托错误文案 → (5 位码, http_status) 映射 ──────────────────────────────────

_EXEC_MSG = {"任务不存在": (21300, 404), "评测正在运行中": (31300, 409), "关联了多个": (11312, 400)}
_FINALIZE_MSG = {"任务不存在": (21300, 404), "只有已完成": (31301, 409)}
_UNCONFIRM_MSG = {"任务不存在": (21300, 404), "尚未确认": (31302, 409)}
_CONFIRM_MSG = {"任务不存在": (21300, 404), "不在评测视频集中": (11313, 400)}
_NOTFOUND_MSG = {"任务不存在": (21300, 404)}
_EVAL_STATUS_MSG = {"没有正在运行的评测": (21311, 404)}
_REPORT_MSG = {"任务不存在": (21300, 404), "请先完成评测": (31303, 409),
               "生成报告失败": (41301, 500), "PDF 生成失败": (41302, 500)}
_PREVIEW_MSG = {"任务不存在": (21300, 404), "缺少 API Key": (11310, 400)}
_SYNC_MSG = {"缺少视频ID": (11306, 400), "同步方向": (11307, 400), "视频不存在": (21309, 404),
             "视频ID未设置": (11308, 400), "GT 文件不存在": (21310, 404),
             "读取 GT 文件失败": (41303, 500)}


def _delegate_binary(old_func, task_id, msg_to_code):
    """二进制委托（report-image/detailed-report/pdf）：旧视图成功返 Response（send_file/HTML），
    失败返 (jsonify, code)。Response 直传（不走信封），tuple 走 raise_msg。"""
    result = old_func(task_id)
    if isinstance(result, tuple):
        resp, status = result[0], result[1]
        raise_msg(resp.get_json(), msg_to_code, fallback=_EVAL_FALLBACK)
    return result


# ── 评测任务：查询 ─────────────────────────────────────────────────────────────

@bp.route("/evaluation/tasks", methods=["GET"])
def list_tasks():
    """评测任务列表（真分页 + 名称富化 + algo_versions 富化）。"""
    page, page_size = parse_pagination()
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT COUNT(*) FROM eval_tasks")
    total = cur.fetchone()[0]
    offset = (page - 1) * page_size
    cur.execute(
        f"SELECT {_TASK_COLS} FROM eval_tasks ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (page_size, offset),
    )
    tasks = [dict(t) for t in cur.fetchall()]
    for t in tasks:
        if t.get("dataset_id"):
            cur.execute("SELECT name FROM datasets WHERE id = ?", (t["dataset_id"],))
            d = cur.fetchone()
            t["dataset_name"] = d["name"] if d else None
        if t.get("alert_eval_set_id"):
            cur.execute("SELECT name FROM eval_alert_sets WHERE id = ?", (t["alert_eval_set_id"],))
            d = cur.fetchone()
            t["alert_eval_set_name"] = d["name"] if d else None
        if t.get("eval_set_id"):
            cur.execute("SELECT name FROM eval_video_sets WHERE id = ?", (t["eval_set_id"],))
            d = cur.fetchone()
            t["eval_set_name"] = d["name"] if d else None
        _legacy._enrich_task_algo_versions(t, db)
    return paginated(tasks, total, page, page_size)


@bp.route("/evaluation/tasks/<int:task_id>", methods=["GET"])
def get_task(task_id):
    """任务详情。不存在→404(21300)。"""
    db = get_db()
    cur = db.cursor()
    cur.execute(f"SELECT {_TASK_COLS} FROM eval_tasks WHERE id = ?", (task_id,))
    task = cur.fetchone()
    if not task:
        raise ApiError(21300, "任务不存在", 404)
    task_dict = dict(task)
    _legacy._enrich_task_algo_versions(task_dict, db)
    return ok(task_dict)


@bp.route("/evaluation/tasks/<int:task_id>/status", methods=["GET"])
def eval_status(task_id):
    """评测进度（委托旧 eval_status：读模块态 _eval_progress）。无运行→404(21311)。"""
    body, status = call_old_view(_legacy.eval_status, task_id)
    if status == 200:
        return ok(body)
    raise_msg(body, _EVAL_STATUS_MSG, fallback=_EVAL_FALLBACK)


@bp.route("/evaluation/tasks/<int:task_id>/results", methods=["GET"])
def get_results(task_id):
    """评测结果（委托旧 get_results：含内联 realtime 指标公式，D3 委托避转录风险）。不存在→404(21300)。"""
    body, status = call_old_view(_legacy.get_results, task_id)
    if status == 200:
        return ok(body)
    raise_msg(body, _NOTFOUND_MSG, fallback=_EVAL_FALLBACK)


@bp.route("/evaluation/tasks/<int:task_id>/event-metrics", methods=["GET"])
def get_event_metrics(task_id):
    """事件级指标（委托旧 get_event_metrics：调 compute_task_metrics，指标邻接故委托）。不存在→404(21300)。"""
    body, status = call_old_view(_legacy.get_event_metrics, task_id)
    if status == 200:
        return ok(body)
    raise_msg(body, _NOTFOUND_MSG, fallback=_EVAL_FALLBACK)


@bp.route("/evaluation/tasks/<int:task_id>/check-updates", methods=["GET"])
def check_updates(task_id):
    """检查标注是否有更新。任务不存在→404(21300)。"""
    db = get_db()
    cur = db.cursor()
    try:
        cur.execute("SELECT updated_at FROM eval_tasks WHERE id = ?", (task_id,))
    except Exception:
        return ok({"has_updates": False})
    task = cur.fetchone()
    if not task:
        raise ApiError(21300, "任务不存在", 404)
    evaluated_at = task["updated_at"]
    if not evaluated_at:
        return ok({"has_updates": False})

    cur.execute("""
        SELECT DISTINCT video_id FROM (
            SELECT video_id FROM eval_merged_events WHERE task_id = ?
            UNION
            SELECT video_id FROM eval_gt_events WHERE task_id = ?
        ) WHERE video_id IS NOT NULL
    """, (task_id, task_id))
    video_ids = [row[0] for row in cur.fetchall()]
    if not video_ids:
        return ok({"has_updates": False})
    placeholders = ",".join("?" for _ in video_ids)
    try:
        cur.execute(f"SELECT MAX(updated_at) FROM videos WHERE video_id IN ({placeholders})", video_ids)
        video_max = cur.fetchone()[0]
        cur.execute(f"SELECT MAX(e.updated_at) FROM events e JOIN videos v ON v.id = e.video_db_id WHERE v.video_id IN ({placeholders})", video_ids)
        event_max = cur.fetchone()[0]
    except Exception:
        return ok({"has_updates": False})

    def _to_dt(value):
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            return datetime.fromisoformat(value)
        return None

    evaluated_dt = _to_dt(evaluated_at)
    if not evaluated_dt:
        return ok({"has_updates": False})
    times = [evaluated_dt] + [_to_dt(v) for v in [video_max, event_max] if v is not None]
    times = [t for t in times if t is not None]
    if not times:
        return ok({"has_updates": False})
    return ok({"has_updates": max(times) > evaluated_dt})


# ── 评测任务：CRUD ──────────────────────────────────────────────────────────────

@bp.route("/evaluation/tasks", methods=["POST"])
def create_task():
    """新建评测任务→201。对齐旧 create_task 校验+INSERT 逻辑。"""
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    if not name:
        raise ApiError(11300, "任务名称不能为空", 400)
    dataset_id = data.get("dataset_id")
    alert_eval_set_id = data.get("alert_eval_set_id")
    if not dataset_id and not alert_eval_set_id:
        raise ApiError(11301, "请选择告警数据集或告警评测集", 400)

    db = get_db()
    cur = db.cursor()
    is_realtime = False
    if dataset_id:
        cur.execute("SELECT id, mode FROM datasets WHERE id = ?", (dataset_id,))
        ds = cur.fetchone()
        if not ds:
            raise ApiError(21301, "告警数据集不存在", 404)
        is_realtime = ds["mode"] == "realtime"
    eval_set_id = data.get("eval_set_id")
    if is_realtime:
        duration_hours = data.get("duration_hours")
        if duration_hours is not None:
            duration_hours = float(duration_hours)
    else:
        if not eval_set_id:
            raise ApiError(11302, "请选择评测视频集", 400)
        cur.execute("SELECT id FROM eval_video_sets WHERE id = ?", (eval_set_id,))
        if not cur.fetchone():
            raise ApiError(21302, "评测视频集不存在", 404)
        duration_hours = None
    if alert_eval_set_id:
        cur.execute("SELECT id FROM eval_alert_sets WHERE id = ?", (alert_eval_set_id,))
        if not cur.fetchone():
            raise ApiError(21303, "告警评测集不存在", 404)

    cur.execute("""
        INSERT INTO eval_tasks
        (name, notes, dataset_id, alert_eval_set_id, eval_set_id, merge_interval_sec, event_start_sec,
         event_end_sec, event_interval_sec, trigger_rate, min_event_duration_sec, status, duration_hours)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (name, data.get("notes", ""), dataset_id, alert_eval_set_id, eval_set_id,
          data.get("merge_interval_sec", 5.0), 0, 0, data.get("event_interval_sec", 10.0),
          data.get("trigger_rate", 0.5), data.get("min_event_duration_sec", 0), "created", duration_hours))
    db.commit()
    task_id = cur.lastrowid
    cur.execute(f"SELECT {_TASK_COLS}, duration_hours FROM eval_tasks WHERE id = ?", (task_id,))
    return created({"task": dict(cur.fetchone())}, location=f"/api/v1/evaluation/tasks/{task_id}")


@bp.route("/evaluation/tasks/<int:task_id>:clone", methods=["POST"])
def clone_task(task_id):
    """复制任务配置→201。源不存在→404(21304)；源视频集不存在→404(21305，旧 400→404)。"""
    data = request.get_json() or {}
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT name, eval_set_id, merge_interval_sec, event_start_sec, event_end_sec, "
        "event_interval_sec, trigger_rate, min_event_duration_sec FROM eval_tasks WHERE id = ?",
        (task_id,),
    )
    src = cur.fetchone()
    if not src:
        raise ApiError(21304, "源任务不存在", 404)
    cur.execute("SELECT id FROM eval_video_sets WHERE id = ?", (src["eval_set_id"],))
    if not cur.fetchone():
        raise ApiError(21305, "源任务关联的视频集已不存在，无法复制", 404)
    name = data.get("name", "").strip() or f"{src['name']} (复制)"
    cur.execute(
        """INSERT INTO eval_tasks (name, notes, dataset_id, alert_eval_set_id, eval_set_id,
           merge_interval_sec, event_start_sec, event_end_sec, event_interval_sec, trigger_rate,
           min_event_duration_sec, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (name, data.get("notes", ""), None, None, src["eval_set_id"], src["merge_interval_sec"],
         src["event_start_sec"], src["event_end_sec"], src["event_interval_sec"],
         src["trigger_rate"], src["min_event_duration_sec"], "created"),
    )
    db.commit()
    new_id = cur.lastrowid
    cur.execute(
        "SELECT id, name, notes, dataset_id, alert_eval_set_id, eval_set_id, merge_interval_sec, "
        "event_start_sec, event_end_sec, event_interval_sec, trigger_rate, min_event_duration_sec, "
        "status, created_at FROM eval_tasks WHERE id = ?", (new_id,)
    )
    return created({"task": dict(cur.fetchone())}, location=f"/api/v1/evaluation/tasks/{new_id}")


@bp.route("/evaluation/tasks/<int:task_id>", methods=["PATCH"])
def update_task(task_id):
    """更新任务参数（PUT→PATCH，部分更新）。不存在→404(21300)。"""
    data = request.get_json() or {}
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM eval_tasks WHERE id = ?", (task_id,))
    if not cur.fetchone():
        raise ApiError(21300, "任务不存在", 404)
    update_fields, update_values = [], []
    for f in ("merge_interval_sec", "event_interval_sec", "trigger_rate",
              "min_event_duration_sec", "duration_hours"):
        if f in data:
            update_fields.append(f"{f} = ?")
            update_values.append(data[f])
    if update_fields:
        update_values.append(task_id)
        cur.execute(f'UPDATE eval_tasks SET {", ".join(update_fields)} WHERE id = ?', update_values)
        db.commit()
    cur.execute(f"SELECT {_TASK_COLS} FROM eval_tasks WHERE id = ?", (task_id,))
    return ok({"task": dict(cur.fetchone())})


@bp.route("/evaluation/tasks/<int:task_id>", methods=["DELETE"])
def delete_task(task_id):
    """删除任务+级联。不存在→404(21300)；成功→204。"""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM eval_tasks WHERE id = ?", (task_id,))
    if not cur.fetchone():
        raise ApiError(21300, "任务不存在", 404)
    for tbl in ("eval_results", "eval_merged_events", "eval_gt_events", "report_chat_sessions"):
        cur.execute(f"DELETE FROM {tbl} WHERE task_id = ?", (task_id,))
    cur.execute("DELETE FROM eval_tasks WHERE id = ?", (task_id,))
    db.commit()
    return no_content()


@bp.route("/evaluation/tasks/<int:task_id>:analyze", methods=["POST"])
def analyze_task(task_id):
    """分析可合并事件（复用 service 纯函数 analyze_merged_events）。不存在→404(21300)；失败→500(41300)。"""
    try:
        db = get_db()
        result = analyze_merged_events(task_id, db)
        if result is None:
            raise ApiError(21300, "任务不存在", 404)
        return ok(result)
    except ApiError:
        raise
    except Exception as e:
        raise ApiError(41300, f"分析出错: {e}", 500)


# ── 评测任务：状态变更（委托，不改指标逻辑） ───────────────────────────────────

@bp.route("/evaluation/tasks/<int:task_id>:confirm", methods=["POST"])
def confirm_merged(task_id):
    """保存合并告警+GT 事件（委托旧 confirm_merged：写 GT 指标输入+跨表校验）。请求体自读。"""
    body, status = call_old_view(_legacy.confirm_merged, task_id)
    if status == 200:
        return ok(body)
    raise_msg(body, _CONFIRM_MSG, fallback=_EVAL_FALLBACK)


@bp.route("/evaluation/tasks/<int:task_id>:execute", methods=["POST"])
def execute_task(task_id):
    """执行评测（委托旧 execute_task：起 worker，worker 内联命中判定+compute_task_metrics，绝不抽改）。
    冲突→409(31300)；不存在→404(21300)。"""
    body, status = call_old_view(_legacy.execute_task, task_id)
    if status == 200:
        return ok(body)
    raise_msg(body, _EXEC_MSG, fallback=_EVAL_FALLBACK)


@bp.route("/evaluation/tasks/<int:task_id>:finalize", methods=["POST"])
def finalize_task(task_id):
    """确认结果+算指标+锁定（委托旧 finalize_task：调 compute_task_metrics，指标邻接故委托）。
    未完成→409(31301)；不存在→404(21300)。"""
    body, status = call_old_view(_legacy.finalize_task, task_id)
    if status == 200:
        return ok(body)
    raise_msg(body, _FINALIZE_MSG, fallback=_EVAL_FALLBACK)


@bp.route("/evaluation/tasks/<int:task_id>:unconfirm", methods=["POST"])
def unconfirm_task(task_id):
    """取消确认（委托旧 unconfirm_task：清指标，metric lifecycle 状态转换）。
    未确认→409(31302)；不存在→404(21300)。"""
    body, status = call_old_view(_legacy.unconfirm_task, task_id)
    if status == 200:
        return ok(body)
    raise_msg(body, _UNCONFIRM_MSG, fallback=_EVAL_FALLBACK)


# ── 人工状态 / GT 计数（CRUD 重写，仅 UPDATE 存用户决策，下游 finalize 重算） ─────

@bp.route("/evaluation/tasks/<int:task_id>/merged-events/<int:merged_id>/status", methods=["PATCH"])
def update_manual_status(task_id, merged_id):
    """改人工状态（PUT→PATCH）。无效状态→400(11303)；任务不存在→404(21300)；记录不存在→404(21306)。"""
    data = request.get_json() or {}
    manual_status = data.get("manual_status")
    if manual_status not in ("auto", "correct", "false_positive", "ignored"):
        raise ApiError(11303, "无效的状态值", 400)
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM eval_tasks WHERE id = ?", (task_id,))
    if not cur.fetchone():
        raise ApiError(21300, "任务不存在", 404)
    cur.execute("UPDATE eval_merged_events SET manual_status = ? WHERE id = ? AND task_id = ?",
                (manual_status, merged_id, task_id))
    db.commit()
    if cur.rowcount == 0:
        raise ApiError(21306, "记录不存在", 404)
    return ok({"manual_status": manual_status})


@bp.route("/evaluation/tasks/<int:task_id>/merged-events:batch-status", methods=["PATCH"])
def batch_update_manual_status(task_id):
    """批量改人工状态（PUT→PATCH）。无效状态→400(11303)；缺 merged_ids→400(11304)；任务不存在→404(21300)。"""
    data = request.get_json() or {}
    merged_ids = data.get("merged_ids", [])
    manual_status = data.get("manual_status")
    if manual_status not in ("auto", "correct", "false_positive", "ignored"):
        raise ApiError(11303, "无效的状态值", 400)
    if not merged_ids or not isinstance(merged_ids, list):
        raise ApiError(11304, "请提供 merged_ids 列表", 400)
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM eval_tasks WHERE id = ?", (task_id,))
    if not cur.fetchone():
        raise ApiError(21300, "任务不存在", 404)
    placeholders = ",".join("?" for _ in merged_ids)
    cur.execute(f"UPDATE eval_merged_events SET manual_status = ? WHERE id IN ({placeholders}) AND task_id = ?",
               [manual_status] + list(merged_ids) + [task_id])
    db.commit()
    return ok({"updated_count": cur.rowcount})


@bp.route("/evaluation/tasks/<int:task_id>/gt-events/<int:gt_id>", methods=["PATCH"])
def update_gt_event_counts(task_id, gt_id):
    """改 GT 预期/实际数（PUT→PATCH）。任务不存在→404(21300)；缺字段→400(11305)；记录不存在→404(21306)。"""
    data = request.get_json() or {}
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM eval_tasks WHERE id = ?", (task_id,))
    if not cur.fetchone():
        raise ApiError(21300, "任务不存在", 404)
    update_fields, update_values = [], []
    if "confirmed_count" in data:
        update_fields.append("confirmed_count = ?")
        update_values.append(int(data["confirmed_count"]))
    if "actual_count" in data:
        update_fields.append("actual_count = ?")
        update_values.append(int(data["actual_count"]))
    if not update_fields:
        raise ApiError(11305, "缺少要更新的字段（confirmed_count 或 actual_count）", 400)
    update_values += [gt_id, task_id]
    cur.execute(f'UPDATE eval_gt_events SET {", ".join(update_fields)} WHERE id = ? AND task_id = ?', update_values)
    db.commit()
    if cur.rowcount == 0:
        raise ApiError(21306, "记录不存在", 404)
    return ok({})


# ── 报告（委托：二进制直传 / LLM / 指标邻接） ───────────────────────────────────

@bp.route("/evaluation/tasks/<int:task_id>/report/image", methods=["GET"])
def get_report_image(task_id):
    """PNG 报告图（委托旧 get_report_image，二进制直传；继承 #19 字体盲区）。"""
    return _delegate_binary(_legacy.get_report_image, task_id, _NOTFOUND_MSG)


@bp.route("/evaluation/tasks/<int:task_id>/report", methods=["POST"])
def detailed_report(task_id):
    """HTML 报告（委托旧 detailed_report，二进制直传；不存在→404/未完成→409/失败→500）。"""
    return _delegate_binary(_legacy.detailed_report, task_id, _REPORT_MSG)


@bp.route("/evaluation/tasks/<int:task_id>/report/pdf", methods=["POST"])
def detailed_report_pdf(task_id):
    """PDF 报告（委托旧 detailed_report_pdf，Playwright 渲染，二进制直传；继承 Playwright 依赖盲区）。"""
    return _delegate_binary(_legacy.detailed_report_pdf, task_id, _REPORT_MSG)


@bp.route("/evaluation/tasks/<int:task_id>/report:preview", methods=["POST"])
def detailed_report_preview(task_id):
    """AI 摘要预览（委托旧 detailed_report_preview，调 _call_claude，继承 #20 无 timeout 盲区）。
    不存在→404(21300)；缺 API Key→400(11310)。"""
    body, status = call_old_view(_legacy.detailed_report_preview, task_id)
    if status == 200:
        return ok(body)
    raise_msg(body, _PREVIEW_MSG, fallback=_EVAL_FALLBACK)


@bp.route("/evaluation/tasks/<int:task_id>/report:chat", methods=["POST"])
def detailed_report_chat(task_id):
    """AI 对话迭代（委托旧 detailed_report_chat，调 _call_claude_chat，继承 #20 无 timeout 盲区）。
    不存在→404(21300)；缺 API Key→400(11310)。"""
    body, status = call_old_view(_legacy.detailed_report_chat, task_id)
    if status == 200:
        return ok(body)
    raise_msg(body, _PREVIEW_MSG, fallback=_EVAL_FALLBACK)


# ── GT 帧 / GT 同步 ─────────────────────────────────────────────────────────────

@bp.route("/evaluation/gt-frames/<int:frame_id>/file", methods=["GET"])
def serve_gt_frame(frame_id):
    """GT 帧图片（二进制，?w=&h= 缩略图）。不存在→404(21300)。"""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT file_path FROM gt_frames WHERE id = ?", (frame_id,))
    frame = cur.fetchone()
    if not frame:
        raise ApiError(21300, "Frame not found", 404)
    file_path = Path(frame["file_path"])
    if not file_path.exists():
        raise ApiError(21300, "File not found", 404)
    max_w = request.args.get("w", type=int)
    max_h = request.args.get("h", type=int)
    if max_w or max_h:
        return send_image_with_thumbnail(str(file_path), max_width=max_w, max_height=max_h)
    return send_file_with_cache(str(file_path))


@bp.route("/evaluation/gt:sync", methods=["POST"])
def sync_ground_truth():
    """GT 同步（委托旧 sync_ground_truth：写 events 表 GT 指标输入，保守委托）。请求体自读。"""
    body, status = call_old_view(_legacy.sync_ground_truth)
    if status == 200:
        return ok(body)
    raise_msg(body, _SYNC_MSG, fallback=_EVAL_FALLBACK)


# ── 测前分析（CRUD 重写，复用 _legacy._run_pre_analysis） ───────────────────────

@bp.route("/evaluation/pre-analysis", methods=["GET"])
def list_pre_analysis():
    """测前分析列表（真分页）。"""
    page, page_size = parse_pagination()
    base = ("SELECT p.id, p.eval_video_set_id, p.merge_interval_sec, p.event_interval_sec, "
            "p.trigger_rate, p.min_event_duration_sec, p.result_json, p.created_at, "
            "e.name AS eval_set_name FROM pre_analysis_records p "
            "LEFT JOIN eval_video_sets e ON e.id = p.eval_video_set_id")

    def _m(r):
        d = dict(r)
        try:
            d["result"] = json.loads(d["result_json"])
        except Exception:
            d["result"] = {}
        d.pop("result_json", None)
        return d

    return paginate(get_db(), base, "ORDER BY p.created_at DESC", (), page, page_size, _m)


@bp.route("/evaluation/pre-analysis/<int:record_id>", methods=["GET"])
def get_pre_analysis(record_id):
    """测前分析详情。不存在→404(21307)。"""
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT p.id, p.eval_video_set_id, p.merge_interval_sec, p.event_interval_sec, "
        "p.trigger_rate, p.min_event_duration_sec, p.result_json, p.created_at, "
        "e.name AS eval_set_name FROM pre_analysis_records p "
        "LEFT JOIN eval_video_sets e ON e.id = p.eval_video_set_id WHERE p.id = ?",
        (record_id,),
    )
    row = cur.fetchone()
    if not row:
        raise ApiError(21307, "分析记录不存在", 404)
    r = dict(row)
    try:
        r["result"] = json.loads(r["result_json"])
    except Exception:
        r["result"] = {}
    r.pop("result_json", None)
    return ok(r)


@bp.route("/evaluation/pre-analysis:by-set/<int:set_id>", methods=["GET"])
def list_pre_analysis_by_set(set_id):
    """某评测集的测前分析（真分页）。"""
    page, page_size = parse_pagination()
    base = ("SELECT p.id, p.eval_video_set_id, p.merge_interval_sec, p.event_interval_sec, "
            "p.trigger_rate, p.min_event_duration_sec, p.result_json, p.created_at, "
            "e.name AS eval_set_name FROM pre_analysis_records p "
            "LEFT JOIN eval_video_sets e ON e.id = p.eval_video_set_id WHERE p.eval_video_set_id = ?")

    def _m(r):
        d = dict(r)
        try:
            d["result"] = json.loads(d["result_json"])
        except Exception:
            d["result"] = {}
        d.pop("result_json", None)
        return d

    return paginate(get_db(), base, "ORDER BY p.created_at DESC", (set_id,), page, page_size, _m)


@bp.route("/evaluation/pre-analysis", methods=["POST"])
def create_pre_analysis():
    """执行测前分析+存记录→201（复用 _legacy._run_pre_analysis）。缺视频集→400(11309)。"""
    data = request.get_json() or {}
    eval_video_set_id = data.get("eval_video_set_id")
    if not eval_video_set_id:
        raise ApiError(11309, "请选择评测视频集", 400)
    merge_interval_sec = float(data.get("merge_interval_sec", 5.0))
    event_interval_sec = float(data.get("event_interval_sec", 10.0))
    trigger_rate = float(data.get("trigger_rate", 0.5))
    min_event_duration_sec = float(data.get("min_event_duration_sec", 0))
    result = _legacy._run_pre_analysis(eval_video_set_id, merge_interval_sec, event_interval_sec,
                                        trigger_rate, min_event_duration_sec)
    if "error" in result:
        raise ApiError(11309, result["error"], 400)
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "INSERT INTO pre_analysis_records (eval_video_set_id, merge_interval_sec, event_interval_sec, "
        "trigger_rate, min_event_duration_sec, result_json) VALUES (?, ?, ?, ?, ?, ?)",
        (eval_video_set_id, merge_interval_sec, event_interval_sec, trigger_rate,
         min_event_duration_sec, json.dumps(result, ensure_ascii=False)),
    )
    db.commit()
    record_id = cur.lastrowid
    return created({"record_id": record_id, "result": result},
                   location=f"/api/v1/evaluation/pre-analysis/{record_id}")


@bp.route("/evaluation/pre-analysis/<int:record_id>", methods=["DELETE"])
def delete_pre_analysis(record_id):
    """删除测前分析。不存在→404(21307)；成功→204。"""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM pre_analysis_records WHERE id = ?", (record_id,))
    if not cur.fetchone():
        raise ApiError(21307, "记录不存在", 404)
    cur.execute("DELETE FROM pre_analysis_records WHERE id = ?", (record_id,))
    db.commit()
    return no_content()


# ── 评测视频集 ──────────────────────────────────────────────────────────────────

@bp.route("/evaluation/eval-sets", methods=["GET"])
def list_eval_sets():
    """评测视频集列表（真分页，含 video_count + gt_frame_count）。"""
    page, page_size = parse_pagination()
    base = "SELECT id, name, notes, video_ids, created_at FROM eval_video_sets"

    def _m(r):
        s = dict(r)
        try:
            s["video_ids"] = json.loads(s["video_ids"]) if s.get("video_ids") else []
        except Exception:
            s["video_ids"] = []
        return s

    # gt_frame_count 需 video_ids 子查询，分页后单独补（避免 _paginate 单 mapper 不支持 cursor 复用）
    db = get_db()
    cur = db.cursor()
    cur.execute(f"SELECT COUNT(*) FROM ({base}) _c")
    total = cur.fetchone()[0]
    offset = (page - 1) * page_size
    cur.execute(f"{base} ORDER BY created_at DESC LIMIT ? OFFSET ?", (page_size, offset))
    items = []
    for s in cur.fetchall():
        s = dict(s)
        try:
            s["video_ids"] = json.loads(s["video_ids"]) if s.get("video_ids") else []
        except Exception:
            s["video_ids"] = []
        s["video_count"] = len(s["video_ids"])
        gt = 0
        if s["video_ids"]:
            placeholders = ",".join("?" for _ in s["video_ids"])
            cur.execute(f"SELECT COUNT(*) FROM gt_frames WHERE video_db_id IN ({placeholders})", s["video_ids"])
            gt = cur.fetchone()[0]
        s["gt_frame_count"] = gt
        items.append(s)
    return paginated(items, total, page, page_size)


@bp.route("/evaluation/eval-sets:with-analysis-count", methods=["GET"])
def list_eval_sets_with_analysis_count():
    """评测视频集 + 分析次数（真分页）。"""
    page, page_size = parse_pagination()
    base = ("SELECT e.id, e.name, e.notes, e.video_ids, e.created_at, "
            "COUNT(p.id) AS analysis_count FROM eval_video_sets e "
            "LEFT JOIN pre_analysis_records p ON p.eval_video_set_id = e.id GROUP BY e.id")

    def _m(r):
        s = dict(r)
        try:
            s["video_ids"] = json.loads(s["video_ids"]) if s.get("video_ids") else []
        except Exception:
            s["video_ids"] = []
        s["video_count"] = len(s["video_ids"])
        s["analysis_count"] = s.get("analysis_count") or 0
        return s

    return paginate(get_db(), base, "ORDER BY e.created_at DESC", (), page, page_size, _m)


# ── Chat 会话 ───────────────────────────────────────────────────────────────────

@bp.route("/evaluation/tasks/<int:task_id>/chat-sessions", methods=["GET"])
def list_chat_sessions(task_id):
    """任务 Chat 会话列表（真分页）。任务不存在→404(21300)。"""
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM eval_tasks WHERE id = ?", (task_id,))
    if not cur.fetchone():
        raise ApiError(21300, "任务不存在", 404)
    base = ("SELECT id, name, summary_text, conclusion_text, created_at, updated_at "
            "FROM report_chat_sessions WHERE task_id = ?")
    return paginate(get_db(), base, "ORDER BY updated_at DESC", (task_id,), *parse_pagination(), dict)


@bp.route("/evaluation/tasks/<int:task_id>/chat-sessions", methods=["POST"])
def save_chat_session(task_id):
    """保存/更新 Chat 会话（upsert）。任务不存在→404(21300)；会话不存在(update)→404(21308)。
    create→201、update→200。"""
    data = request.get_json() or {}
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM eval_tasks WHERE id = ?", (task_id,))
    if not cur.fetchone():
        raise ApiError(21300, "任务不存在", 404)
    session_id = data.get("session_id")
    name = data.get("name", "未命名会话")
    messages = json.dumps(data.get("messages", []), ensure_ascii=False)
    summary_text = data.get("summary_text", "")
    conclusion_text = data.get("conclusion_text", "")
    if session_id:
        cur.execute(
            "UPDATE report_chat_sessions SET name=?, messages=?, summary_text=?, conclusion_text=?, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=? AND task_id=?",
            (name, messages, summary_text, conclusion_text, session_id, task_id),
        )
        if cur.rowcount == 0:
            raise ApiError(21308, "会话不存在", 404)
        db.commit()
        return ok({"session_id": session_id})
    cur.execute(
        "INSERT INTO report_chat_sessions (task_id, name, messages, summary_text, conclusion_text) "
        "VALUES (?, ?, ?, ?, ?)", (task_id, name, messages, summary_text, conclusion_text),
    )
    db.commit()
    new_id = cur.lastrowid
    return created({"session_id": new_id},
                   location=f"/api/v1/evaluation/tasks/{task_id}/chat-sessions/{new_id}")


@bp.route("/evaluation/tasks/<int:task_id>/chat-sessions/<int:session_id>", methods=["GET"])
def get_chat_session(task_id, session_id):
    """Chat 会话详情（含完整 messages）。不存在→404(21308)。"""
    db = get_db()
    cur = db.cursor()
    cur.execute(
        "SELECT id, name, messages, summary_text, conclusion_text, created_at, updated_at "
        "FROM report_chat_sessions WHERE id=? AND task_id=?", (session_id, task_id)
    )
    row = cur.fetchone()
    if not row:
        raise ApiError(21308, "会话不存在", 404)
    result = dict(row)
    try:
        result["messages"] = json.loads(result["messages"])
    except Exception:
        result["messages"] = []
    return ok(result)


@bp.route("/evaluation/tasks/<int:task_id>/chat-sessions/<int:session_id>", methods=["DELETE"])
def delete_chat_session(task_id, session_id):
    """删除 Chat 会话。不存在→404(21308)；成功→204。"""
    db = get_db()
    cur = db.cursor()
    cur.execute("DELETE FROM report_chat_sessions WHERE id=? AND task_id=?", (session_id, task_id))
    db.commit()
    if cur.rowcount == 0:
        raise ApiError(21308, "会话不存在", 404)
    return no_content()
