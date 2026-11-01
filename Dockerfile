FROM python:3.13-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY bridge.py metadata.py storage.py sonarr_library.py radarr_library.py indexer_discovery.py ./
COPY indexers.json overrides.json ./
COPY LICENSE /app/LICENSE
RUN mkdir -p /app/data && chown -R 1000:1000 /app

USER 1000:1000
EXPOSE 8789
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8789') + '/health', timeout=3).read()"]
CMD ["python", "-u", "bridge.py"]
