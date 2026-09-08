FROM python:3.12.13-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NEWS_MONITOR_DB_PATH=/app/data/news.db

WORKDIR /app

# Install dependencies separately so source edits reuse this layer.
COPY requirements.lock ./
RUN python -m pip install -r requirements.lock && python -m pip check

RUN groupadd --gid 10001 monitor \
    && useradd --uid 10001 --gid monitor --create-home monitor \
    && mkdir /app/data \
    && chown monitor:monitor /app/data

COPY app.py config.yaml ./
COPY monitor/ ./monitor/
COPY .streamlit/config.toml ./.streamlit/config.toml

USER monitor
EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3)"

CMD ["python", "-m", "streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]
