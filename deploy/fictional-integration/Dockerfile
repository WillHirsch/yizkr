FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim
WORKDIR /app
ENV UV_LINK_MODE=copy
RUN apt-get update && apt-get install --no-install-recommends --yes git && \
    rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# The runner builds an allowlisted context, with no archive/runtime files.
# A managed executor can supply its public CA bundle as a build secret.
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi; \
    uv sync --frozen --no-default-groups --group dev
COPY tests ./tests
ENV PATH="/app/.venv/bin:$PATH"
# This is a reproducible fixture snapshot, not the original checkout's commit.
# Real retained-code admission observes this repository inside the test image.
RUN printf '%s\n' '.venv/' '__pycache__/' '*.py[cod]' '.pytest_cache/' '.ruff_cache/' > .gitignore && \
    git init --quiet --initial-branch=fictional-integration && \
    git add .gitignore pyproject.toml uv.lock README.md src tests && \
    GIT_AUTHOR_DATE='2026-01-01T00:00:00+0000' \
    GIT_COMMITTER_DATE='2026-01-01T00:00:00+0000' \
    git -c user.name='Fictional Integration' \
        -c user.email='fictional@example.invalid' \
        -c commit.gpgsign=false \
        commit --quiet --message='Record fictional integration source snapshot'
