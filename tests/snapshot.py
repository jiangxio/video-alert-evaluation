"""简易 golden 快照辅助：把旧 API 响应序列化成基线文件，之后逐项比对。

用法：
- 首次运行：快照不存在 → 自动写入并返回 seeded=True（让测试 skip，完成基线采集）
- 后续运行：快照存在 → 严格比对，不一致即让测试失败
- UPDATE_SNAPSHOTS=1：强制重写（确认是有意变更后再采）

刻意不引第三方依赖（syrupy/pytest-regressions），用 stdlib json 落盘。
"""
import json
import os
from pathlib import Path

SNAP_DIR = Path(__file__).parent / "snapshots" / "old_api"


def _name_for(path: str) -> str:
    return (path.strip("/").replace("/", "_") or "root") + ".json"


def _signature(resp) -> dict:
    """把一个 test client 响应压缩成可序列化、可比较的签名。"""
    ctype = resp.content_type or ""
    if ctype.startswith("application/json"):
        body = resp.get_json(silent=True)
        body_kind = "json"
    else:
        body = resp.data.decode("utf-8", "replace")
        body_kind = "text"
    return {
        "status": resp.status_code,
        "content_type": ctype,
        "body_kind": body_kind,
        "body": body,
    }


def assert_unchanged(client, path: str):
    """对 client.get(path) 比对快照。

    返回 (seeded, actual, expected, snap_path)：
    - seeded=True 表示刚刚写入基线（首次或 UPDATE_SNAPSHOTS），expected=None
    - seeded=False 表示做了严格比对，expected 为基线
    """
    resp = client.get(path)
    actual = _signature(resp)
    snap_path = SNAP_DIR / _name_for(path)
    if os.environ.get("UPDATE_SNAPSHOTS") == "1" or not snap_path.exists():
        SNAP_DIR.mkdir(parents=True, exist_ok=True)
        snap_path.write_text(
            json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return True, actual, None, snap_path
    expected = json.loads(snap_path.read_text(encoding="utf-8"))
    return False, actual, expected, snap_path
