FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STATE_DIR=/data

WORKDIR /app
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && useradd --system --uid 10001 --home /nonexistent bridge \
 && mkdir -p /data && chown bridge /data

COPY app/bridge.py .

USER bridge
VOLUME ["/data"]

# Healthy if the main loop touched the heartbeat in the last 3 minutes
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import os,time,sys; sys.exit(0 if time.time()-os.path.getmtime('/data/heartbeat')<180 else 1)"

ENTRYPOINT ["python", "-I", "/app/bridge.py"]
