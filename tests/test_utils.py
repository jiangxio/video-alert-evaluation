"""Tests for app.utils."""
import pytest

from app.utils import allowed_file, merge_intervals, row_to_dict


class TestAllowedFile:
    def test_valid_extension(self):
        assert allowed_file("video.mp4", {"mp4", "avi"}) is True

    def test_case_insensitive(self):
        assert allowed_file("video.MP4", {"mp4", "avi"}) is True
        assert allowed_file("video.Mp4", {"mp4", "avi"}) is True

    def test_invalid_extension(self):
        assert allowed_file("report.txt", {"mp4", "avi"}) is False

    def test_no_extension(self):
        assert allowed_file("video", {"mp4"}) is False

    def test_empty_filename(self):
        assert allowed_file("", {"mp4"}) is False


class TestMergeIntervals:
    def test_empty(self):
        assert merge_intervals([]) == []

    def test_single_interval(self):
        assert merge_intervals([(1, 5)]) == [(1, 5)]

    def test_overlapping(self):
        assert merge_intervals([(1, 3), (2, 5), (7, 9)]) == [(1, 5), (7, 9)]

    def test_adjacent(self):
        # 相邻区间 [1,3] 和 [3,5] 会被合并为 [1,5]
        assert merge_intervals([(1, 3), (3, 5)]) == [(1, 5)]

    def test_unordered_input(self):
        assert merge_intervals([(5, 7), (1, 3), (2, 6)]) == [(1, 7)]

    def test_non_overlapping(self):
        assert merge_intervals([(1, 2), (4, 5), (7, 8)]) == [(1, 2), (4, 5), (7, 8)]


class TestRowToDict:
    def test_converts_row(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("CREATE TABLE t (a INTEGER, b TEXT)")
        cur.execute("INSERT INTO t VALUES (1, 'x')")
        cur.execute("SELECT * FROM t")
        row = cur.fetchone()

        d = row_to_dict(row)
        assert d == {"a": 1, "b": "x"}
        # 确认返回的是普通 dict，支持 .get()
        assert d.get("a") == 1
        assert d.get("c", "default") == "default"
        conn.close()

    def test_none_row(self):
        assert row_to_dict(None) is None
