## Linting helpers
.PHONY: lint format fix check

lint:
	ruff check .

format:
	ruff format .

fix:
	ruff check . --fix

check:
	ruff format . --check
	ruff check .
