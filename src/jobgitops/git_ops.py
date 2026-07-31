"""Git repository operations wrapper for JobGitOps."""

import hashlib
import pathlib
import re
import subprocess

# Pre-compiled regex for slugify to optimize performance in loops.
SLUG_REGEX = re.compile(r"[^a-z0-9]+")


class GitOpsError(Exception):
    """Raised when a Git operation fails."""

    pass


def run_git(args: list[str], cwd: pathlib.Path) -> str:
    """Execute a Git command and return its stdout.

    Args:
        args: List of command-line arguments to pass to git.
        cwd: Directory context for execution.

    Returns:
        The stripped standard output of the command.

    Raises:
        GitOpsError: If the command returns a non-zero exit code or fails to execute.
    """
    try:
        res = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()
    except subprocess.CalledProcessError as e:
        stderr_msg = e.stderr.strip() if e.stderr else "No stderr message"
        # Mask potentially sensitive remote access tokens from command args
        # in the error log.
        masked_cmd = " ".join(
            "***" if "token" in arg or "@" in arg else arg for arg in args
        )
        raise GitOpsError(
            f"Git command failed: git {masked_cmd}. Error: {stderr_msg}"
        ) from e
    except (FileNotFoundError, OSError, ValueError) as e:
        # Catch system-level exceptions like missing git binary,
        # invalid working directory paths, or encoding issues to
        # encapsulate them cleanly into GitOpsError.
        raise GitOpsError(f"Failed to execute Git command: {e}") from e


def slugify(s: str) -> str:
    """Convert a string into a lower-case, URL-friendly slug.

    Args:
        s: The string to slugify.

    Returns:
        Sanitized slug string.
    """
    return SLUG_REGEX.sub("-", s.lower()).strip("-")


def generate_branch_name(company: str, role: str, url: str) -> str:
    """Generate a sanitized, unique branch name for a job application.

    Args:
        company: The name of the hiring company.
        role: The role title.
        url: The job posting URL.

    Returns:
        A branch name prefixed with 'applications/'.
    """
    # 5 characters from SHA-256 strikes a balance between low collision
    # probability and keeping the branch name concise.
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:5]

    company_slug = slugify(company)
    role_slug = slugify(role)

    # Use a default fallback prefix if both company and role are empty/non-alphanumeric
    # to avoid creating a branch named just "applications/<hash>".
    parts = []
    if company_slug:
        parts.append(company_slug)
    if role_slug:
        parts.append(role_slug)
    if not parts:
        parts.append("job")
    parts.append(url_hash)

    return f"applications/{'-'.join(parts)}"


def allocate_lengths(len_a: int, len_b: int, total_limit: int) -> tuple[int, int]:
    """Allocate character limits dynamically for two lengths within a total limit.

    Ensures that if one string is short, the other can occupy the remaining space,
    otherwise splits the remaining space evenly.
    """
    half_limit = total_limit // 2
    if len_a <= half_limit:
        return len_a, total_limit - len_a
    if len_b <= half_limit:
        return total_limit - len_b, len_b
    return half_limit, total_limit - half_limit


def build_commit_message(company: str, role: str) -> str:
    """Build a Conventional Commit message that is strictly under 72 characters.

    The commit message must satisfy the commit-msg git hooks. To avoid starving
    the role title or company name when one is very long, available character
    space is allocated dynamically.

    Args:
        company: The company name.
        role: The role title.

    Returns:
        A conventional commit message <= 71 characters.
    """
    company_clean = (company or "").strip()
    role_clean = (role or "").strip()

    prefix = "feat(application): tailor resume for "
    # Max length of company/role/separator is 71 - 37 (prefix) = 34.
    max_len = 71 - len(prefix)

    if not company_clean and not role_clean:
        return f"{prefix}unknown position"

    combined = (
        f"{company_clean} - {role_clean}"
        if company_clean and role_clean
        else (company_clean or role_clean)
    )
    if len(combined) <= max_len:
        return prefix + combined

    # Allocate space dynamically (deducting 3 characters for the " - " separator)
    available = max_len - 3
    limit_c, limit_r = allocate_lengths(len(company_clean), len(role_clean), available)

    def truncate(s: str, limit: int) -> str:
        if len(s) <= limit:
            return s
        if limit <= 2:
            return s[:limit]
        return s[: limit - 2] + ".."

    company_trunc = truncate(company_clean, limit_c)
    role_trunc = truncate(role_clean, limit_r)

    return f"{prefix}{company_trunc} - {role_trunc}"


def create_or_checkout_branch(
    repo_path: pathlib.Path, branch_name: str, base_branch: str = "main"
) -> None:
    """Check out an existing branch or create it from the base branch.

    Args:
        repo_path: Path to the git repository.
        branch_name: The target branch to checkout/create.
        base_branch: The base branch to branch off of if creating a new one.

    Raises:
        GitOpsError: If git execution fails.
    """
    # Verify local heads to check branch existence without raising exceptions.
    res = subprocess.run(
        ["git", "show-ref", "--verify", f"refs/heads/{branch_name}"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if res.returncode == 0:
        run_git(["checkout", branch_name], cwd=repo_path)
    else:
        # Check if base branch exists locally or on remote to avoid checkout crashes.
        base_res = subprocess.run(
            ["git", "show-ref", "--verify", f"refs/heads/{base_branch}"],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        if base_res.returncode == 0:
            run_git(["checkout", "-b", branch_name, base_branch, "--"], cwd=repo_path)
        else:
            # Fallback to checkout from current HEAD if base_branch is not resolved.
            run_git(["checkout", "-b", branch_name, "--"], cwd=repo_path)


def commit_changes(
    repo_path: pathlib.Path,
    files: list[str | pathlib.Path],
    company: str,
    role: str,
) -> None:
    """Stage files and commit them with a Conventional Commit message.

    Args:
        repo_path: Path to the git repository.
        files: List of file paths to stage and commit.
        company: The company name for the commit message.
        role: The role title for the commit message.

    Raises:
        GitOpsError: If git execution fails.
    """
    if not files:
        return

    # Batch file staging into a single call. We use --force to override any
    # .gitignore rules for these generated/tailored files.
    run_git(["add", "--force", "--"] + [str(file) for file in files], cwd=repo_path)

    # Perform a dry-run check to prevent empty commits which raise git errors.
    diff_res = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=repo_path,
    )
    if diff_res.returncode == 0:
        # No staged changes exist to commit, return early gracefully.
        return

    msg = build_commit_message(company, role)
    run_git(["commit", "-m", msg], cwd=repo_path)


def push_branch(
    repo_path: pathlib.Path, branch_name: str, remote: str = "origin"
) -> None:
    """Push the branch to remote and set upstream tracking.

    Args:
        repo_path: Path to the git repository.
        branch_name: The branch name to push.
        remote: The git remote to push to.

    Raises:
        GitOpsError: If git execution fails.
    """
    run_git(["push", "--set-upstream", remote, branch_name], cwd=repo_path)
