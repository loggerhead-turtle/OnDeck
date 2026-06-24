FROM python:3.12-slim

# ffmpeg is needed by yt-dlp to convert downloaded audio to MP3
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# /data is the Render persistent disk mount point (set via env var at runtime)
ENV ONDECK_HOME=/data/ondeck

EXPOSE 10000

CMD gunicorn --workers 1 --bind 0.0.0.0:${PORT:-10000} --timeout 300 web.app:app
