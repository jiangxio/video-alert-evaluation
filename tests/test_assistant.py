"""AI 助手单元测试

运行前请确保已安装依赖：
    pip install -r requirements.txt

运行测试：
    pytest tests/test_assistant.py -v
"""
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from app.database import get_db
from app.services.assistant_settings import get_assistant_settings, update_assistant_settings
from app.services.assistant_tasks import (
    create_pending_confirmation,
    get_pending_confirmation,
    confirm_and_execute,
    create_assistant_task,
    get_assistant_task,
)
from app.services.assistant_tools import (
    list_videos,
    list_event_types,
    analyze_write_tool,
    execute_update_video_tags,
)


@pytest.fixture
def app():
    """创建测试用 Flask 应用。"""
    # 强制使用测试配置，避免被用户真实环境变量覆盖
    test_env = {
        'OPENAI_API_KEY': 'sk-test',
        'OPENAI_BASE_URL': 'https://api.openai.com/v1',
        'OPENAI_MODEL': 'gpt-4o-mini',
        'ASSISTANT_ENCRYPTION_KEY': '',
    }
    with patch.dict(os.environ, test_env, clear=False):
        app = create_app()
        app.config.update({
            'TESTING': True,
            'OPENAI_API_KEY': 'sk-test',
            'OPENAI_BASE_URL': 'https://api.openai.com/v1',
            'OPENAI_MODEL': 'gpt-4o-mini',
            'ASSISTANT_ENCRYPTION_KEY': '',
        })
        with app.app_context():
            # 清理 AI 助手相关表，保证测试隔离
            db = get_db()
            cursor = db.cursor()
            cursor.execute("DELETE FROM assistant_settings")
            cursor.execute("DELETE FROM pending_confirmations")
            cursor.execute("DELETE FROM assistant_audit_log")
            cursor.execute("DELETE FROM assistant_tasks")
            db.commit()
            yield app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def runner(app):
    return app.test_cli_runner()


def test_settings_default_from_env(app):
    """配置应从环境变量/测试配置读取默认值。"""
    with app.app_context():
        settings = get_assistant_settings()
        assert settings['openai_api_key'] == 'sk-test'
        assert settings['openai_base_url'] == 'https://api.openai.com/v1'
        assert settings['openai_model'] == 'gpt-4o-mini'


def test_settings_update(app):
    """更新配置后应能读取新值。"""
    with app.app_context():
        update_assistant_settings({
            'openai_model': 'gpt-4o',
            'max_messages_per_session': '100',
        })
        settings = get_assistant_settings()
        assert settings['openai_model'] == 'gpt-4o'
        assert settings['max_messages_per_session'] == 100


def test_pending_confirmation_lifecycle(app):
    """待确认记录的创建、查询、执行流程应正常。"""
    with app.app_context():
        db = get_db()
        cursor = db.cursor()
        # 清理
        cursor.execute("DELETE FROM pending_confirmations WHERE session_id = 'test-session'")
        db.commit()

        confirmation_id = create_pending_confirmation(
            action='delete_video',
            params={'video_id': '046'},
            summary='删除视频 046',
            session_id='test-session',
            ttl_seconds=300,
        )
        assert confirmation_id

        pending = get_pending_confirmation(confirmation_id, 'test-session')
        assert pending is not None
        assert pending['action'] == 'delete_video'

        def executor(action, params):
            return {'deleted': params['video_id']}

        result = confirm_and_execute(confirmation_id, 'test-session', executor)
        assert result['success'] is True
        assert result['result']['deleted'] == '046'


def test_create_assistant_task(app):
    """统一任务表的创建与读取应正常。"""
    with app.app_context():
        task_id = create_assistant_task('batch_ocr', {'dataset_id': 1})
        assert task_id
        task = get_assistant_task(task_id)
        assert task['task_type'] == 'batch_ocr'
        assert task['status'] == 'pending'


def test_list_event_types(app):
    """list_event_types 应返回事件类型列表。"""
    with app.app_context():
        result = list_event_types()
        assert 'event_types' in result


def test_analyze_update_video_tags_existing_video(app):
    """分析给不存在的视频打标签应返回错误。"""
    with app.app_context():
        result = analyze_write_tool('update_video_tags', {
            'video_id': 'NON_EXISTENT',
            'events': [{'type': 'rat', 'start': 10, 'end': 20}],
            'mode': 'append',
        })
        assert 'error' in result


def test_chat_endpoint_without_config(client):
    """未配置 API Key 时聊天接口应返回 NOT_CONFIGURED。"""
    with patch('app.routes.assistant.is_configured', return_value=False):
        resp = client.post('/assistant/api/chat', json={'message': 'hello'})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['type'] == 'error'
    assert data['error_code'] == 'NOT_CONFIGURED'


def test_settings_page(client):
    """设置页应返回 200。"""
    resp = client.get('/assistant/settings')
    assert resp.status_code == 200


def test_widget_included_in_base(client):
    """首页应包含 AI 助手组件。"""
    resp = client.get('/')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'assistant-widget' in html
    assert 'assistant.js' in html
