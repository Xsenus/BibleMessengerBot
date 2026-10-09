FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl tini espeak-ng ffmpeg tesseract-ocr tesseract-ocr-rus \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system app && useradd --system --gid app --home-dir /app app
COPY requirements.txt requirements-test.txt constraints-linux.txt ./
RUN python -m pip install 'torch==2.10.0+cpu' --index-url https://download.pytorch.org/whl/cpu
COPY requirements-neural.txt constraints-neural-linux.txt ./
RUN python -m pip install -r requirements-neural.txt
RUN python -m pip install -c constraints-linux.txt -r requirements.txt -r requirements-test.txt \
    && python -m pip check && python -m pip freeze > /app/BUILD-DEPENDENCIES.txt
COPY . /app
RUN mkdir -p /app/cache/prayer-ai /app/backups && chown -R app:app /app
USER app
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "app.bot.main"]
