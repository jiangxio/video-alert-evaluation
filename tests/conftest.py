"""Pytest fixtures for the benchmark project."""
import sqlite3
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
            status TEXT
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
