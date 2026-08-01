# JobGitOps

A GitOps-driven job application and tracking system. This project automates resume compilation, job listing updates, and application tracking using a GitOps-based workflow.

## Features

- 📄 **Dynamic PDF Resumes**: Powered by WeasyPrint and Jinja2 templates, compiling HTML/CSS structures directly to PDFs.
- ⚡ **Modern Python Environment**: Configured via Python 3.11, Nix, `devenv`, and `uv` for reproducible, isolated packages.
- ❄️ **Hermetic Nix Environment**: Automated environment setup with all native WeasyPrint dependencies (`cairo`, `pango`, `glib`, `gdk-pixbuf`, `harfbuzz`, `libffi`) and font directories mapped cleanly inside the shell.
- 🛠️ **Local Task Runner (`Justfile`)**: Standardized commands for formatting, linting, type-checking, and validating.
- 🛡️ **Git Hooks**: Pre-configured pre-commit quality gate validations (`just validate`) and conventional commit title verification.
- 🤖 **Automated PR Reviews**: Integrated via `menil/pr-code-review-action` using OpenRouter.

## Issue Labels

Labels (names, colors, descriptions) are managed as code in `.github/labels.yml` and applied automatically by the `.github/workflows/sync-labels.yml` workflow on every push to `main`. Keep both files together when forking or vendoring this repo — the triage engine adds labels that must already exist in the repository.

---

## Getting Started

Detailed instructions for setting up the developer environment, running quality gates with the task runner (`Justfile`), verifying Git hooks, and tracking issues via Beads can be found in [AGENTS.md](AGENTS.md).
