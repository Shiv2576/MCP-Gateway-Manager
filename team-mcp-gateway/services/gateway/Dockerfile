FROM python:3.13-slim

# uv is a single binary; copy it in from its official image.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /srv

# Dependencies first, from the lock file, so this layer is cached until they change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app

# Use the virtualenv uv created, without needing uv at runtime.
ENV PATH="/srv/.venv/bin:$PATH"

# Run as an unprivileged, numeric user so Kubernetes can verify runAsNonRoot.

USER 10001:10001
EXPOSE 8000

# --factory calls create_app() at startup: missing config stops the pod at once.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
