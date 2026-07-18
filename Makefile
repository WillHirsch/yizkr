# Development and operations verbs. Connection settings: deploy/.env.
UV ?= uv
# Single-host run with in-network hostname resolution (no /etc/hosts edit).
COMPOSE_LOCAL = docker compose -f docker-compose.yml -f docker-compose.localhost.yml
# Feed the host uid/gid to the containerised CLI so files it writes into the
# ../var bind mount are owned by you, not root (compose reads ${UID}/${GID}).
export UID := $(shell id -u)
export GID := $(shell id -g)

.PHONY: install hooks test test-live lint fmt up down up-local cli bootstrap smoke publish-queries

install:            ## create/update the uv-managed venv (with dev tools)
	$(UV) sync --dev

hooks:              ## install the git pre-commit hooks (ruff format + lint)
	$(UV) run pre-commit install

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
