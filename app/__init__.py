"""Flask应用工厂"""
from flask import Flask
from app.config import Config


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    # 初始化配置（创建必要的目录）
    config_class.init_app(app)

    # 初始化数据库
    from app import database
    database.init_app(app)

    # 注册蓝图
    from app.routes import videos, alerts, verification, evaluation, auto_annotation, streaming, algorithms
    app.register_blueprint(videos.bp)
    app.register_blueprint(alerts.bp)
    app.register_blueprint(verification.bp)
    app.register_blueprint(evaluation.bp)
    app.register_blueprint(auto_annotation.bp)
    app.register_blueprint(streaming.bp)
    app.register_blueprint(algorithms.bp)

    # 首页路由
    @app.route('/')
    def index():
        from flask import render_template
        return render_template('index.html')

    @app.errorhandler(413)
    def request_entity_too_large(error):
        from flask import jsonify
        max_mb = app.config.get('MAX_CONTENT_LENGTH', 0) / 1024 / 1024
        return jsonify({'error': f'文件超过上传限制（最大 {max_mb:.0f} MB）'}), 413

    return app
