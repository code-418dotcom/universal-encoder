FROM python:3.11-alpine
RUN apk add --no-cache ffmpeg bash coreutils findutils dos2unix inotify-tools
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY entrypoint.sh /app/entrypoint.sh
RUN dos2unix /app/entrypoint.sh && chmod +x /app/entrypoint.sh
COPY app /app/app
ENV HOST=0.0.0.0 PORT=11223 TARGET_DIR=/data CRF=22 PRESET=veryfast AUDIO_BITRATE=160k USE_NVENC=false KEEP_ORIGINAL=false PRESERVE_TIMESTAMPS=true DRY_RUN=false
EXPOSE 11223
ENTRYPOINT ["/bin/bash","/app/entrypoint.sh"]
