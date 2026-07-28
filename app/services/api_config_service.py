"""统一 API Token 配置服务

把平台散落在三处的模型 API 配置（AI 助手、行为分析/自动标注、报告生成）
统一到一处管理。

设计原则：
- 密钥（API Key / Auth Token）只写 .env 文件，永不进数据库、永不进日志
- 非敏感项（base_url、model、请求间隔等）存数据库 api_config 表，方便页面回显编辑
- DB 中仅存"是否已配置密钥"的 boolean 标记，用于页面展示状态而不暴露密钥本身
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
OPENAI_KEY_ENV = 'OPENAI_API_KEY'
OPENAI_BASE_URL_ENV = 'OPENAI_BASE_URL'
OPENAI_MODEL_ENV = 'OPENAI_MODEL'

CLAUDE_KEY_ENV = 'ANTHROPIC_AUTH_TOKEN'
CLAUDE_BASE_URL_ENV = 'ANTHROPIC_BASE_URL'
CLAUDE_MODEL_ENV = 'ANTHROPIC_MODEL'


def get_openai_creds() -> dict:
    """返回 OpenAI 兼容调用所需配置。

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


def get_claude_creds() -> dict:
    """返回 Claude 调用所需配置。"""
    cfg = _load_db_config()
    base_url = cfg.get('claude_base_url') or os.environ.get(CLAUDE_BASE_URL_ENV) or None
    model = cfg.get('claude_model') or os.environ.get(CLAUDE_MODEL_ENV, 'claude-sonnet-5')
    return {
        'auth_token': os.environ.get(CLAUDE_KEY_ENV, ''),
        'base_url': base_url,
        'model': model,
    }


def get_openai_request_interval() -> float:
    """多模态调用的请求间隔（秒），用于限流。"""
    cfg = _load_db_config()
    val = cfg.get('openai_request_interval_sec')
    if val is None:
        return 1.0
    try:
        return float(val)
    except (TypeError, ValueError):
        return 1.0


def is_openai_configured() -> bool:
    return bool(os.environ.get(OPENAI_KEY_ENV, '').strip())


def is_claude_configured() -> bool:
    return bool(os.environ.get(CLAUDE_KEY_ENV, '').strip())


def get_config_for_display() -> dict:
    """返回可在设置页展示的配置（含密钥脱敏标记与已配置状态）。"""
    cfg = _load_db_config()
    return {
        'openai_base_url': cfg.get('openai_base_url') or os.environ.get(OPENAI_BASE_URL_ENV, 'https://api.openai.com/v1'),
        'openai_model': cfg.get('openai_model') or os.environ.get(OPENAI_MODEL_ENV, 'gpt-4o-mini'),
        'openai_request_interval_sec': cfg.get('openai_request_interval_sec') if cfg.get('openai_request_interval_sec') is not None else 1,
        'openai_key_configured': is_openai_configured(),
        'claude_base_url': cfg.get('claude_base_url') or os.environ.get(CLAUDE_BASE_URL_ENV, ''),
        'claude_model': cfg.get('claude_model') or os.environ.get(CLAUDE_MODEL_ENV, 'claude-sonnet-5'),
        'claude_key_configured': is_claude_configured(),
    }


def save_config(data: dict) -> dict:
    """保存统一配置。

    data 可能包含：
      - openai_api_key / claude_api_key：敏感，写 .env（空字符串或缺失表示不改）
      - openai_base_url / openai_model / openai_request_interval_sec：非敏感，写 DB
      - claude_base_url / claude_model：非敏感，写 DB
    """
    env_updates = {}
    if data.get('openai_api_key'):
        env_updates[OPENAI_KEY_ENV] = data['openai_api_key'].strip()
    if data.get('claude_api_key'):
        env_updates[CLAUDE_KEY_ENV] = data['claude_api_key'].strip()

    # base_url 也同步写 .env，保证 config.py 的环境变量读取与 DB 一致，
    # 同时让命令行脚本（scripts/）也能读到
    if data.get('openai_base_url') is not None:
        env_updates[OPENAI_BASE_URL_ENV] = data['openai_base_url'].strip()
    if data.get('openai_model'):
        env_updates[OPENAI_MODEL_ENV] = data['openai_model'].strip()
    if data.get('claude_base_url') is not None:
        env_updates[CLAUDE_BASE_URL_ENV] = data['claude_base_url'].strip()
    if data.get('claude_model'):
        env_updates[CLAUDE_MODEL_ENV] = data['claude_model'].strip()

    if env_updates:
        _write_env(env_updates)
        # 写入后重新加载，让当前进程立即生效
        load_dotenv(ENV_PATH, override=True)

    # 非敏感项落 DB
    _save_db_config(data)

    # 同步"已配置"标记
    _update_key_configured_flags()

    return get_config_for_display()


