FROM python:3.11-slim

WORKDIR /app

# Зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код
COPY . .

# Volumes для персистентных данных
VOLUME ["/app/data", "/app/logs"]

ENV PYTHONUNBUFFERED=1

# Healthcheck: бот поднимает HTTP /health на 8080 при старте
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')" || exit 1

CMD ["python", "main.py"]
