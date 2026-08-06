"""Shared CLI bootstrap helpers and entry points for the job-search pipeline.

Every entry point (respond, triage, status_transition, project_sync)
imports these logging setup and repository path resolution helpers.
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
