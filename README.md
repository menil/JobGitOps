# JobGitOps

A GitOps-driven job application and tracking system. This project automates resume compilation, job listing updates, and application tracking using a GitOps-based workflow.

## Features

- 📄 **Dynamic PDF Resumes**: Powered by WeasyPrint and Jinja2 templates, compiling HTML/CSS structures directly to PDFs.
- ⚡ **Modern Python Environment**: Configured via Python 3.11, Nix, `devenv`, and `uv` for reproducible, isolated packages.
- ❄️ **Hermetic Nix Environment**: Automated environment setup with all native WeasyPrint dependencies (`cairo`, `pango`, `glib`, `gdk-pixbuf`, `harfbuzz`, `libffi`) and font directories mapped cleanly inside the shell.
- 🛠️ **Local Task Runner (`Justfile`)**: Standardized commands for formatting, linting, type-checking, and validating.
- 🛡️ **Git Hooks**: Pre-configured pre-commit quality gate validations (`just validate`) and conventional commit title verification.
- 🤖 **Automated PR Reviews**: Integrated via `menil/pr-code-review-action` using OpenRouter.

---

## Getting Started

### 1. Developer Environment (devenv & direnv)

This project uses `devenv` to manage system dependencies and the virtualenv. To get started:

1. Install [Nix](https://nixos.org/download) and [devenv](https://devenv.sh/getting-started/).
2. Run `devenv shell` to enter the environment, or configure `direnv` with `direnv allow` to load the environment automatically upon entering the repository.
3. Git hooks will be automatically registered on entering the devenv shell.

### 2. Task Runner (`Justfile`)

Tasks are managed via `just`:
- `just`: List all available recipes.
- `just format`: Format code and configurations using Ruff.
- `just lint`: Lint codebase using Ruff.
- `just validate`: Execute linting, formatting check, and the test suite with a 90% coverage threshold.

### 3. Git Hooks

- **`commit-msg`**: Validates commit titles follow the [Conventional Commits](https://www.conventionalcommits.org/) format.
- **`pre-commit`**: Automatically runs `just validate` inside the hermetic devenv shell. If you are already within an active devenv shell, it executes directly to avoid startup latency.