def test_openai() -> dict:
    """发一个最小请求验证 OpenAI 兼容端点连通性。"""
    creds = get_openai_creds()
    if not creds['api_key']:
        return {'ok': False, 'msg': '未配置 OpenAI API Key'}
    try:
        from openai import OpenAI
        client = OpenAI(api_key=creds['api_key'], base_url=creds['base_url'])
        client.models.list()
        return {'ok': True, 'msg': f'连接成功（{creds["base_url"]}）'}
    except Exception as e:
        return {'ok': False, 'msg': f'连接失败：{e}'}


def test_claude() -> dict:
    """发一个最小请求验证 Claude 端点连通性。"""
    creds = get_claude_creds()
    if not creds['auth_token']:
        return {'ok': False, 'msg': '未配置 Claude API Token'}
    try:
        import anthropic
        kwargs = {'api_key': creds['auth_token']}
        if creds['base_url']:
            kwargs['base_url'] = creds['base_url']
        client = anthropic.Anthropic(**kwargs)
        client.messages.create(
            model=creds['model'],
            max_tokens=16,
            messages=[{'role': 'user', 'content': 'ping'}],
        )
        return {'ok': True, 'msg': f'连接成功（model={creds["model"]}）'}
    except Exception as e:
        return {'ok': False, 'msg': f'连接失败：{e}'}


# ── 内部实现 ──────────────────────────────────────────────────────────────────

def _load_db_config() -> dict:
    """从数据库读取非敏感配置项。

    请求上下文内用 Flask get_db；后台线程（无应用上下文）用独立 sqlite 连接。
    """
    import sqlite3
    row = None
    try:
        db = get_db()
        cursor = db.cursor()
        cursor.execute('''
            SELECT openai_base_url, openai_model, openai_request_interval_sec,
                   claude_base_url, claude_model,
                   openai_key_configured, claude_key_configured
            FROM api_config WHERE id = ?
        ''', (CONFIG_ROW_ID,))
        row = cursor.fetchone()
    except RuntimeError:
        # Working outside of application context（后台线程）
        conn = sqlite3.connect(str(BASE_DIR / 'benchmark.db'), detect_types=sqlite3.PARSE_DECLTYPES)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            cur.execute('''
                SELECT openai_base_url, openai_model, openai_request_interval_sec,
                       claude_base_url, claude_model,
                       openai_key_configured, claude_key_configured
                FROM api_config WHERE id = ?
            ''', (CONFIG_ROW_ID,))
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
    if 'openai_base_url' in data:
        fields.append('openai_base_url = ?')
        values.append((data.get('openai_base_url') or '').strip() or None)
    if 'openai_model' in data:
        fields.append('openai_model = ?')
        values.append((data.get('openai_model') or '').strip() or None)
    if 'openai_request_interval_sec' in data:
        fields.append('openai_request_interval_sec = ?')
        try:
            values.append(int(float(data['openai_request_interval_sec'])))
        except (TypeError, ValueError):
            values.append(1)
    if 'claude_base_url' in data:
        fields.append('claude_base_url = ?')
        values.append((data.get('claude_base_url') or '').strip() or None)
    if 'claude_model' in data:
        fields.append('claude_model = ?')
        values.append((data.get('claude_model') or '').strip() or None)

    if fields:
        fields.append('updated_at = CURRENT_TIMESTAMP')
        values.append(CONFIG_ROW_ID)
        cursor.execute(
            f'UPDATE api_config SET {", ".join(fields)} WHERE id = ?',
            values
        )
        db.commit()


def _update_key_configured_flags() -> None:
    """同步密钥"已配置"标记到 DB（不存密钥本身）。"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('INSERT OR IGNORE INTO api_config (id) VALUES (?)', (CONFIG_ROW_ID,))
    cursor.execute('''
        UPDATE api_config SET
            openai_key_configured = ?,
            claude_key_configured = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
    ''', (1 if is_openai_configured() else 0, 1 if is_claude_configured() else 0, CONFIG_ROW_ID))
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
            CLAUDE_KEY_ENV, CLAUDE_BASE_URL_ENV, CLAUDE_MODEL_ENV,
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
