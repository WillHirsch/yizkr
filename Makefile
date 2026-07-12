# Development and operations verbs. Connection settings: deploy/.env.
UV ?= uv

.PHONY: install test test-live lint fmt up down bootstrap smoke publish-queries

install:            ## create/update the uv-managed venv (with dev tools)
	$(UV) sync --dev

test:               ## offline unit tests
	$(UV) run pytest

test-live:          ## integration tests against the running stack (make up first)
	$(UV) run pytest -m live tests/integration -v

lint:
	$(UV) run ruff check src tests
	$(UV) run ruff format --check src tests

fmt:
	$(UV) run ruff format src tests
	$(UV) run ruff check --fix src tests

up:                 ## start the Wikibase Suite stack
	cd deploy && docker compose up -d --wait

down:               ## stop the stack (data persists in volumes)
	cd deploy && docker compose down

bootstrap:          ## apply model/manifest.yaml to the instance
	$(UV) run yizkr bootstrap

smoke:              ## deployment smoke test
	$(UV) run yizkr smoke-test

publish-queries:    ## publish the SPARQL library to wiki pages
	$(UV) run yizkr publish-queries
