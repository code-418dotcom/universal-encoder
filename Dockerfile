FROM python:3.11-slim

# Install ffmpeg
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app

EXPOSE 8000

ENV TARGET_DIR=/data \
    CRF=22 \
    PRESET=veryfast \
    AUDIO_BITRATE=160k \
    USE_NVENC=false \
    KEEP_ORIGINAL=false \
    PRESERVE_TIMESTAMPS=true \
    DRY_RUN=false

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
