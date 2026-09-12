#!/bin/sh
# Docker 容器内启动：主平台 8080（前台，waitress）+ od 目标检测 5000（后台）
# 主进程退出时容器停止，od 后台进程随之结束。
cd /app
python od-dataset-manager/app.py &
exec python run.py
