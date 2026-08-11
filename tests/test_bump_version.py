"""Unit tests for the scripts/bump-version.sh release automation script."""

import pathlib
import subprocess

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "bump-version.sh"


def _git(repo: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.com",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def _commit(repo: pathlib.Path, message: str) -> None:
    with (repo / "file.txt").open("a") as file:
        file.write(f"change {message}\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture()
def repo(tmp_path: pathlib.Path) -> pathlib.Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    return tmp_path


def run_script(repo: pathlib.Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", str(SCRIPT)],
        cwd=repo,
        capture_output=True,
        text=True,
    )


def test_feat_bumps_minor_from_existing_tag(repo: pathlib.Path) -> None:
    _commit(repo, "feat: initial")
    _git(repo, "tag", "v1.2.3")
    _commit(repo, "feat: add widget")

    result = run_script(repo)

    assert result.returncode == 0
    assert result.stdout.strip() == "v1.3.0"


def test_fix_bumps_patch(repo: pathlib.Path) -> None:
    _commit(repo, "feat: initial")
    _git(repo, "tag", "v1.2.3")
    _commit(repo, "fix: correct typo")

    result = run_script(repo)

    assert result.stdout.strip() == "v1.2.4"


def test_breaking_change_bumps_major_post_1_0(repo: pathlib.Path) -> None:
    _commit(repo, "feat: initial")
    _git(repo, "tag", "v2.0.0")
    _commit(repo, "feat!: break the api")

    result = run_script(repo)

    assert result.stdout.strip() == "v3.0.0"


def test_breaking_change_in_body_bumps_major(repo: pathlib.Path) -> None:
    _commit(repo, "feat: initial")
    _git(repo, "tag", "v2.0.0")
    _commit(repo, "feat: add thing\n\nBREAKING CHANGE: remove the old thing")

    result = run_script(repo)

    assert result.stdout.strip() == "v3.0.0"


def test_breaking_change_pre_1_0_bumps_minor(repo: pathlib.Path) -> None:
    _commit(repo, "feat: initial")
    _git(repo, "tag", "v0.5.0")
    _commit(repo, "feat!: restructure")

    result = run_script(repo)

    assert result.stdout.strip() == "v0.6.0"


def test_scope_bang_breaking_bumps_major(repo: pathlib.Path) -> None:
    _commit(repo, "feat: initial")
    _git(repo, "tag", "v1.0.0")
    _commit(repo, "fix(scope)!: remove deprecated flag")

    result = run_script(repo)

    assert result.stdout.strip() == "v2.0.0"


def test_no_commits_since_last_tag_emits_nothing(repo: pathlib.Path) -> None:
    _commit(repo, "feat: initial")
    _git(repo, "tag", "v1.0.0")

    result = run_script(repo)

    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_invalid_and_non_releasing_commits_emit_nothing(repo: pathlib.Path) -> None:
    _commit(repo, "feat: initial")
    _git(repo, "tag", "v1.0.0")
    _commit(repo, "docs: update readme")
    _commit(repo, "WIP: not a conventional commit")
    _commit(repo, "update stuff without a prefix")

    result = run_script(repo)

    assert result.stdout.strip() == ""


def test_no_tags_uses_initial_commit_range(repo: pathlib.Path) -> None:
    _commit(repo, "feat: first")
    _commit(repo, "feat: second")

    result = run_script(repo)

    assert result.stdout.strip() == "v0.1.0"


def test_semver_sort_picks_highest_tag(repo: pathlib.Path) -> None:
    _commit(repo, "feat: a")
    _git(repo, "tag", "v1.9.0")
    _commit(repo, "feat: b")
    _git(repo, "tag", "v1.10.0")
    _commit(repo, "feat: c")

    result = run_script(repo)

    assert result.stdout.strip() == "v1.11.0"
