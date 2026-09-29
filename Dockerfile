FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && groupadd --gid 10001 monitor \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin monitor \
    && mkdir -p /data /logs \
    && chown 10001:10001 /data /logs

USER 10001:10001
EXPOSE 8090
ENTRYPOINT ["rtmp-monitor"]
CMD ["server", "--config", "/etc/rtmp-monitor/central.yaml"]
