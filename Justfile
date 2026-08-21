# Project Task Runner

# List available recipes
default:
    @just --list

format:
    ruff format .
    npm run format --prefix installer

format-check:
    ruff format --check .
    npm run format:check --prefix installer

lint:
    ruff check .
    npm run lint --prefix installer

# Shellcheck all POSIX shell scripts
shellcheck:
    shellcheck scripts/*.sh

validate:
    just lint
    just shellcheck
    just format-check
    pytest --cov=src --cov-fail-under=90 tests/
    npm run test --prefix installer
