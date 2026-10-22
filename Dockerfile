# The yizkr CLI as a container image, for driving the Wikibase Suite stack
# (smoke-test, bootstrap, ingest, publish-site) WITHOUT adding the wiki
# and query hostnames to the host's /etc/hosts. Paired with
# deploy/docker-compose.localhost.yml, which makes those hostnames resolve
# inside the Docker network. See deploy/README.md ("Run on a single host").
FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app
# deterministic installs into an in-image venv; copy (not hardlink) because
# the cache and the venv are on different layers
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# dependency manifests first so the install layer caches across source edits
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# base package + the `llm` extra (anthropic engine, for API-based ingest).
# `faces` is intentionally omitted — heavy, and not needed to talk to the
# stack. `--llm claude-code` ingest needs that CLI, which is not in the
# image; run those from the host.
RUN uv sync --frozen --no-dev --extra llm

ENV PATH="/app/.venv/bin:$PATH"
ENTRYPOINT ["yizkr"]
CMD ["--help"]
