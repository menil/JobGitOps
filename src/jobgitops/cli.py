"""Shared CLI bootstrap helpers for the job-search pipeline scripts.

Every entry point (``respond.py``, ``triage.py``, ``status_transition.py``,
``project_sync.py``) used to copy the same logging setup, ``--repo-path``
argument, and repository path resolution. Centralizing them keeps the CLI
surface consistent so the scripts cannot drift apart.
"""

import argparse
import logging
import pathlib
import sys


def setup_logging() -> None:
    """Configure INFO-level logging to stdout for a CLI script."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def add_repo_path_argument(parser: argparse.ArgumentParser) -> None:
    """Add the standard ``--repo-path`` argument shared by all scripts."""
    parser.add_argument(
        "--repo-path",
        type=str,
        default=".",
        help="Path to the local git repository (defaults to '.').",
    )


def resolve_repo_path(repo_path: str) -> pathlib.Path:
    """Resolve a ``--repo-path`` value to an absolute path."""
    return pathlib.Path(repo_path).resolve()
