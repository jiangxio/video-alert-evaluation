"""公共工具函数

项目中多处复用的纯工具函数，不依赖 Flask 上下文。
"""


def allowed_file(filename, allowed_extensions):
    """检查文件扩展名是否在允许列表中"""
    return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed_extensions


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
