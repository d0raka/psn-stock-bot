FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_ROOT_USER_ACTION=ignore
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY bot.py /app/bot.py
RUN mkdir -p /app/data
CMD ["python", "/app/bot.py"]
