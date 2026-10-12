# syntax=docker/dockerfile:1.6
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PUID=1000 \
    PGID=1000 \
    TZ=UTC \
    LOG_LEVEL=INFO

# Strumenti di masterizzazione + utility:
#   wodim          -> CD audio/dati e ISO9660
#   dvd+rw-tools   -> growisofs + dvd+rw-mediainfo per DVD e detection media
#   genisoimage    -> creazione ISO da cartella (uso futuro)
#   eject          -> espulsione disco
#   tini           -> init PID 1 corretto (signal forwarding)
#   gosu           -> drop di privilegi nel entrypoint
RUN apt-get update && apt-get install -y --no-install-recommends \
      wodim \
      dvd+rw-tools \
      genisoimage \
      eject \
      tini \
      gosu \
      ca-certificates \
      tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app/ ./app/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
