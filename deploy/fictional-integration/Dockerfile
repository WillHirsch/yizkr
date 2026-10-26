FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim
WORKDIR /app
ENV UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# The runner builds an allowlisted context, with no archive/runtime files.
# A managed executor can supply its public CA bundle as a build secret.
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi; \
    uv sync --frozen --no-default-groups --group dev
COPY tests ./tests
ENV PATH="/app/.venv/bin:$PATH"
