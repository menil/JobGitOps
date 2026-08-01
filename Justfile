# Project Task Runner

# List available recipes
default:
    @just --list

# Format code and configuration files
format:
    ruff format .
    just format-resume

# Rewrite resumes/resume.yaml in canonical form
format-resume:
    python scripts/format_resume.py

# Check formatting
format-check:
    ruff format --check .
    just format-check-resume

# Check resumes/resume.yaml is canonical without modifying it
format-check-resume:
    python scripts/format_resume.py --check

# Run code and markdown linting checks
lint:
    ruff check .

# Run all local checks (tests, format checks, lints)
validate:
    just lint
    just format-check
    pytest --cov=src --cov-fail-under=90 tests/
