default:
    @just --list

sync:
    uv sync
    uv run prek install

dev:
    uv run lst-bot

format:
    uv run ruff format

lint: format
    uv run ruff check --fix

tc: lint
    uv run ty check

test: tc
    uv run pytest

check:
    uv lock --check
    uv run ruff format --check
    uv run ruff check
    uv run ty check

build: check
    uv build --all-packages --no-create-gitignore --no-sources

clean:
    fd -I -t d -F __pycache__ -x rm -rf
    rm -rf dist/ .pytest_cache/ .ruff_cache/
