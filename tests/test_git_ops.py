"""Unit tests for Git repository operations."""

import hashlib
import pathlib
import subprocess
from unittest import mock

import pytest

from jobgitops.git_ops import (
    GitOpsError,
    allocate_lengths,
    build_commit_message,
    commit_changes,
    create_or_checkout_branch,
    generate_branch_name,
    push_branch,
    run_git,
    slugify,
)


def test_slugify() -> None:
    """Test string slugification logic."""
    assert slugify("Google") == "google"
    assert slugify("Apple Inc.") == "apple-inc"
    assert slugify("🚀 Space Company") == "space-company"
    assert slugify("Company & Co.") == "company-co"
    assert slugify("Company - ") == "company"
    assert slugify("---") == ""


def test_allocate_lengths() -> None:
    """Test dynamic limit allocation helper."""
    # Case 1: First is short, second gets remaining
    assert allocate_lengths(5, 40, 30) == (5, 25)
    # Case 2: Second is short, first gets remaining
    assert allocate_lengths(40, 5, 30) == (25, 5)
    # Case 3: Both are long, split evenly
    assert allocate_lengths(40, 40, 31) == (15, 16)


def test_generate_branch_name() -> None:
    """Test application branch name generation and hashing."""
    url = "https://careers.google.com/jobs/12345"
    expected_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:5]

    # Normal case
    branch = generate_branch_name("Google", "Staff Engineer", url)
    assert branch == f"applications/google-staff-engineer-{expected_hash}"

    # Missing/empty company or role
    branch_no_company = generate_branch_name("", "Engineer", url)
    assert branch_no_company == f"applications/engineer-{expected_hash}"

    branch_no_role = generate_branch_name("Google", "", url)
    assert branch_no_role == f"applications/google-{expected_hash}"

    # Fallback default prefix when both company and role are empty
    branch_empty = generate_branch_name("", "", url)
    assert branch_empty == f"applications/job-{expected_hash}"


def test_build_commit_message_lengths() -> None:
    """Test build_commit_message length constraints and truncation."""
    # Under limit (71 characters)
    msg = build_commit_message("Google", "Engineer")
    assert msg == "feat(application): tailor resume for Google - Engineer"
    assert len(msg) <= 71

    # Stripping check
    msg_strip = build_commit_message("  Google  ", "\n Engineer \t")
    assert msg_strip == "feat(application): tailor resume for Google - Engineer"

    # Empty company and role
    msg_empty = build_commit_message("", "")
    assert msg_empty == "feat(application): tailor resume for unknown position"

    # Right on the edge
    # Prefix = 37. Max remaining = 34.
    # "A" * 16 + " - " + "B" * 15 = 34. Total = 71.
    msg_edge = build_commit_message("A" * 16, "B" * 15)
    assert msg_edge == f"feat(application): tailor resume for {'A' * 16} - {'B' * 15}"
    assert len(msg_edge) == 71

    # Over limit: Company very long, Role short
    # Total available for company + role + " - " is 34.
    # Role is 5. So company gets up to 31 - 5 = 26.
    # Since company is 40, it gets truncated.
    # limit_c = 26. Truncate 40 to 26: company[:24] + ".."
    msg_long_c = build_commit_message("A" * 40, "B" * 5)
    expected_c = "A" * 24 + ".."
    assert (
        msg_long_c == f"feat(application): tailor resume for {expected_c} - {'B' * 5}"
    )
    assert len(msg_long_c) == 71

    # Over limit: Company short, Role very long
    # Company is 5. Role is 40. Role gets truncated.
    # limit_r = 26. Truncate 40 to 26: role[:24] + ".."
    msg_long_r = build_commit_message("A" * 5, "B" * 40)
    expected_r = "B" * 24 + ".."
    assert (
        msg_long_r == f"feat(application): tailor resume for {'A' * 5} - {expected_r}"
    )
    assert len(msg_long_r) == 71

    # Over limit: Both very long
    # limit_c = 15. Truncate to 15: company[:13] + ".."
    # limit_r = 16. Truncate to 16: role[:14] + ".."
    msg_both_long = build_commit_message("A" * 40, "B" * 40)
    expected_c_both = "A" * 13 + ".."
    expected_r_both = "B" * 14 + ".."
    assert (
        msg_both_long
        == f"feat(application): tailor resume for {expected_c_both} - {expected_r_both}"
    )
    assert len(msg_both_long) == 71

    # Small limits check
    # Check that when limit <= 2, it truncates without ".."
    # We can fake it by calling build_commit_message with strings
    # that push limits to <= 2.
    # E.g., if total available is 32. If company is "A"*31 (len 31),
    # then role only has 1 char left.
    # If company is "A"*35, role is "B"*35, limit_c = 14, limit_r = 15.
    # Let's test build_commit_message with extremely long inputs to ensure safety.
    msg_extreme = build_commit_message("A" * 1000, "B" * 1000)
    assert len(msg_extreme) == 71


