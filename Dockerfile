FROM node:26-alpine AS panel
WORKDIR /ui
COPY admin-ui/package.json admin-ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY admin-ui/ ./
RUN npm run build -- --outDir /panel --emptyOutDir

FROM python:3.14-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl tini espeak-ng ffmpeg tesseract-ocr tesseract-ocr-rus \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app && useradd --system --gid app --home-dir /app app
COPY requirements.txt requirements-test.txt constraints-linux.txt ./
COPY requirements-neural.txt constraints-neural-linux.txt ./
RUN python -m pip install -r requirements-neural.txt
RUN python -m pip install -c constraints-linux.txt -r requirements.txt -r requirements-test.txt \
    && python -m pip check && python -m pip freeze > /app/BUILD-DEPENDENCIES.txt
COPY . /app
COPY --from=panel /panel /app/app/web/static/panel
ARG PROJECT_REVISION=unknown
LABEL org.opencontainers.image.revision=$PROJECT_REVISION
RUN python -c 'import json,os; from datetime import datetime,timezone; json.dump({"commit":os.environ["PROJECT_REVISION"],"built_at":datetime.now(timezone.utc).isoformat()},open("/app/BUILD-METADATA.json","w"))'
# The maintenance backup runs as root with all capabilities dropped, so it cannot
# bypass owner-only source permissions inherited from a host with umask 077.
RUN chmod a+rx /app && chmod -R a+rX /app/app \
    && mkdir -p /app/cache/prayer-ai /app/backups && chown -R app:app /app
USER app
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "app.bot.main"]
