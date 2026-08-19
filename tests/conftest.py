"""Pytest fixtures for the benchmark project."""
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest


def _create_schema(conn):
    """创建测试所需的最小化数据库 schema."""
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE eval_tasks (
            id INTEGER PRIMARY KEY,
            eval_set_id INTEGER,
            dataset_id INTEGER,
            alert_eval_set_id INTEGER,
            status TEXT,
            duration_hours REAL
        );

        -- compute_task_metrics LEFT JOIN datasets 取 mode（测试不插行，NULL → 非实时）
        CREATE TABLE datasets (
            id INTEGER PRIMARY KEY,
            mode TEXT
        );

        CREATE TABLE eval_video_sets (
            id INTEGER PRIMARY KEY,
            video_ids TEXT
        );

        CREATE TABLE videos (
            id INTEGER PRIMARY KEY,
            duration REAL,
            video_id TEXT
        );

        CREATE TABLE eval_merged_events (
            id INTEGER PRIMARY KEY,
            task_id INTEGER,
            event_type TEXT,
            is_false_positive INTEGER DEFAULT 0,
            manual_status TEXT
        );

        CREATE TABLE eval_gt_events (
            id INTEGER PRIMARY KEY,
            task_id INTEGER,
            event_type TEXT,
            confirmed_count INTEGER DEFAULT 0,
            actual_count INTEGER DEFAULT 0,
            start_sec REAL,
            end_sec REAL
        );
        """
    )
    conn.commit()


@pytest.fixture
def db_conn():
    """返回一个已初始化最小 schema 的内存 SQLite 连接."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _create_schema(conn)
    yield conn
    conn.close()


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """返回 Flask test client，使用临时目录做 DB 与上传路径（完整 schema，隔离无副作用）。

    供 API 级异常测试用：样本导入、报告生成等需真实路由与完整表结构的场景。
    """
    db_path = tmp_path / "benchmark.db"

    # app/database.py 的 DATABASE_PATH 是模块级常量，import 时已读取，
    # 需直接 patch 模块属性（setenv 太晚，已 import 过）
    import app.database as _db
    monkeypatch.setattr(_db, "DATABASE_PATH", db_path)

    # 坑：app/routes/alerts.py 顶部 `from app.database import DATABASE_PATH`
    # 在导入期把 DATABASE_PATH 绑定为模块内独立名字，ocr_batch 的后台线程
    # _worker 用的是这个绑定（routes.alerts.DATABASE_PATH），与 database.DATABASE_PATH
    # 是两个名字——只 patch 后者不够，前者仍指向真实库，会污染线上库。
    monkeypatch.setattr("app.routes.alerts.DATABASE_PATH", str(db_path))

    from app import create_app
    app = create_app()
    app.config["TESTING"] = True
    app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024

    with app.test_client() as client:
        yield client


@pytest.fixture
def app_ctx(tmp_path, monkeypatch):
    """提供 app app_context 与一个已建表的真实 DB（临时隔离，每个测试独立）。"""
    db_path = tmp_path / "test.db"
    import app.database as _db
    monkeypatch.setattr(_db, "DATABASE_PATH", db_path)

    from app import create_app
    app = create_app()
    with app.app_context():
        yield app


# ── od-dataset-manager（独立子项目）测试 fixture ──────────────────────────────
# od 的 app.py 模块名 `app` 与主仓库 `app` 包冲突，必须 importlib 按路径加载避开。
_OD_DIR = Path(__file__).parent.parent / "od-dataset-manager"


@pytest.fixture(scope="session")
def od_module():
    """importlib 加载 od-dataset-manager/app.py 为独立模块，避开 `app` 包名冲突。

    临时把 od 目录加到 sys.path 让 app.py 顶层 `import config` 生效，加载后移除
    （config 模块已进 sys.modules，后续 od 函数内 config.X 引用不受影响）。
    """
    import importlib.util
    import sys
    od_dir = str(_OD_DIR)
    sys.path.insert(0, od_dir)
    try:
        spec = importlib.util.spec_from_file_location("od_under_test", _OD_DIR / "app.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        if od_dir in sys.path:
            sys.path.remove(od_dir)


@pytest.fixture
def od_client(od_module, tmp_path, monkeypatch):
    """od app test client + 临时隔离 DB（DB_FILE 是 import 期绑定，patch 模块属性）。"""
    import config
    monkeypatch.setattr(od_module, "DB_FILE", str(tmp_path / "annotations.db"))
    monkeypatch.setattr(config, "BASE_DIR", str(tmp_path))
    # importlib 加载的模块 __name__ 非真实包名，Flask root_path 解析不到 templates，
    # 显式设绝对路径让页面渲染可用
    od_module.app.template_folder = str(_OD_DIR / "templates")
    od_module.init_db()
    od_module.app.config["TESTING"] = True
    with od_module.app.test_client() as client:
        yield client

