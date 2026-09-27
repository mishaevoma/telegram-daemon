FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    XDG_CONFIG_HOME=/config \
    XDG_DATA_HOME=/data

WORKDIR /app
RUN pip install --no-cache-dir uv==0.11.7
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable \
    && useradd --uid 10001 --create-home daemonuser \
    && mkdir -p /config/telegram-daemon /data/telegram-daemon \
    && chown -R daemonuser:daemonuser /config /data

USER daemonuser
ENTRYPOINT ["/app/.venv/bin/tgdaemon"]
CMD ["run"]
