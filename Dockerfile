FROM python:3.13-slim-bookworm AS builder

### ENVIRONMENT VARIABLES ###
ENV PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1 \
    UV_LINK_MODE=copy \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    gcc \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock ./

RUN pip install -U pip uv && \
    uv sync --frozen --no-install-project --no-upgrade --no-cache --no-dev --python /usr/local/bin/python3.13 && \
    rm -rf *.egg-info /root/.cache/pip /tmp/* /var/tmp/* && \
    uv cache clean && \
    find ./ -type d -name __pycache__ -exec rm -rf {} +

FROM python:3.13-slim-bookworm AS runner

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libmagic1 \
    libmagic-mgc \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv /app/.venv
COPY . .

EXPOSE 8501

ENV PATH="/app/.venv/bin:$PATH"
ENV STREAMLIT_SERVER_PORT=8501
ENV STREAMLIT_SERVER_ADDRESS=0.0.0.0
ENV PYTHONPATH=/app

CMD ["streamlit", "run", "app/main.py", "--server.address=0.0.0.0", "--server.port=8501", "--server.headless=true"]
