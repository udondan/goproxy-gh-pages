PRETTIER := npx --yes prettier@3.9.10

.PHONY: lint format prettier test coverage compat bench actions all

all: lint test compat actions

lint: prettier
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

format:
	uv run ruff check --fix .
	uv run ruff format .
	$(PRETTIER) --write --log-level warn '**/*.{md,yml,yaml,json}'

prettier:
	$(PRETTIER) --check --log-level warn '**/*.{md,yml,yaml,json}'

test:
	uv run pytest

coverage:
	uv run coverage run -m pytest
	uv run coverage report

compat:
	cd tests/compat && go test ./...
	bash tests/compat/e2e.sh

bench:
	uv run python tests/bench/bench.py --versions 30 --size-mb 10

actions:
	mise exec -- actionlint
	mise exec -- shellcheck run.sh tests/compat/*.sh
	mise exec -- zizmor .
