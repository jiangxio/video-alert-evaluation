import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 数据库目录（默认与代码同目录；Docker 部署时通过 OD_DB_DIR 指向持久化卷）
DB_DIR = os.environ.get('OD_DB_DIR', BASE_DIR)

IMAGES_DIR = os.path.join(BASE_DIR, "datasets", "calling", "images")
LABELS_DIR = os.path.join(BASE_DIR, "datasets", "calling", "labels")
BACKUP_LABELS_DIR = os.path.join(LABELS_DIR, "backup")

# 自定义类别列表。根据你的实际用例推广可修改这个列表。
CLASSES = ["call_phone", "not_call_phone", "other"]

# 数据导入时允许的图像扩展名
IMAGE_EXTS = ['.jpg', '.jpeg', '.png']
