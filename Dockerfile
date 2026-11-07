FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MINIHOME_HOST=0.0.0.0 \
    MINIHOME_PORT=8080 \
    MINIHOME_DATA_DIR=/app/data
WORKDIR /app
RUN groupadd --gid 10001 minihome \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin minihome \
    && mkdir -p /app/data \
    && chown -R minihome:minihome /app
COPY --chown=minihome:minihome server.py /app/server.py
COPY --chown=minihome:minihome static/ /app/static/
USER minihome
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=4s --start-period=8s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3)"
CMD ["python", "/app/server.py"]
