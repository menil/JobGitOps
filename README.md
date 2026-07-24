# Project Template

A generic, modern project template pre-configured with developer tooling, Nix integration, local git hook validation, and automated AI code reviews.

## Features

- 🤖 **Automated PR Reviews**: Integrated via `menil/pr-code-review-action` using OpenRouter (free tier by default).
- ❄️ **Nix Shell**: Pre-configured `shell.nix` for consistent, reproducible developer environments.
- 🛠️ **Local Task Runner (`Justfile`)**: Standardized commands for formatting, linting, and validating code.
- 🛡️ **Git Hooks**: Pre-configured conventional commit title checks and automatic pre-commit quality checks.
- ⚡ **Direnv Ready**: Automatically configures local git hooks upon entering the directory.

---

## Getting Started

### 1. Create a Repository from this Template

Click the **"Use this template"** button on GitHub, or create it via the GitHub CLI:
```bash
gh repo create my-new-project --template menil/project-template --private --clone
```

### 2. Configure GitHub Secrets

For the automated PR code reviews to run successfully, navigate to your new repository's **Settings > Secrets and variables > Actions** and add:

* **`OPENROUTER_API_KEY`**: Your OpenRouter API Key.

*(Note: The template uses GitHub's Action Sharing to fetch `menil/pr-code-review-action` keylessly. Ensure you have configured the action repository under **Settings > Actions > General > Access** to be accessible from other repositories owned by your user account).*

---

## Development Environment

### Nix Shell
Activate the Nix developer shell to load project tools:
```bash
nix-shell
```

### Task Runner (`Justfile`)
The following tasks are available via `just`:
- `just`: List all available tasks.
- `just format`: Format code and configuration files.
- `just lint`: Run code and markdown linters.
- `just validate`: Execute all formatting, linting, and verification checks.

### Git Hook Checks
The project automatically configures local Git hooks:
- **`commit-msg`**: Validates that all commit titles adhere to the [Conventional Commits](https://www.conventionalcommits.org/) standard (e.g. `feat: add database support`).
- **`pre-commit`**: Automatically runs `just validate` before allowing a commit. If any check fails, the commit is aborted.
