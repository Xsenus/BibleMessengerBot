FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app && useradd --system --gid app --home-dir /app app
COPY requirements.txt requirements-test.txt ./
RUN python -m pip install -r requirements.txt -r requirements-test.txt \
    && python -m pip check && python -m pip freeze > /app/BUILD-DEPENDENCIES.txt
COPY . /app
RUN mkdir -p /app/cache /app/backups && chown -R app:app /app
USER app
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "app.bot.main"]
