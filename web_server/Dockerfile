FROM python:3.9-slim

# === 关键修正：使用新的包名，并添加 --no-install-recommends 减小体积 ===
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安装 Python 库 (使用清华源加速，防止超时)
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
    ultralytics \
    redis \
    flask \
    opencv-python-headless \
    psutil

COPY . .
CMD ["python", "main_web.py"]
