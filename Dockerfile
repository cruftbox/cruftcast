FROM python:3.14-slim

# ffmpeg joins the per-turn MP3s and measures episode length.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Data directory for the ./data bind mount (database, episodes, artwork)
RUN mkdir -p /app/data

EXPOSE 8091

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"CRUFTCAST_PORT\", \"8091\")}/health', timeout=4)"

CMD ["python", "main.py"]
