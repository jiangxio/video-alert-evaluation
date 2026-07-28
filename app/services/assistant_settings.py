"""AI 助手配置管理

支持从环境变量读取默认值，并允许管理员在 UI 中覆盖。
数据库中的 API Key 使用 Fernet 对称加密存储。
"""
import json
import os
from typing import Optional

from cryptography.fernet import Fernet

from app.database import get_db


SETTINGS_ROW_ID = 1


def _get_fernet() -> Optional[Fernet]:
    """根据环境变量中的 ASSISTANT_ENCRYPTION_KEY 创建 Fernet 实例。"""
    key = os.environ.get('ASSISTANT_ENCRYPTION_KEY', '').strip()
    if not key:
        return None
    try:
        return Fernet(key.encode('utf-8'))
    except Exception:
        return None


def _encrypt(value: str) -> str:
    """加密字符串，返回格式标记以便识别是否已加密。"""
    if not value:
        return value
    fernet = _get_fernet()
    if not fernet:
        # 无加密密钥时明文存储并做标记，方便后续识别
        return f'[PLAINTEXT]{value}'
    return f'[ENC]{fernet.encrypt(value.encode("utf-8")).decode("utf-8")}'


def _decrypt(value: str) -> str:
    """解密字符串。"""
    if not value:
        return ''
    if value.startswith('[PLAINTEXT]'):
        return value[len('[PLAINTEXT]'):]
    if value.startswith('[ENC]'):
        ciphertext = value[len('[ENC]'):]
        fernet = _get_fernet()
        if not fernet:
            return ''
        try:
            return fernet.decrypt(ciphertext.encode('utf-8')).decode('utf-8')
        except Exception:
            return ''
    # 兼容旧数据：无任何标记时视为明文
    return value


def get_assistant_settings() -> dict:
    """获取当前生效的 AI 助手配置（合并环境变量默认值与数据库覆盖值）。"""
    db = get_db()
    cursor = db.cursor()
    cursor.execute('''
        SELECT openai_api_key, openai_base_url, openai_model,
               max_messages_per_session, max_write_actions_per_session,
               confirmation_ttl_seconds
        FROM assistant_settings WHERE id = ?
    ''', (SETTINGS_ROW_ID,))
    row = cursor.fetchone()

    # 环境变量默认值
    defaults = {
        'openai_api_key': os.environ.get('OPENAI_API_KEY', ''),
        'openai_base_url': os.environ.get('OPENAI_BASE_URL', 'https://api.openai.com/v1'),
        'openai_model': os.environ.get('OPENAI_MODEL', 'gpt-4o-mini'),
        'max_messages_per_session': int(os.environ.get('ASSISTANT_MAX_MESSAGES_PER_SESSION', '50')),
        'max_write_actions_per_session': int(os.environ.get('ASSISTANT_MAX_WRITE_ACTIONS_PER_SESSION', '30')),
        'confirmation_ttl_seconds': int(os.environ.get('ASSISTANT_CONFIRMATION_TTL_SECONDS', '300')),
    }

    if not row:
        return defaults

    # 数据库值覆盖环境变量默认值；key 需要解密
    result = dict(row)
    decrypted_key = _decrypt(result.get('openai_api_key') or '')
    if decrypted_key:
        defaults['openai_api_key'] = decrypted_key
    if result.get('openai_base_url'):
        defaults['openai_base_url'] = result['openai_base_url']
    if result.get('openai_model'):
        defaults['openai_model'] = result['openai_model']
    if result.get('max_messages_per_session') is not None:
        defaults['max_messages_per_session'] = result['max_messages_per_session']
    if result.get('max_write_actions_per_session') is not None:
        defaults['max_write_actions_per_session'] = result['max_write_actions_per_session']
    if result.get('confirmation_ttl_seconds') is not None:
        defaults['confirmation_ttl_seconds'] = result['confirmation_ttl_seconds']

    return defaults


def get_openai_credentials() -> dict:
    """返回 OpenAI 调用所需配置，若未配置则返回空值。

    优先走统一 API 配置（api_config_service，密钥来自 .env）；
    若统一来源未配置 key，回退到助手设置页 DB 中加密存储的 key（向后兼容）。
    """
    from app.services import api_config_service
    creds = api_config_service.get_openai_creds()
    if creds.get('api_key'):
        return creds
    # 回退：DB 加密存储的 key
    settings = get_assistant_settings()
    base_url = creds.get('base_url') or settings.get('openai_base_url', 'https://api.openai.com/v1')
    if base_url and not base_url.rstrip('/').endswith('/v1'):
        base_url = base_url.rstrip('/') + '/v1'
    return {
        'api_key': settings.get('openai_api_key', ''),
        'base_url': base_url,
        'model': creds.get('model') or settings.get('openai_model', 'gpt-4o-mini'),
    }


def is_configured() -> bool:
    """检查 AI 助手是否已配置好 OpenAI API Key。"""
    creds = get_openai_credentials()
    return bool(creds.get('api_key'))


def update_assistant_settings(data: dict) -> dict:
    """更新 AI 助手配置。data 中的 key 为配置项名，空字符串表示不修改。"""
    db = get_db()
    cursor = db.cursor()

    # 先确保有一行记录
    cursor.execute('INSERT OR IGNORE INTO assistant_settings (id) VALUES (?)', (SETTINGS_ROW_ID,))

    fields = []
    values = []
    if 'openai_api_key' in data:
        fields.append('openai_api_key = ?')
        values.append(_encrypt(data['openai_api_key']))
    if 'openai_base_url' in data:
        fields.append('openai_base_url = ?')
        values.append(data['openai_base_url'])
    if 'openai_model' in data:
        fields.append('openai_model = ?')
        values.append(data['openai_model'])
    if 'max_messages_per_session' in data:
        fields.append('max_messages_per_session = ?')
        values.append(int(data['max_messages_per_session']))
    if 'max_write_actions_per_session' in data:
        fields.append('max_write_actions_per_session = ?')
        values.append(int(data['max_write_actions_per_session']))
    if 'confirmation_ttl_seconds' in data:
        fields.append('confirmation_ttl_seconds = ?')
        values.append(int(data['confirmation_ttl_seconds']))

    if fields:
        fields.append('updated_at = CURRENT_TIMESTAMP')
        values.append(SETTINGS_ROW_ID)
        cursor.execute(
            f'UPDATE assistant_settings SET {", ".join(fields)} WHERE id = ?',
            values
        )
        db.commit()

    return get_assistant_settings()


def get_settings_for_display() -> dict:
    """返回可在设置页展示的配置（API Key 脱敏）。"""
    settings = get_assistant_settings()
    key = settings.get('openai_api_key', '')
    if key:
        settings['openai_api_key'] = key[:8] + '...' + key[-4:]
    return settings
