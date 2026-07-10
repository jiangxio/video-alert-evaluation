"""平台事件类型注册表

所有事件类型的英文标识、中文显示名、行为分析描述、标签颜色集中定义于此。
Python 路由/服务统一从本模块导入；Flask 通过 Jinja2 context processor 向模板注入。

新增事件类别时，优先在页面上操作；本文件保留硬编码作为数据库不可用时的降级备份。
"""

import json
import os
import sqlite3
import tempfile
from pathlib import Path

EVENT_TYPES = [
    "rat",
    "smoke",
    "use_phone",
    "call_phone",
    "chef",
    "trash",
    "mask",
    "flame",
    "fireEscapeOccupy",
    "safetyOfficerOnDuty",
    "personSleep",
    "personLadderHigh",
    "withoutHelmetOnSite",
    "withoutRefClothes",
    "personFallDown",
    "personAction",
    "inHandDangerTool",
]

TYPE_NAMES = {
    "rat": "老鼠检测",
    "smoke": "抽烟检测",
    "use_phone": "玩手机检测",
    "call_phone": "打电话检测",
    "chef": "厨师服/厨师帽检测",
    "trash": "垃圾桶未关检测",
    "mask": "未戴口罩检测",
    "flame": "火焰检测",
    "fireEscapeOccupy": "消防通道占用识别",
    "safetyOfficerOnDuty": "人员离岗检测",
    "personSleep": "睡岗检测",
    "personLadderHigh": "登高检测",
    "withoutHelmetOnSite": "工地人员不戴安全帽检测",
    "withoutRefClothes": "反光衣识别",
    "personFallDown": "人员跌倒检测",
    "personAction": "人员动作检测",
    "inHandDangerTool": "手持危险工具检测",
}

TYPE_DESCRIPTIONS = {
    "rat": "老鼠：画面中出现老鼠",
    "smoke": "抽烟：有人正在抽烟，手持香烟靠近嘴边，可能有烟雾",
    "use_phone": "玩手机：低头看着手中的手机屏幕（发短信、刷视频、玩游戏），手机不在耳边",
    "call_phone": "打电话：手机贴在耳朵旁，正在语音通话",
    "chef": "厨师服：穿着白色厨师服或围裙（含未戴厨师帽场景）",
    "trash": "垃圾：垃圾桶未关或垃圾/废弃物堆积",
    "mask": "口罩：人员未佩戴口罩",
    "flame": "火焰：画面中出现明火或火焰",
    "fireEscapeOccupy": "消防通道占用：消防通道被车辆、物品或人员堵塞",
    "safetyOfficerOnDuty": "人员离岗：安全岗位人员不在指定位置",
    "personSleep": "睡岗：值班人员在工作岗位上睡觉",
    "personLadderHigh": "登高检测：人员在梯子或高处作业",
    "withoutHelmetOnSite": "不戴安全帽：工地人员未佩戴安全帽",
    "withoutRefClothes": "反光衣识别：人员未穿着反光衣",
    "personFallDown": "人员跌倒：画面中有人摔倒或跌倒",
    "personAction": "人员动作：检测特定的人员动作行为",
    "inHandDangerTool": "手持危险工具：人员手持危险工具或器械",
}

# 算法标签颜色 (background, text)
TYPE_TAG_COLORS = {
    "rat": ("#ffeaa7", "#6c5ce7"),
    "smoke": ("#dfe6e9", "#2d3436"),
    "use_phone": ("#74b9ff", "#0984e3"),
    "call_phone": ("#a29bfe", "#6c5ce7"),
    "chef": ("#fd79a8", "#e84393"),
    "trash": ("#55efc4", "#00b894"),
    "mask": ("#fab1a0", "#d63031"),
    "flame": ("#ff7675", "#d63031"),
    "fireEscapeOccupy": ("#ffe0b2", "#e65100"),
    "safetyOfficerOnDuty": ("#c5cae9", "#3f51b5"),
    "personSleep": ("#b2dfdb", "#00796b"),
    "personLadderHigh": ("#f8bbd0", "#c2185b"),
    "withoutHelmetOnSite": ("#d7ccc8", "#5d4037"),
    "withoutRefClothes": ("#fff9c4", "#f57f17"),
    "personFallDown": ("#e1bee7", "#7b1fa2"),
    "personAction": ("#bbdefb", "#1565c0"),
    "inHandDangerTool": ("#ffccbc", "#bf360c"),
}

ALERT_TYPES_CONFIG_PATH = Path(__file__).parent.parent / "config" / "alert_types.json"
DATABASE_PATH = Path(__file__).parent.parent / "benchmark.db"


def _get_db_connection():
    """获取数据库连接，优先使用 Flask get_db，否则直连 SQLite"""
    try:
        from app.database import get_db
        return get_db()
    except Exception:
        conn = sqlite3.connect(str(DATABASE_PATH))
        conn.row_factory = sqlite3.Row
        return conn


def _load_event_types_from_db():
    """从 event_types 表加载全部事件类型，失败返回空列表"""
    try:
        conn = _get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, key, name, description, bg_color, fg_color, tags "
            "FROM event_types ORDER BY sort_order, id"
        )
        rows = [dict(r) for r in cur.fetchall()]
        if conn is not None:
            # 如果是直连创建的连接需要关闭；Flask g.db 不要关闭
            try:
                from app.database import get_db
                if conn is not get_db():
                    conn.close()
            except Exception:
                conn.close()
        return rows
    except Exception:
        return []


def _db_available():
    """检查 event_types 表是否存在且有数据"""
    rows = _load_event_types_from_db()
    return len(rows) > 0


def get_event_types():
    """返回所有事件类型英文标识列表（副本）"""
    if _db_available():
        return [r["key"] for r in _load_event_types_from_db()]
    return list(EVENT_TYPES)


def get_type_names():
    """返回英文标识 -> 中文名的映射（副本）"""
    if _db_available():
        return {r["key"]: r["name"] for r in _load_event_types_from_db()}
    return dict(TYPE_NAMES)


def get_type_descriptions():
    """返回英文标识 -> 描述文本的映射（副本）"""
    if _db_available():
        return {r["key"]: (r["description"] or "") for r in _load_event_types_from_db()}
    return dict(TYPE_DESCRIPTIONS)


def get_type_keywords():
    """返回用于文件名自动识别的关键词列表（副本）"""
    return list(get_event_types())


def get_type_tag_colors():
    """返回英文标识 -> (背景色, 文字色) 的映射（副本）"""
    if _db_available():
        return {r["key"]: (r["bg_color"], r["fg_color"]) for r in _load_event_types_from_db()}
    return dict(TYPE_TAG_COLORS)


def get_type_tags():
    """返回英文标识 -> 标签列表的映射（副本）"""
    result = {}
    for r in _load_event_types_from_db():
        try:
            tags = json.loads(r["tags"] or "[]")
        except Exception:
            tags = []
        result[r["key"]] = tags
    return result


def _sync_alert_types_json():
    """将当前数据库中的事件类型按 id 排序写回 config/alert_types.json"""
    try:
        rows = _load_event_types_from_db()
        lines = [f"{r['id']} {r['key']}\n" for r in sorted(rows, key=lambda x: x["id"])]
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(ALERT_TYPES_CONFIG_PATH.parent),
            prefix=".alert_types_",
            suffix=".json.tmp",
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.writelines(lines)
            os.replace(tmp_path, str(ALERT_TYPES_CONFIG_PATH))
        except Exception:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise
    except Exception:
        pass