@mock.patch("subprocess.run")
def test_run_git_success(mock_run: mock.MagicMock) -> None:
    """Test successful run_git execution."""
    mock_res = mock.MagicMock()
    mock_res.returncode = 0
    mock_res.stdout = "  my-output \n"
    mock_run.return_value = mock_res

    res = run_git(["status"], pathlib.Path("/tmp"))
    assert res == "my-output"
    mock_run.assert_called_once_with(
        ["git", "status"],
        cwd=pathlib.Path("/tmp"),
        capture_output=True,
        text=True,
        check=True,
    )


@mock.patch("subprocess.run")
def test_run_git_failure_masking(mock_run: mock.MagicMock) -> None:
    """Test run_git handling of subprocess failures with token masking."""
    mock_run.side_effect = subprocess.CalledProcessError(
        returncode=128,
        cmd=[
            "git",
            "clone",
            "https://x-access-token:secret_token@github.com/owner/repo.git",
        ],
        stderr="fatal: Authentication failed",
    )

    with pytest.raises(GitOpsError) as exc_info:
        run_git(
            ["clone", "https://x-access-token:secret_token@github.com/owner/repo.git"],
            pathlib.Path("/tmp"),
        )

    assert "Git command failed" in str(exc_info.value)
    assert "fatal: Authentication failed" in str(exc_info.value)
    # Ensure raw secret URL token is masked
    assert "secret_token" not in str(exc_info.value)
    assert "***" in str(exc_info.value)


@mock.patch("subprocess.run")
def test_run_git_file_not_found(mock_run: mock.MagicMock) -> None:
    """Test run_git wrapping FileNotFoundError."""
    mock_run.side_effect = FileNotFoundError(
        "[Errno 2] No such file or directory: 'git'"
    )

    with pytest.raises(GitOpsError) as exc_info:
        run_git(["status"], pathlib.Path("/tmp"))

    assert "Failed to execute Git command" in str(exc_info.value)


