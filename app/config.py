"""Flask应用配置"""
import os
import secrets
import logging
from pathlib import Path

# 自动加载 .env 文件中的环境变量（.env 优先于系统环境变量）
from dotenv import load_dotenv
BASE_DIR = Path(__file__).parent.parent
load_dotenv(BASE_DIR / '.env', override=True)

UPLOAD_FOLDER = BASE_DIR / 'uploads'
UPLOAD_VIDEOS = UPLOAD_FOLDER / 'videos'
UPLOAD_ALERTS = UPLOAD_FOLDER / 'alerts'

# 确保目录存在
UPLOAD_VIDEOS.mkdir(parents=True, exist_ok=True)
UPLOAD_ALERTS.mkdir(parents=True, exist_ok=True)


class Config:
    # 优先从环境变量读取；未设置时生成随机密钥（不可预测，防 session 伪造），
    # 但每次重启会令旧 session 失效——生产环境务必在 .env 中设置固定的 SECRET_KEY。
    SECRET_KEY = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
    if not os.environ.get('SECRET_KEY'):
        logging.getLogger(__name__).warning(
            'SECRET_KEY 未在环境变量中设置，已生成随机临时密钥；重启后所有 session 将失效。'
            '生产环境请在 .env 中配置固定的 SECRET_KEY。'
        )
    UPLOAD_FOLDER = str(UPLOAD_FOLDER)
    UPLOAD_VIDEOS = str(UPLOAD_VIDEOS)
    UPLOAD_ALERTS = str(UPLOAD_ALERTS)
    MAX_CONTENT_LENGTH = int(1.3 * 1024 * 1024 * 1024)  # 1.3GB max upload

    # 项目路径
    PROJECT_ROOT = str(BASE_DIR)
    OUTPUT_DIR = str(BASE_DIR / 'output')
    GROUND_TRUTH_DIR = str(BASE_DIR / 'ground_truth')
    REPORT_DIR = str(BASE_DIR / 'report')
    THUMBNAILS_DIR = str(BASE_DIR / 'thumbnails')
    GENERATED_VIDEOS_DIR = str(BASE_DIR / 'generated_videos')
    ALERT_TYPES_CONFIG = str(BASE_DIR / 'config' / 'alert_types.json')
    EXTRACTED_FRAMES_DIR = str(BASE_DIR / 'extracted_frames')

    # 允许的文件扩展名
    ALLOWED_VIDEO_EXTENSIONS = {'mp4', 'avi', 'mov', 'mkv'}
    ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'bmp'}

    # AI 助手配置（环境变量为默认值，管理员可在 UI 覆盖）
    OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')
    OPENAI_BASE_URL = os.environ.get('OPENAI_BASE_URL', 'https://api.openai.com/v1')
    OPENAI_MODEL = os.environ.get('OPENAI_MODEL', 'gpt-4o-mini')
    ASSISTANT_ENCRYPTION_KEY = os.environ.get('ASSISTANT_ENCRYPTION_KEY', '')
    ASSISTANT_MAX_MESSAGES_PER_SESSION = int(os.environ.get('ASSISTANT_MAX_MESSAGES_PER_SESSION', '50'))
    ASSISTANT_MAX_WRITE_ACTIONS_PER_SESSION = int(os.environ.get('ASSISTANT_MAX_WRITE_ACTIONS_PER_SESSION', '30'))
    ASSISTANT_CONFIRMATION_TTL_SECONDS = int(os.environ.get('ASSISTANT_CONFIRMATION_TTL_SECONDS', '300'))

    @classmethod
    def init_app(cls, app):
        # 确保必要的目录存在
        Path(cls.THUMBNAILS_DIR).mkdir(parents=True, exist_ok=True)
        Path(cls.GENERATED_VIDEOS_DIR).mkdir(parents=True, exist_ok=True)
        Path(cls.EXTRACTED_FRAMES_DIR).mkdir(parents=True, exist_ok=True)
