# Project Task Runner

# List available recipes
default:
    @just --list

# Format code and configuration files
format:
    ruff format .
    just format-resume

# Rewrite the canonical resume fixture in canonical form
format-resume:
    python scripts/format_resume.py

# Check formatting
format-check:
    ruff format --check .
    just format-check-resume

# Check the canonical resume fixture is canonical without modifying it
format-check-resume:
    python scripts/format_resume.py --check

# Run code and markdown linting checks
lint:
    ruff check .

# Shellcheck all POSIX shell scripts
shellcheck:
    shellcheck scripts/*.sh

# Run all local checks (tests, format checks, lints, shellcheck)
validate:
    just lint
    just shellcheck
    just format-check
    pytest --cov=src --cov-fail-under=90 tests/
