FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    DATABASE_PATH=/data/platform.sqlite3 \
    APP_ENV=production \
    COOKIE_SECURE=1 \
    ALLOW_CONSOLE_PASSWORD_RESET=0

WORKDIR /app
COPY --chown=10001:10001 . /app
RUN mkdir -p /data /app/instance && chown -R 10001:10001 /data /app/instance

USER 10001:10001
EXPOSE 8000
VOLUME ["/data"]

CMD ["python", "server.py"]
