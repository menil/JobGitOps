# Project Task Runner

# List available recipes
default:
    @just --list

# Format code and configuration files
format:
    @echo "No formatter configured yet. Customize this recipe in the Justfile!"

# Run code and markdown linting checks
lint:
    @echo "No linter configured yet. Customize this recipe in the Justfile!"

# Run all local checks (tests, format checks, lints)
validate:
    @echo "Running project validations..."
    just lint
