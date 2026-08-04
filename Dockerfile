# Use python:3.12-slim-bookworm as the base image for a lightweight runner
FROM python:3.12-slim-bookworm

# Prevent interactive prompts during apt package installation
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies for WeasyPrint, git, just, and fontconfig
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
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

# Expose src directory to PYTHONPATH so python modules are discoverable at runtime
ENV PYTHONPATH=/workspace/src:/github/workspace/src

# Disable Git's safe.directory checks so GHA-mounted checkouts don't trigger ownership errors
RUN git config --global --add safe.directory '*'

WORKDIR /workspace

# Copy dependency files to bake them into the image
COPY pyproject.toml uv.lock ./

# Sync dependencies (including dev groups) system-wide without installing the source project itself
RUN uv sync --frozen --no-install-project
