.PHONY: help install test cov lint fmt check clean

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

install:  ## Create .venv and install the project with dev dependencies
	uv sync

test:  ## Run the test suite with the 100% coverage gate
	uv run pytest

cov:  ## Write an HTML coverage report to htmlcov/
	uv run pytest --cov-report=html
	@echo "open htmlcov/index.html"

lint:  ## Lint and check formatting
	uv run ruff check .
	uv run ruff format --check .

fmt:  ## Apply formatting and autofixes
	uv run ruff format .
	uv run ruff check --fix .

check: lint test  ## Everything CI runs

clean:
	rm -rf .pytest_cache .ruff_cache .coverage coverage.xml htmlcov build dist
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
