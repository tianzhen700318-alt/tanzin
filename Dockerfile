# 使用一个稳定且轻量的 Python 基础镜像
FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制整个项目
COPY . .

# 声明要运行的端口 (Render 会通过环境变量 PORT 注入)
ENV PORT=10000

# 启动命令
CMD exec gunicorn --bind 0.0.0.0:$PORT zin:app