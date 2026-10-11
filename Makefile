UV ?= uv

.PHONY: check fmt test dev openapi

check:
	$(UV) run ruff format --check .
	$(UV) run ruff check .
	$(UV) run mypy
	$(UV) run pytest -q

fmt:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

test:
	$(UV) run pytest -q

dev:
	UAM_FAKES=1 UAM_DATA_DIR=./.dev-data $(UV) run python -m uam

openapi:
	$(UV) run python -m uam.web.openapi > docs/openapi.json
