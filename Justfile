# Project Task Runner

# List available recipes
default:
    @just --list

format:
    ruff format .
    just format-resume
    npm run format --prefix installer

# Rewrite the canonical resume fixture in canonical form
format-resume:
    python scripts/format_resume.py

format-check:
    ruff format --check .
    just format-check-resume
    npm run format:check --prefix installer

# Check the canonical resume fixture is canonical without modifying it
format-check-resume:
    python scripts/format_resume.py --check

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
