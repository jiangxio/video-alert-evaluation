"""统一 API Token 配置路由"""
from flask import Blueprint, request, jsonify, render_template

from app.services import api_config_service

bp = Blueprint('api_config', __name__, url_prefix='/api-config')


@bp.route('/')
def config_page():
    """统一 API 配置页。"""
    config = api_config_service.get_config_for_display()
    return render_template('api_config.html', config=config)


@bp.route('/api/config', methods=['GET'])
def api_get_config():
    """获取当前配置（密钥不返回，仅返回是否已配置）。"""
    return jsonify({'success': True, 'config': api_config_service.get_config_for_display()})


@bp.route('/api/save', methods=['POST'])
def api_save():
    """保存配置。密钥写 .env，非敏感项写 DB。"""
    data = request.get_json() or {}
    try:
        config = api_config_service.save_config(data)
        return jsonify({'success': True, 'config': config})
    except Exception as e:
        return jsonify({'success': False, 'error': f'保存失败：{e}'}), 500


@bp.route('/api/test', methods=['POST'])
def api_test():
    """测试连接。"""
    data = request.get_json() or {}
    provider = data.get('provider')
    if provider in ('text', 'openai'):  # openai 兼容旧前端
        return jsonify(api_config_service.test_text_llm())
    if provider == 'vision':
        return jsonify(api_config_service.test_vision())
    return jsonify({'ok': False, 'msg': '未知的 provider'}), 400
