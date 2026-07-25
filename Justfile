# Project Task Runner

# List available recipes
default:
    @just --list

# Format code and configuration files
format:
    ruff format .

# Check formatting
format-check:
    ruff format --check .

# Run code and markdown linting checks
lint:
    ruff check .

# Run all local checks (tests, format checks, lints)
validate:
    just lint
    just format-check
    pytest --cov=jobgitops --cov-fail-under=90 tests/
