"""Pytest fixtures for the benchmark project."""
import sqlite3
from pathlib import Path

import pytest


def _create_schema(conn):
    """创建测试所需的最小化数据库 schema."""
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE datasets (
            id INTEGER PRIMARY KEY,
            mode TEXT
        );

        CREATE TABLE eval_tasks (
            id INTEGER PRIMARY KEY,
            eval_set_id INTEGER,
            dataset_id INTEGER,
            alert_eval_set_id INTEGER,
            status TEXT,
            duration_hours REAL
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
    """返回一个已初始化最小 schema 的内存 SQLite 连接（service 层单测用）。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _create_schema(conn)
    yield conn
    conn.close()


# ── 路由/API 层测试 fixtures ──────────────────────────────────────────────────

@pytest.fixture
def app(tmp_path, monkeypatch):
    """一个隔离的 Flask app：临时 DB + 临时上传/GT 目录，不污染真实 benchmark.db。"""
    # 在 create_app 之前替换模块级常量与 Config 类属性
    from app import database
    from app.config import Config

    db_path = tmp_path / "test.db"
    monkeypatch.setattr(database, "DATABASE_PATH", db_path)
    # ocr_batch 后台线程用 app.routes.alerts 模块内捕获的 DATABASE_PATH 绑定
    # （`from app.database import DATABASE_PATH` 是导入期捕获，与 database.DATABASE_PATH
    # 是两个名字），跨用例模块缓存后不会随 database.DATABASE_PATH 更新，须显式重绑到
    # 当前 tmp DB，否则批量 OCR 线程会写真实 benchmark.db。
    monkeypatch.setattr("app.routes.alerts.DATABASE_PATH", db_path)
    # streaming 同款双绑定：streaming.py:23 `from app.database import DATABASE_PATH`
    # 导入期捕获，后台监控/重连线程与 _resolve_watermarked_videos/_ensure_duration 等
    # 直连 helper 读此拷贝，不随 database.DATABASE_PATH 更新，须显式重绑到 tmp DB，
    # 否则推流后台线程会写真实 benchmark.db。
    monkeypatch.setattr("app.routes.streaming.DATABASE_PATH", db_path)
    # auto-annotation 同款双绑定：auto_annotation.py:17 `from app.database
    # import DATABASE_PATH` 导入期捕获，后台 worker（_do_auto_annotation）/
    # _process_queue / _batch_capture_gt_frames 直连此拷贝（sqlite3.connect(
    # str(DATABASE_PATH))，不走 get_db()），不随 database.DATABASE_PATH 更新，
    # 须显式重绑到 tmp DB，否则后台线程写真实 benchmark.db。
    monkeypatch.setattr("app.routes.auto_annotation.DATABASE_PATH", db_path)
    # behavior_analysis_service.load_config/save_config 读写模块级
    # DEFAULT_CONFIG_PATH（= 真实 app/auto_anno_config.json）。不重定向 →
    # save_config（start 传 api_key/base_url/model 时）改写仓库配置。重定向到
    # tmp 后 load 读 tmp（不存在→except 返回默认值）、save 写 tmp 文件，不碰真实配置。
    monkeypatch.setattr("app.services.behavior_analysis_service.DEFAULT_CONFIG_PATH",
                        tmp_path / "auto_anno_config.json")
    # extract 同款双绑定：extract.py:19 `from app.database import DATABASE_PATH`
    # 导入期捕获，后台 worker（_do_extract_batch line 304）/ _fail_task(line 326)
    # 直连此拷贝（sqlite3.connect(str(DATABASE_PATH))，不走 get_db()），不随
    # database.DATABASE_PATH 更新，须显式重绑到 tmp DB，否则后台线程写真实 benchmark.db。
    monkeypatch.setattr("app.routes.extract.DATABASE_PATH", db_path)
    # extract start 把 output_dir 写到 EXTRACTED_FRAMES_DIR（= 真实仓库 extracted_frames/），
    # worker 也往此目录抽帧。不重定向 → 测试改写仓库目录。重定向到 tmp 后路径落 tmp、
    # 抽帧写 tmp（被 stub 规避），不碰真实目录。
    monkeypatch.setattr(Config, "EXTRACTED_FRAMES_DIR", str(tmp_path / "extracted_frames"))
    # review 同款双绑定：review.py:10 `from app.database import DATABASE_PATH`
    # 导入期捕获，后台 worker（_ai_check_worker line 213）直连此拷贝
    # （sqlite3.connect(str(DATABASE_PATH))，不走 get_db()），不随
    # database.DATABASE_PATH 更新，须显式重绑到 tmp DB，否则后台线程写真实 benchmark.db。
    monkeypatch.setattr("app.routes.review.DATABASE_PATH", db_path)
    # evaluation 同款双绑定：evaluation.py:11 `from app.database import DATABASE_PATH`
    # 导入期捕获，execute_task 的 worker（闭包，:574）直连此拷贝
    # （sqlite3.connect(str(DATABASE_PATH))，不走 get_db()），不随
    # database.DATABASE_PATH 更新，须显式重绑到 tmp DB，否则后台 worker 写真实 benchmark.db。
    monkeypatch.setattr("app.routes.evaluation.DATABASE_PATH", db_path)
    # event_types 增删改末尾调 _sync_alert_types_json()，它写到模块级
    # ALERT_TYPES_CONFIG_PATH（= 真实 config/alert_types.json）。不重定向 → 测试改写仓库
    # 配置文件，违反"恢复原状"。重定向到 tmp 后，_sync 内部经 get_db()（请求上下文，
    # tmp 库）读已提交数据再写 tmp 文件，不碰真实配置。
    monkeypatch.setattr("app.event_types.ALERT_TYPES_CONFIG_PATH", tmp_path / "alert_types.json")
    # api_config save_config->_write_env 写模块级 ENV_PATH（= 真实 .env）。不重定向 →
    # save 测试改写仓库 .env。重定向到 tmp 后 _write_env 写 tmp 文件，load_dotenv 读
    # tmp 文件注入 os.environ（副作用由 config 测试模块的 _isolate_env_keys 隔离）。
    monkeypatch.setattr("app.services.api_config_service.ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(Config, "UPLOAD_FOLDER", str(tmp_path / "uploads"))
    monkeypatch.setattr(Config, "UPLOAD_VIDEOS", str(tmp_path / "uploads" / "videos"))
    monkeypatch.setattr(Config, "UPLOAD_ALERTS", str(tmp_path / "uploads" / "alerts"))
    monkeypatch.setattr(Config, "GROUND_TRUTH_DIR", str(tmp_path / "ground_truth"))
    Path(tmp_path / "uploads" / "videos").mkdir(parents=True, exist_ok=True)
    Path(tmp_path / "uploads" / "alerts").mkdir(parents=True, exist_ok=True)

    from app import create_app
    app = create_app()
    app.config.update(TESTING=True)
    yield app
    # 显式删除临时库，恢复原状。Windows 下需先无连接占用——OCR 后台线程由
    # tests/test_api_v1_alerts_ocr.py 的 _reset_ocr_progress（依赖 app，先于此 teardown）
    # 保证已退出并关闭其对 tmp DB 的 sqlite 连接，故此处 unlink 不会因文件锁失败。
    for _suffix in ("", "-wal", "-shm"):
        try:
            (db_path.parent / (db_path.name + _suffix)).unlink()
        except OSError:
            pass


@pytest.fixture
def client(app):
    """Flask 测试客户端。"""
    return app.test_client()


def pytest_configure(config):
    """注册 slow marker，避免未知 marker 警告（真跑 EasyOCR 的用例标 @pytest.mark.slow）。"""
    config.addinivalue_line("markers", "slow: 真跑 EasyOCR，约 10s+")


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


# ── 兼容 main 分支测试命名（main 用 app_client/app_ctx，PR 用 app/client）────────
@pytest.fixture
def app_client(client):
    """兼容别名：main 分支测试用 app_client，委托给 PR 的 client（隔离更全）。"""
    return client


@pytest.fixture
def app_ctx(app):
    """兼容别名：main 分支测试用 app_ctx，在 app_context 内 yield app。"""
    with app.app_context():
        yield app

