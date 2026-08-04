"""Unit tests for the shared CLI bootstrap helpers."""

import argparse

from jobgitops.cli import add_repo_path_argument, resolve_repo_path, setup_logging


def test_add_repo_path_argument_defaults() -> None:
    parser = argparse.ArgumentParser()
    add_repo_path_argument(parser)
    args = parser.parse_args([])
    assert args.repo_path == "."

    args = parser.parse_args(["--repo-path", "somewhere"])
    assert args.repo_path == "somewhere"


def test_resolve_repo_path_returns_absolute(tmp_path) -> None:
    resolved = resolve_repo_path(str(tmp_path))
    assert resolved.is_absolute()
    assert resolved == tmp_path.resolve()


def test_setup_logging_runs() -> None:
    setup_logging()
