"""统一 API Token 配置服务

把平台散落在多处的模型 API 配置统一到一处管理，按“能力角色”分两组：
- 文本逻辑组（OPENAI_* env）：AI 助手、评测报告生成（总结/结论/对话改写）
- 多模态审查组（VISION_* env，未填回退 OPENAI_*）：智能审查、自动标注

设计原则：
- 密钥（API Key）只写 .env 文件，永不进数据库、永不进日志
- 非敏感项（base_url、model、请求间隔等）存数据库 api_config 表，方便页面回显编辑
- DB 中仅存“是否已配置密钥”的 boolean 标记，用于页面展示状态而不暴露密钥本身
- .env 写入采用临时文件 + os.replace 原子操作，避免写一半崩溃导致配置丢失
"""
import os
import tempfile
from pathlib import Path

from dotenv import load_dotenv

from app.config import BASE_DIR
from app.database import get_db


ENV_PATH = BASE_DIR / '.env'
ENV_EXAMPLE_PATH = BASE_DIR / '.env.example'

CONFIG_ROW_ID = 1

# .env 中各配置项的键名
# 文本逻辑组（复用 OPENAI_*，兼容历史 .env）
OPENAI_KEY_ENV = 'OPENAI_API_KEY'
OPENAI_BASE_URL_ENV = 'OPENAI_BASE_URL'
OPENAI_MODEL_ENV = 'OPENAI_MODEL'

# 多模态审查组（新增；未填时回退到 OPENAI_* 以平滑迁移）
VISION_KEY_ENV = 'VISION_API_KEY'
VISION_BASE_URL_ENV = 'VISION_BASE_URL'
VISION_MODEL_ENV = 'VISION_MODEL'


def get_text_creds() -> dict:
    """返回文本逻辑组调用所需配置（AI 助手、评测报告生成）。

    优先级：数据库非敏感项（model/base_url）覆盖环境变量默认值；
    api_key 始终来自 .env 注入的环境变量。
    """
    cfg = _load_db_config()
    base_url = cfg.get('openai_base_url') or os.environ.get(OPENAI_BASE_URL_ENV, 'https://api.openai.com/v1')
    model = cfg.get('openai_model') or os.environ.get(OPENAI_MODEL_ENV, 'gpt-4o-mini')
    # 兼容用户只填域名未填 /v1 的情况
    if base_url and not base_url.rstrip('/').endswith('/v1'):
        base_url = base_url.rstrip('/') + '/v1'
    return {
        'api_key': os.environ.get(OPENAI_KEY_ENV, ''),
        'base_url': base_url,
        'model': model,
    }


def get_vision_creds() -> dict:
    """返回多模态审查组调用所需配置（智能审查、自动标注）。

    读 VISION_* env + DB vision_* 字段；若 VISION_API_KEY 未配置，
    回退到文本逻辑组（OPENAI_*），保证迁移期间不中断。
    """
    cfg = _load_db_config()
    base_url = cfg.get('vision_base_url') or os.environ.get(VISION_BASE_URL_ENV) or None
    model = cfg.get('vision_model') or os.environ.get(VISION_MODEL_ENV) or None
    api_key = os.environ.get(VISION_KEY_ENV, '')

    # 回退到文本逻辑组
    if not api_key:
        api_key = os.environ.get(OPENAI_KEY_ENV, '')
    if not base_url:
        base_url = os.environ.get(OPENAI_BASE_URL_ENV, 'https://api.openai.com/v1')
    if not model:
        model = os.environ.get(OPENAI_MODEL_ENV, 'gpt-4o-mini')

    if base_url and not base_url.rstrip('/').endswith('/v1'):
        base_url = base_url.rstrip('/') + '/v1'
    return {
        'api_key': api_key,
        'base_url': base_url,
        'model': model,
    }


def get_vision_request_interval() -> float:
    """多模态调用的请求间隔（秒），用于限流。"""
    cfg = _load_db_config()
    val = cfg.get('vision_request_interval_sec')
    if val is None:
        # 回退旧字段名（兼容迁移期旧库）
        val = cfg.get('openai_request_interval_sec')
    if val is None:
        return 1.0
    try:
        return float(val)
    except (TypeError, ValueError):
        return 1.0


def is_text_configured() -> bool:
    return bool(os.environ.get(OPENAI_KEY_ENV, '').strip())


def is_vision_configured() -> bool:
    """多模态组是否已配置（VISION_API_KEY 或回退的 OPENAI_API_KEY）。"""
    if os.environ.get(VISION_KEY_ENV, '').strip():
        return True
    return is_text_configured()


