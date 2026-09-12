"""公共工具函数

项目中多处复用的纯工具函数，不依赖 Flask 上下文。
"""
import os


def allowed_file(filename, allowed_extensions):
    """检查文件扩展名是否在允许列表中"""
    return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed_extensions


def safe_filename(filename):
    """净化文件名，阻止路径穿越，保留 Unicode 字符。

    取最终路径组件（剥离任何目录部分），并拒绝 ``.`` / ``..`` 等特殊名。
    用于上传/重命名场景，防止 ``../../evil.mp4`` 写出目标目录。相比
    ``werkzeug.secure_filename``，本函数不会丢弃中文等非 ASCII 字符。

    Returns:
        净化后的纯文件名；若无法得到安全文件名则返回 None。
    """
    if not filename:
        return None
    # os.path.basename 在 Windows 上同时处理 / 与 \，仅取最后一段
    name = os.path.basename(str(filename).replace("\x00", ""))
    if name in ("", ".", ".."):
        return None
    return name


def row_to_dict(row):
    """将 sqlite3.Row 转换为普通 dict（支持 .get() 方法）

    项目使用 row_factory = sqlite3.Row，该对象支持 dict 式索引访问
    但不支持 .get() 方法。此函数提供统一转换。
    """
    return dict(row) if row is not None else None


def merge_intervals(intervals):
    """合并重叠或相邻的时间区间

    Args:
        intervals: [(start, end), ...] 时间区间列表

    Returns:
        [(start, end), ...] 合并后的区间列表
    """
    if not intervals:
        return []
    sorted_intervals = sorted(intervals, key=lambda x: x[0])
    merged = [list(sorted_intervals[0])]
    for start, end in sorted_intervals[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]
