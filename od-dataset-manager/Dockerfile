FROM python:3.10-slim-bullseye

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 预创建数据目录（datasets 挂卷持久化，data 供 OD_DB_DIR 放数据库）
RUN mkdir -p datasets data

EXPOSE 5000

CMD ["python", "app.py"]
