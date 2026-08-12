# Use python:3.12-slim-bookworm as the base image for a lightweight runner
FROM python:3.12-slim-bookworm

# Prevent interactive prompts during apt package installation
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies for WeasyPrint, git, just, shellcheck, and fontconfig
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
    && rm -rf /var/lib/apt/lists/*

# Install Casey's 'just' command runner
RUN curl --proto '=https' --tlsv1.2 -sSf https://just.systems/install.sh | bash -s -- --to /usr/local/bin

# Install uv for ultra-fast python package installation
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

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
