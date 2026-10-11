# Build from team-mcp-gateway/:
#   docker build -f services/store-operator/Dockerfile -t store-operator:0.1.0 .
FROM alpine/helm:3.16.3 AS helm

FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
COPY --from=helm /usr/bin/helm /usr/bin/helm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 \
    HOME=/tmp CHARTS_DIR=/charts PYTHONPATH=/srv \
    USER=store-operator
WORKDIR /srv
COPY services/store-operator/pyproject.toml services/store-operator/uv.lock ./
RUN uv sync --frozen --no-dev
COPY services/store-operator/store_operator ./store_operator
# The charts every store is installed from (Bitnami WordPress is vendored in charts/store/charts).
COPY charts/store /charts/store
COPY charts/woo-mcp /charts/woo-mcp
COPY charts/gateway /charts/gateway
ENV PATH="/srv/.venv/bin:$PATH"

USER 10001:10001
CMD ["sh", "-c", "kopf run -m store_operator.handlers --standalone --namespace \"$STORES_NAMESPACE\" --liveness=http://0.0.0.0:8080/healthz"]
