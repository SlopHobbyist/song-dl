FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# YouTube downloads: yt-dlp needs a JavaScript runtime to read YouTube's formats, and ffmpeg
# to take the audio out of its video container and tag it
COPY --from=denoland/deno:bin-2.9.7 /deno /usr/local/bin/deno
COPY --from=mwader/static-ffmpeg:9.0.2 /ffmpeg /ffprobe /usr/local/bin/
ENV DENO_DIR=/tmp/deno

WORKDIR /srv
COPY server/requirements.txt .
RUN pip install -r requirements.txt

COPY server/songdl ./songdl
COPY webui ./webui

# Paths of the volumes mounted in docker-compose.yaml
ENV SONGDL_CONFIG=/config/config.yaml \
    SONGDL_SLSKD_DIR=/slskd \
    SONGDL_SLSKD_URL=http://slskd:5030 \
    SONGDL_LLM_URL=http://llm:8080 \
    SONGDL_DOWNLOADS=/downloads \
    SONGDL_CERTS=/certs \
    SONGDL_DATA=/data \
    SONGDL_WEBUI=/srv/webui \
    SONGDL_BIND=0.0.0.0:8080

EXPOSE 8080
CMD ["python", "-m", "songdl", "serve"]