def get_config_for_display() -> dict:
    """返回可在设置页展示的配置（含密钥脱敏标记与已配置状态）。"""
    cfg = _load_db_config()
    return {
        'text_base_url': cfg.get('openai_base_url') or os.environ.get(OPENAI_BASE_URL_ENV, 'https://api.openai.com/v1'),
        'text_model': cfg.get('openai_model') or os.environ.get(OPENAI_MODEL_ENV, 'gpt-4o-mini'),
        'text_key_configured': is_text_configured(),
        'vision_base_url': cfg.get('vision_base_url') or os.environ.get(VISION_BASE_URL_ENV, 'https://api.openai.com/v1'),
        'vision_model': cfg.get('vision_model') or os.environ.get(VISION_MODEL_ENV, 'Qwen3-VL-8B-Instruct'),
        'vision_request_interval_sec': cfg.get('vision_request_interval_sec') if cfg.get('vision_request_interval_sec') is not None else 1,
        'vision_key_configured': is_vision_configured(),
    }


def save_config(data: dict) -> dict:
    """保存统一配置。

    data 可能包含：
      - text_api_key / vision_api_key：敏感，写 .env（空字符串或缺失表示不改）
      - text_base_url / text_model：非敏感，写 .env + DB（openai_* 字段）
      - vision_base_url / vision_model / vision_request_interval_sec：非敏感，写 .env + DB（vision_* 字段）
      - 兼容旧前端字段名：openai_* 按 text_* 处理，openai_request_interval_sec 按 vision 限流处理
    """
    env_updates = {}

    # 兼容旧字段名 openai_* → text_*
    text_api_key = data.get('text_api_key') or data.get('openai_api_key')
    text_base_url = data.get('text_base_url') if data.get('text_base_url') is not None else data.get('openai_base_url')
    text_model = data.get('text_model') if data.get('text_model') is not None else data.get('openai_model')

    if text_api_key:
        env_updates[OPENAI_KEY_ENV] = text_api_key.strip()
    if text_base_url is not None:
        env_updates[OPENAI_BASE_URL_ENV] = text_base_url.strip()
    if text_model:
        env_updates[OPENAI_MODEL_ENV] = text_model.strip()

    if data.get('vision_api_key'):
        env_updates[VISION_KEY_ENV] = data['vision_api_key'].strip()
    if data.get('vision_base_url') is not None:
        env_updates[VISION_BASE_URL_ENV] = data['vision_base_url'].strip()
    if data.get('vision_model'):
        env_updates[VISION_MODEL_ENV] = data['vision_model'].strip()

    if env_updates:
        _write_env(env_updates)
        # 写入后重新加载，让当前进程立即生效
        load_dotenv(ENV_PATH, override=True)

    # 非敏感项落 DB
    _save_db_config(data)

    # 同步“已配置”标记
    _update_key_configured_flags()

    return get_config_for_display()


def test_text_llm() -> dict:
    """发一个最小请求验证文本逻辑组端点连通性。"""
    creds = get_text_creds()
    if not creds['api_key']:
        return {'ok': False, 'msg': '未配置文本逻辑组 API Key'}
    try:
        from openai import OpenAI
        client = OpenAI(api_key=creds['api_key'], base_url=creds['base_url'])
        client.models.list()
        return {'ok': True, 'msg': f'连接成功（{creds["base_url"]}）'}
    except Exception as e:
        return {'ok': False, 'msg': f'连接失败：{e}'}


def test_vision() -> dict:
    """发一个最小请求验证多模态审查组端点连通性。"""
    creds = get_vision_creds()
    if not creds['api_key']:
        return {'ok': False, 'msg': '未配置多模态审查组 API Key'}
    try:
        from openai import OpenAI
        client = OpenAI(api_key=creds['api_key'], base_url=creds['base_url'])
        client.models.list()
        return {'ok': True, 'msg': f'连接成功（{creds["base_url"]}）'}
    except Exception as e:
        return {'ok': False, 'msg': f'连接失败：{e}'}


# ── 向后兼容别名（供未覆盖的调用点逐步迁移）──────────────────────────────────

def get_openai_creds() -> dict:
    """[别名] 文本逻辑组凭证。"""
    return get_text_creds()


def get_openai_request_interval() -> float:
    """[别名] 多模态请求间隔。"""
    return get_vision_request_interval()


def is_openai_configured() -> bool:
    """[别名] 文本逻辑组是否已配置。"""
    return is_text_configured()


def test_openai() -> dict:
    """[别名] 文本逻辑组连通性测试。"""
    return test_text_llm()


# ── 内部实现 ──────────────────────────────────────────────────────────────────

_DB_COLUMNS = (
    'openai_base_url, openai_model, '
    'vision_base_url, vision_model, vision_request_interval_sec, '
    'openai_key_configured, vision_key_configured'
)