@mock.patch("subprocess.run")
def test_create_or_checkout_branch_exists(mock_run: mock.MagicMock) -> None:
    """Test create_or_checkout_branch when branch already exists locally."""
    # First call: show-ref --verify refs/heads/my-branch (returns 0)
    # Second call: checkout my-branch (inside run_git, returns 0)
    mock_verify = mock.MagicMock(returncode=0)
    mock_checkout = mock.MagicMock(returncode=0, stdout="")
    mock_run.side_effect = [mock_verify, mock_checkout]

    repo = pathlib.Path("/repo")
    create_or_checkout_branch(repo, "my-branch")

    # Verify that it checked out the existing branch
    mock_run.assert_has_calls(
        [
            mock.call(
                ["git", "show-ref", "--verify", "refs/heads/my-branch"],
                cwd=repo,
                capture_output=True,
                text=True,
            ),
            mock.call(
                ["git", "checkout", "my-branch"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            ),
        ]
    )


@mock.patch("subprocess.run")
def test_create_or_checkout_branch_new_with_base(mock_run: mock.MagicMock) -> None:
    """Test create_or_checkout_branch when branch is new and base branch exists."""
    # 1. show-ref --verify refs/heads/my-branch (returns 1)
    # 2. show-ref --verify refs/heads/main (returns 0)
    # 3. checkout -b my-branch -- main (inside run_git, returns 0)
    mock_verify_branch = mock.MagicMock(returncode=1)
    mock_verify_base = mock.MagicMock(returncode=0)
    mock_checkout = mock.MagicMock(returncode=0, stdout="")
    mock_run.side_effect = [mock_verify_branch, mock_verify_base, mock_checkout]

    repo = pathlib.Path("/repo")
    create_or_checkout_branch(repo, "my-branch", "main")

    mock_run.assert_has_calls(
        [
            mock.call(
                ["git", "show-ref", "--verify", "refs/heads/my-branch"],
                cwd=repo,
                capture_output=True,
                text=True,
            ),
            mock.call(
                ["git", "show-ref", "--verify", "refs/heads/main"],
                cwd=repo,
                capture_output=True,
                text=True,
            ),
            mock.call(
                ["git", "checkout", "-b", "my-branch", "main", "--"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            ),
        ]
    )


@mock.patch("subprocess.run")
def test_create_or_checkout_branch_new_no_base(mock_run: mock.MagicMock) -> None:
    """Test create_or_checkout_branch when branch and base branch do not exist."""
    # 1. show-ref --verify refs/heads/my-branch (returns 1)
    # 2. show-ref --verify refs/heads/main (returns 1)
    # 3. checkout -b my-branch (inside run_git, returns 0)
    mock_verify_branch = mock.MagicMock(returncode=1)
    mock_verify_base = mock.MagicMock(returncode=1)
    mock_checkout = mock.MagicMock(returncode=0, stdout="")
    mock_run.side_effect = [mock_verify_branch, mock_verify_base, mock_checkout]

    repo = pathlib.Path("/repo")
    create_or_checkout_branch(repo, "my-branch", "main")

    mock_run.assert_has_calls(
        [
            mock.call(
                ["git", "show-ref", "--verify", "refs/heads/my-branch"],
                cwd=repo,
                capture_output=True,
                text=True,
            ),
            mock.call(
                ["git", "show-ref", "--verify", "refs/heads/main"],
                cwd=repo,
                capture_output=True,
                text=True,
            ),
            mock.call(
                ["git", "checkout", "-b", "my-branch", "--"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            ),
        ]
    )


@mock.patch("subprocess.run")
def test_commit_changes_success(mock_run: mock.MagicMock) -> None:
    """Test successful commit workflow with changes staged."""
    # 1. git add -- file1 file2 (returns 0)
    # 2. git diff --cached --quiet (staged changes exist; returns 1)
    # 3. git commit -m msg (returns 0)
    mock_add = mock.MagicMock(returncode=0, stdout="")
    mock_diff = mock.MagicMock(returncode=1)  # 1 indicates differences exist
    mock_commit = mock.MagicMock(returncode=0, stdout="")
    mock_run.side_effect = [mock_add, mock_diff, mock_commit]

    repo = pathlib.Path("/repo")
    commit_changes(repo, ["file1", "file2"], "Google", "Engineer")

    mock_run.assert_has_calls(
        [
            mock.call(
                ["git", "add", "--force", "--", "file1", "file2"],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            ),
            mock.call(
                ["git", "diff", "--cached", "--quiet"],
                cwd=repo,
            ),
            mock.call(
                [
                    "git",
                    "commit",
                    "-m",
                    "feat(application): tailor resume for Google - Engineer",
                ],
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            ),
        ]
    )


@mock.patch("subprocess.run")
def test_commit_changes_no_files(mock_run: mock.MagicMock) -> None:
    """Test commit_changes returns early if files list is empty."""
    repo = pathlib.Path("/repo")
    commit_changes(repo, [], "Google", "Engineer")
    mock_run.assert_not_called()


@mock.patch("subprocess.run")
def test_commit_changes_no_staged_changes(mock_run: mock.MagicMock) -> None:
    """Test commit_changes returns early if no staged changes exist (diff returns 0)."""
    # 1. git add -- file1 file2 (returns 0)
    # 2. git diff --cached --quiet (no changes; returns 0)
    mock_add = mock.MagicMock(returncode=0, stdout="")
    mock_diff = mock.MagicMock(returncode=0)
    mock_run.side_effect = [mock_add, mock_diff]

    repo = pathlib.Path("/repo")
    commit_changes(repo, ["file1"], "Google", "Engineer")

    # Verify commit is not invoked
    assert mock_run.call_count == 2
    last_call = mock_run.call_args_list[-1]
    assert last_call[0][0] == ["git", "diff", "--cached", "--quiet"]


@mock.patch("subprocess.run")
def test_push_branch(mock_run: mock.MagicMock) -> None:
    """Test pushing branch to remote."""
    mock_run.return_value = mock.MagicMock(returncode=0, stdout="")

    repo = pathlib.Path("/repo")
    push_branch(repo, "my-branch", "origin")

    mock_run.assert_called_once_with(
        ["git", "push", "--set-upstream", "origin", "my-branch"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
