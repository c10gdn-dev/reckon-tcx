# Overridable so CI can pin the exact renderer. Layout shifts between plantuml
# versions, and the drift check compares rendered bytes, so an unpinned CI would
# fail on a diagram nobody touched.
PLANTUML ?= plantuml

.PHONY: help install test cov lint fmt check clean diagrams check-diagrams

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

analyse:  ## Report the factor distribution over training-data/
	uv run reckon analyse

diagrams:  ## Render docs/diagrams/*.puml to SVG
	$(PLANTUML) -tsvg docs/diagrams/*.puml
	@# The renderer stamps its own version into the output. That is not part of
	@# the diagram, and leaving it in would make every plantuml upgrade look
	@# like a diagram change to the drift check in CI.
	@for f in docs/diagrams/*.svg; do \
		sed -i.bak 's|<?plantuml [^?]*?>||' "$$f" && rm -f "$$f.bak"; \
	done
	@# plantuml exits 0 on a file containing no diagram at all, having produced
	@# nothing, so insist on one SVG per source. (A missing @enduml is not that
	@# case: plantuml treats it as an implicit end and renders correctly.)
	@for f in docs/diagrams/*.puml; do \
		test -f "$${f%.puml}.svg" || { \
			echo "$$f produced no SVG; is @enduml missing?" >&2; exit 1; }; \
	done
	@echo "rendered $$(ls docs/diagrams/*.svg | wc -l | tr -d ' ') diagrams"

check-diagrams:  ## Fail if a .puml is invalid or its .svg is out of date
	$(PLANTUML) -failfast2 -checkonly docs/diagrams/*.puml
	$(MAKE) diagrams
	git diff --exit-code -- docs/diagrams

mutate:  ## Mutation-test the transform (advisory, slow)
	uv run mutmut run --no-progress || true
	uv run mutmut results

check: lint test  ## Everything CI runs

clean:
	rm -rf .pytest_cache .ruff_cache .coverage coverage.xml htmlcov build dist .mutmut-cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
