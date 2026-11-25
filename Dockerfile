FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/home/oracle

WORKDIR /app
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 oracle \
    && useradd --uid 10001 --gid 10001 --home-dir /home/oracle --create-home --shell /usr/sbin/nologin oracle \
    && mkdir -p /data \
    && chown 10001:10001 /data

COPY --chown=10001:10001 snatch.py ./snatch.py
USER 10001:10001
VOLUME ["/data"]
ENTRYPOINT ["python", "/app/snatch.py"]
