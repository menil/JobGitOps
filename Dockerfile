# Use python:3.12-slim-bookworm as the base image for a lightweight runner
FROM python:3.12-slim-bookworm

# Prevent interactive prompts during apt package installation
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies for git, just, shellcheck, fontconfig, WeasyPrint
# (still used by the current renderer.py until it migrates to `resumed` --
# see JobGitOps-nx0/JobGitOps-b4m, which will drop these once that lands),
# and Chromium (+ its setuid sandbox helper) for headless PDF export via
# resumed/Puppeteer.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    gh \
    curl \
    shellcheck \
    build-essential \
    libffi-dev \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf2.0-0 \
    shared-mime-info \
    fonts-dejavu \
    fonts-liberation \
    chromium \
    chromium-sandbox \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged account for running Chromium (see the `bun add -g` step below).
RUN useradd --create-home --shell /bin/sh pptruser

# Install Casey's 'just' command runner
RUN curl --proto '=https' --tlsv1.2 -sSf https://just.systems/install.sh | bash -s -- --to /usr/local/bin

# Install uv for ultra-fast python package installation
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install Bun: the JS runtime/package manager for the JSON Resume `resumed`
# CLI. Bun is npm-registry-compatible and ships its own npx equivalent, so no
# separate Node/npm is needed.
COPY --from=oven/bun:latest /usr/local/bin/bun /usr/local/bin/bunx /usr/local/bin/

# Point Puppeteer (used by `resumed export`) at the apt-installed Chromium
# above instead of downloading its own bundled copy. This sidesteps
# Puppeteer's postinstall Chromium download -- the one area with known
# Bun/Puppeteer friction -- and lets apt resolve Chromium's many runtime
# shared-library dependencies instead of Puppeteer's manual dependency list.
ENV PUPPETEER_SKIP_DOWNLOAD=true
ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

# Install Bun's global packages under /opt/bun rather than the default
# ~/.bun (root's home, mode 700) so the unprivileged `pptruser` account above
# can actually read them -- a plain root-owned home directory would block
# traversal entirely for any other user.
ENV BUN_INSTALL=/opt/bun
ENV PATH="/opt/bun/bin:${PATH}"

# Install resumed, puppeteer, and the pinned default resume theme (all
# version-pinned -- see template/config/settings.yaml's own "always pinned"
# policy for why floating versions of code we install-and-execute are a
# supply-chain risk) as global Bun packages -- siblings in the same
# node_modules tree -- so the theme and puppeteer resolve correctly no
# matter which repo checkout resumed runs from. Keep the theme pin in sync
# with settings.yaml's default `theme`.
#
# IMPORTANT, confirmed by hands-on testing: invoke this as
# `su -s /bin/sh pptruser -c "bun /opt/bun/bin/resumed <command> ..."`, NOT
# `bunx resumed` and NOT as root:
# - `bunx` re-resolves `resumed` into an isolated per-invocation cache on
#   every call and cannot see these globally-installed siblings, so a theme
#   that is actually installed still fails with "Could not load theme ...
#   Is it installed?". The global bin's shebang (`#!/usr/bin/env node`) also
#   won't run on its own since this image has no `node` binary, only `bun`
#   -- `bun` must be the explicit interpreter. Any additionally-installed
#   theme (see JobGitOps-9ru) must also be installed with `bun add -g` into
#   this same global tree for the same reason.
# - `resumed export` still needs `--puppeteer-arg=--no-sandbox` even when
#   run as `pptruser`, not because of a missing non-root user (there is
#   one), but because Chromium's own internal sandbox needs to create user/
#   PID/network namespaces, and confirmed by hands-on testing, Docker's
#   default container runtime -- including GitHub Actions' declarative
#   `container:` job syntax used by triage-issue.yml, which has no way to
#   add `--cap-add=SYS_ADMIN` short of a workflow-level `options:` change --
#   refuses that regardless of the calling user, root or not ("Failed to
#   move to new namespace: ... Operation not permitted"). Running as
#   `pptruser` instead of root still meaningfully narrows the blast radius
#   of a Chromium renderer compromise even with `--no-sandbox` (no write
#   access to system files, no ability to tamper with installed packages).
#   As additional defense in depth, whatever code invokes this (JobGitOps-
#   nx0) should strip GITHUB_TOKEN/GH_PAT/LLM API keys from the subprocess
#   environment before running as `pptruser`, since a dropped-privilege
#   child process otherwise still inherits the full parent environment.
RUN bun add -g resumed@7.0.0 puppeteer@25.11.0 @jsonresume/jsonresume-theme-professional@1.0.22 \
    && chmod -R a+rX /opt/bun

# Set the project environment to target system Python
ENV UV_PROJECT_ENVIRONMENT=/usr/local

WORKDIR /workspace

# Copy the project so uv can build and install it. README.md is required:
# pyproject.toml declares it as the package readme, so the build fails without it.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# Sync dependencies AND install the project system-wide, so the jobgitops CLI
# runs from any working directory without PYTHONPATH or a src/ checkout
RUN uv sync --frozen
