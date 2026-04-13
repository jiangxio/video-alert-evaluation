"""Flask应用配置"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
UPLOAD_FOLDER = BASE_DIR / 'uploads'
UPLOAD_VIDEOS = UPLOAD_FOLDER / 'videos'
UPLOAD_ALERTS = UPLOAD_FOLDER / 'alerts'

# 确保目录存在
UPLOAD_VIDEOS.mkdir(parents=True, exist_ok=True)
UPLOAD_ALERTS.mkdir(parents=True, exist_ok=True)


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-in-production'
    UPLOAD_FOLDER = str(UPLOAD_FOLDER)
    UPLOAD_VIDEOS = str(UPLOAD_VIDEOS)
    UPLOAD_ALERTS = str(UPLOAD_ALERTS)
    MAX_CONTENT_LENGTH = 500 * 1024 * 1024  # 500MB max upload

    # 项目路径
    PROJECT_ROOT = str(BASE_DIR)
    OUTPUT_DIR = str(BASE_DIR / 'output')
    GROUND_TRUTH_DIR = str(BASE_DIR / 'ground_truth')
    REPORT_DIR = str(BASE_DIR / 'report')
    THUMBNAILS_DIR = str(BASE_DIR / 'thumbnails')
    GENERATED_VIDEOS_DIR = str(BASE_DIR / 'generated_videos')

    # 允许的文件扩展名
    ALLOWED_VIDEO_EXTENSIONS = {'mp4', 'avi', 'mov', 'mkv'}
    ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'bmp'}

    @classmethod
    def init_app(cls, app):
        # 确保必要的目录存在
        Path(cls.THUMBNAILS_DIR).mkdir(parents=True, exist_ok=True)
        Path(cls.GENERATED_VIDEOS_DIR).mkdir(parents=True, exist_ok=True)