def _load_db_config() -> dict:
    """从数据库读取非敏感配置项。

    请求上下文内用 Flask get_db；后台线程（无应用上下文）用独立 sqlite 连接。
    """
    import sqlite3
    row = None
    try:
        db = get_db()
        cursor = db.cursor()
        cursor.execute(
            f'SELECT {_DB_COLUMNS} FROM api_config WHERE id = ?',
            (CONFIG_ROW_ID,)
        )
        row = cursor.fetchone()
    except RuntimeError:
        # Working outside of application context（后台线程）
        conn = sqlite3.connect(str(BASE_DIR / 'benchmark.db'), detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            cur.execute(
                f'SELECT {_DB_COLUMNS} FROM api_config WHERE id = ?',
                (CONFIG_ROW_ID,)
            )
            row = cur.fetchone()
        finally:
            conn.close()
    if not row:
        return {}
    return dict(row)


def _save_db_config(data: dict) -> None:
    """把非敏感项写入 api_config 表（upsert）。"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('INSERT OR IGNORE INTO api_config (id) VALUES (?)', (CONFIG_ROW_ID,))

    fields = []
    values = []

    # 兼容旧字段名 openai_* → 文本组
    text_base_url = data.get('text_base_url') if data.get('text_base_url') is not None else data.get('openai_base_url')
    text_model = data.get('text_model') if data.get('text_model') is not None else data.get('openai_model')
    if text_base_url is not None:
        fields.append('openai_base_url = ?')
        values.append((text_base_url or '').strip() or None)
    if text_model is not None:
        fields.append('openai_model = ?')
        values.append((text_model or '').strip() or None)

    if data.get('vision_base_url') is not None:
        fields.append('vision_base_url = ?')
        values.append((data.get('vision_base_url') or '').strip() or None)
    if data.get('vision_model') is not None:
        fields.append('vision_model = ?')
        values.append((data.get('vision_model') or '').strip() or None)
    # 兼容旧字段名 openai_request_interval_sec
    interval = data.get('vision_request_interval_sec')
    if interval is None:
        interval = data.get('openai_request_interval_sec')
    if interval is not None:
        fields.append('vision_request_interval_sec = ?')
        try:
            values.append(int(float(interval)))
        except (TypeError, ValueError):
            values.append(1)

    if fields:
        fields.append('updated_at = CURRENT_TIMESTAMP')
        values.append(CONFIG_ROW_ID)
        cursor.execute(
            f'UPDATE api_config SET {", ".join(fields)} WHERE id = ?',
            values
        )
        db.commit()


def _update_key_configured_flags() -> None:
    """同步密钥“已配置”标记到 DB（不存密钥本身）。"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('INSERT OR IGNORE INTO api_config (id) VALUES (?)', (CONFIG_ROW_ID,))
    cursor.execute('''
        UPDATE api_config SET
            openai_key_configured = ?,
            vision_key_configured = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
    ''', (1 if is_text_configured() else 0, 1 if is_vision_configured() else 0, CONFIG_ROW_ID))
    db.commit()


def _write_env(updates: dict) -> None:
    """原子写入 .env 文件，合并 updates。

    - 若 .env 已存在：保留其原有键不动，只更新/追加 updates 中的键
    - 若 .env 不存在（首次创建）：先用当前真实环境变量值补齐本服务管理的键，
      再应用 updates，避免用占位符覆盖用户已有的系统环境变量配置

    不管理的键（如 ASSISTANT_ENCRYPTION_KEY）在首次创建时不会被写入，
    继续由系统环境变量提供。
    """
    is_first_create = not ENV_PATH.exists()
    existing = _read_env_as_dict()

    if is_first_create:
        # 首次创建：用当前真实环境变量值作为这些键的起点
        managed_keys = [
            OPENAI_KEY_ENV, OPENAI_BASE_URL_ENV, OPENAI_MODEL_ENV,
            VISION_KEY_ENV, VISION_BASE_URL_ENV, VISION_MODEL_ENV,
        ]
        for k in managed_keys:
            val = os.environ.get(k, '')
            if val:
                existing[k] = val

    existing.update(updates)

    lines = []
    for k, v in existing.items():
        lines.append(f'{k}={v}')

    content = '\n'.join(lines) + '\n'

    # 原子写入：先写临时文件，再 rename
    env_dir = ENV_PATH.parent
    env_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(env_dir), prefix='.env-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
        os.chmod(tmp_path, 0o600)  # 密钥文件仅属主可读写
        os.replace(tmp_path, str(ENV_PATH))
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def _read_env_as_dict() -> dict:
    """读取 .env 为 dict（忽略注释行）。

    若 .env 不存在，返回空 dict —— 不回退到 .env.example，
    因为 .env.example 含占位符，作为写入起点会用占位符覆盖用户已有的系统环境变量。
    首次创建 .env 时，由 _write_env 用当前真实环境变量值补齐未提供的键。
    """
    result = {}
    if not ENV_PATH.exists():
        return result
    try:
        with open(ENV_PATH, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.rstrip('\n')
                stripped = line.strip()
                if not stripped or stripped.startswith('#'):
                    continue
                if '=' not in stripped:
                    continue
                key, _, val = stripped.partition('=')
                result[key.strip()] = val.strip()
    except Exception:
        pass
    return result
