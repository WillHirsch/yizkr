# Development and operations verbs. Connection settings: deploy/.env.
UV ?= uv
# Single-host run with in-network hostname resolution (no /etc/hosts edit).
COMPOSE_LOCAL = docker compose -f docker-compose.yml -f docker-compose.localhost.yml
# Feed the host uid/gid to the containerised CLI so files it writes into the
# ../var bind mount are owned by you, not root (compose reads ${UID}/${GID}).
export UID := $(shell id -u)
export GID := $(shell id -g)

.PHONY: install hooks test test-live lint fmt up down up-local cli bootstrap smoke publish-queries wdqs-sandbox-up wdqs-sandbox-down

install:            ## create/update the uv-managed venv (with dev tools)
	$(UV) sync --dev

# Hooks live in the common git dir, so one run covers every worktree.
# The pre-push hook is a plain script, not a pre-commit stage: pre-commit
# shows its hooks only the first pushed ref with new commits, and none for
# a delete.
PRE_PUSH_HOOK = scripts/git-hooks/pre-push

hooks:              ## install git hooks: pre-commit (ruff), pre-push (no pushes to main)
	$(UV) run pre-commit install
	@dest="$$(git rev-parse --git-path hooks)/pre-push"; \
	if [ -e "$$dest" ] && ! grep -q '^# yizkr-pre-push:' "$$dest"; then \
		echo "make hooks: $$dest exists and is not yizkr's;" \
			"move it aside and re-run" >&2; \
		exit 1; \
	fi; \
	install -m 755 $(PRE_PUSH_HOOK) "$$dest" && \
	echo "pre-push installed at $$dest"

test:               ## offline unit tests
	$(UV) run pytest

test-live:          ## integration tests against the running stack (make up first)
	$(UV) run pytest -m live tests/integration -v

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

fmt:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

up:                 ## start the Wikibase Suite stack
	cd deploy && docker compose up -d --wait

down:               ## stop the stack (data persists in volumes)
	cd deploy && docker compose down

up-local:           ## start the stack; hostnames resolve in-network (no /etc/hosts)
	cd deploy && $(COMPOSE_LOCAL) up -d --wait

cli:                ## run a yizkr command in-network, e.g. make cli ARGS="smoke-test"
	cd deploy && $(COMPOSE_LOCAL) --profile cli run --rm yizkr $(ARGS)

bootstrap:          ## apply model/manifest.yaml to the instance
	$(UV) run yizkr bootstrap

smoke:              ## deployment smoke test
	$(UV) run yizkr smoke-test

publish-queries:    ## publish the SPARQL library to wiki pages
	$(UV) run yizkr publish-queries

# Query reviews: a throwaway, network-less query engine, separate from the
# stack, one per checkout. Load a scripts/synthetic_graph.py graph into it
# and run queries with scripts/wdqs_sandbox.py (see its --help).
wdqs-sandbox-up:    ## start a network-less query-engine sandbox (768 MB cap)
	$(UV) run python scripts/wdqs_sandbox.py up

wdqs-sandbox-down:  ## remove the query-engine sandbox (safe to repeat)
	$(UV) run python scripts/wdqs_sandbox.py down
