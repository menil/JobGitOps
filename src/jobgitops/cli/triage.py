"""Job triage and tailoring coordinator for JobGitOps.

Parses issue details, triages the job description against the base resume,
and either rejects/closes the issue or tailors the resume on a Git branch.
"""

import argparse
import hashlib
import json
import logging
import os
import pathlib
import re
import sys
from typing import Any

from jobgitops.cli import add_repo_path_argument, resolve_repo_path, setup_logging
from jobgitops.fit_grades import FIT_GRADE_A_MIN, FIT_GRADE_A_PLUS_MIN, FIT_GRADE_B_MIN
from jobgitops.git_ops import (
    GitOpsError,
    commit_changes,
    create_or_checkout_branch,
    generate_branch_name,
    push_branch,
    run_git,
)
from jobgitops.github_client import GitHubClient, extract_label_names
from jobgitops.llm import LLMClient, QuotaExceededError, TriageResult, get_llm_client
from jobgitops.loader import load_resume, load_settings, render_resume_yaml
from jobgitops.renderer import compile_resume
from jobgitops.status_model import FIT_CATEGORY_MISMATCH_LABELS, LABEL_TO_STATUS
from jobgitops.web import WebClient

logger = logging.getLogger("jobgitops.triage")

EXIT_SUCCESS = 0
EXIT_ERROR = 1

# POSIX exit code for temporary quota/rate-limit failure (EX_TEMPFAIL)
EXIT_QUOTA_EXCEEDED = 75

type ExitCode = EXIT_SUCCESS | EXIT_ERROR | EXIT_QUOTA_EXCEEDED

# Maximum number of pending issues retrieved in each batch API request.
BATCH_PAGE_SIZE = 100

# Sub-scores below this threshold are flagged with a category mismatch label.
CATEGORY_MISMATCH_THRESHOLD = 3.0

# Defaults applied when a job detail cannot be inferred.
DEFAULT_COMPANY = "Unknown Company"
DEFAULT_ROLE = "Unknown Role"
DEFAULT_LOCATION = "Remote"
DEFAULT_SALARY = "Not specified"

# Cap for the resume diff embedded inline in the approval comment; GitHub
# comments are limited to 65,536 characters, leaving room for the rest
# of the triage summary.
INLINE_DIFF_MAX_CHARS = 20000

# Path of the tailored resume whose changes are shown inline.
RESUME_YAML_PATH = "resumes/resume.yaml"


# Pre-compiled regex patterns for robust job detail parsing
COMPANY_REGEX = re.compile(r"\*\*[Cc]ompany:?\*\*:?\s*(.*)")
ROLE_REGEX = re.compile(r"\*\*[Rr]ole:?\*\*:?\s*(.*)")
LOCATION_REGEX = re.compile(r"\*\*[Ll]ocation:?\*\*:?\s*(.*)")
SALARY_REGEX = re.compile(r"\*\*[Ss]alary:?\*\*:?\s*(.*)")
SOURCE_REGEX = re.compile(r"\*\*[Ss]ource:?\*\*:?\s*(.*)")
APPLY_URL_REGEX = re.compile(r"\*\*[Aa]pply\s*[Uu][Rr][Ll]:?\*\*:?\s*(.*)")
MARKDOWN_LINK_REGEX = re.compile(r"^\[.*?\]\((.*?)\)$")
TITLE_COMPANY_ROLE_REGEX = re.compile(r"^\[(.*?)\]\s*(.*)$")
JOB_DESCRIPTION_HEADING_REGEX = re.compile(r"##\s*Job\s*Description", re.IGNORECASE)
BARE_URL_REGEX = re.compile(r"https?://[^\s]+")

# Canonical layout for URL-sourced job issues (spec 5.5). The single source of
# truth for how a fetched posting is rendered into a body that the existing
# parse_job_details / run_triage core can consume unchanged.
CANONICAL_BODY_TEMPLATE = (
    "**Company:** {company}\n"
    "**Role:** {role}\n"
    "**Location:** {location}\n"
    "**Salary:** {salary}\n"
    "**Source:** manual\n"
    "**Apply URL:** {url}\n"
    "\n## Job Description\n"
    "{description}"
)


class JobFetchError(Exception):
    """Raised when a job posting URL cannot be fetched or yields no content."""


def _clean_str(value: str | None) -> str:
    """Coerce a value to a stripped string; empty string when None."""
    return (value or "").strip()


def _sanitize_apply_url(apply_url: str | None) -> str:
    """Validate and sanitize an apply URL against markdown injection.

    Unsafe URLs (missing http(s) scheme or containing characters that could
    break out of markdown, including parentheses that smuggle link-injection
    payloads like ``url) [Click](evil``) are dropped entirely and logged.

    Args:
        apply_url: The raw apply URL to validate.

    Returns:
        The sanitized URL, or an empty string when unsafe or empty.
    """
    apply_url = _clean_str(apply_url)
    if apply_url and (
        not (apply_url.startswith("http://") or apply_url.startswith("https://"))
        or any(char in apply_url for char in " ()[]\"'<>")
    ):
        logger.warning("apply_url is invalid or unsafe: %s", apply_url)
        return ""
    return apply_url


def _has_job_description_section(body: str | None) -> bool:
    """Return True when the body contains a '## Job Description' section."""
    return bool(body and JOB_DESCRIPTION_HEADING_REGEX.search(body))


def parse_job_details(body: str | None, title: str | None = "") -> dict[str, str]:
    """Parse job details from the issue body or title.

    Args:
        body: The markdown body of the issue.
        title: The title of the issue.

    Returns:
        Dictionary of parsed job details (company, role, location, salary,
        source, apply_url, description).
    """
    body = body or ""
    title = title or ""

    company = ""
    role = ""
    location = DEFAULT_LOCATION
    salary = DEFAULT_SALARY
    source = ""
    apply_url = ""
    description = ""

    # Parse details from body using regex
    company_match = COMPANY_REGEX.search(body)
    if company_match:
        company = company_match.group(1).strip()

    role_match = ROLE_REGEX.search(body)
    if role_match:
        role = role_match.group(1).strip()

    location_match = LOCATION_REGEX.search(body)
    if location_match:
        location = location_match.group(1).strip()

    salary_match = SALARY_REGEX.search(body)
    if salary_match:
        salary = salary_match.group(1).strip()

    source_match = SOURCE_REGEX.search(body)
    if source_match:
        source = source_match.group(1).strip()

    apply_url_match = APPLY_URL_REGEX.search(body)
    if apply_url_match:
        apply_url = apply_url_match.group(1).strip()
        # Clean markdown link format [Text](url) or just url
        link_match = MARKDOWN_LINK_REGEX.match(apply_url)
        if link_match:
            apply_url = link_match.group(1).strip()

    # Extract Job Description, split at most once
    desc_split = re.split(
        r"##\s*Job\s*Description", body, maxsplit=1, flags=re.IGNORECASE
    )
    description = desc_split[1].strip() if len(desc_split) > 1 else body.strip()

    # Fallbacks from issue title if company or role are missing
    if not company or not role:
        title_match = TITLE_COMPANY_ROLE_REGEX.match(title)
        if title_match:
            if not company:
                company = title_match.group(1).strip()
            if not role:
                role = title_match.group(2).strip()
        elif " at " in title:
            parts = title.split(" at ", 1)
            if not role:
                role = parts[0].strip()
            if not company:
                company = parts[1].strip()
        else:
            if not role:
                role = title.strip()

    if not company:
        company = DEFAULT_COMPANY
    if not role:
        role = DEFAULT_ROLE

    # Validate/Sanitize Apply URL to prevent markdown injection
    apply_url = _sanitize_apply_url(apply_url)

    # URL-aware parsing (spec 5.5): only when the body carries no explicit
    # Apply URL field and no job-description section is the body treated as a
    # bare URL submission. This deliberately runs only on bodies without an
    # `**Apply URL:**` marker so a rejected injection payload is never
    # re-admitted by scanning for an arbitrary http(s) string.
    if not apply_url_match and not _has_job_description_section(body):
        url_match = BARE_URL_REGEX.search(body)
        if url_match:
            apply_url = _sanitize_apply_url(url_match.group(0).rstrip(".,;"))

    return {
        "company": company,
        "role": role,
        "location": location,
        "salary": salary,
        "source": source,
        "apply_url": apply_url,
        "description": description,
    }


def build_canonical_body(
    company: str,
    role: str,
    location: str,
    salary: str,
    url: str,
    description: str,
) -> str:
    """Build the canonical job issue body for a URL-sourced posting.

    Renders the shared ``CANONICAL_BODY_TEMPLATE``: salary defaults to
    'Not specified', ``url`` passes through the apply-URL markdown-injection
    sanitization, and ``description`` is always the fetched text.

    Args:
        company: Inferred hiring company name.
        role: Inferred job title/role.
        location: Work location (defaults to 'Remote' when empty).
        salary: Stated salary ('Not specified' when empty).
        url: Apply URL (sanitized; unsafe URLs become empty).
        description: Fetched job posting text.

    Returns:
        The formatted canonical body string.
    """
    values = {
        "company": _clean_str(company) or DEFAULT_COMPANY,
        "role": _clean_str(role) or DEFAULT_ROLE,
        "location": _clean_str(location) or DEFAULT_LOCATION,
        "salary": _clean_str(salary) or DEFAULT_SALARY,
        "url": _sanitize_apply_url(url),
        "description": description or "",
    }
    # Substitute placeholders manually instead of ``str.format`` so literal
    # braces in fetched text (code snippets, JSON) never raise a format error.
    # ``description`` is substituted last so a placeholder-looking substring
    # inside the fetched text is preserved verbatim.
    body = CANONICAL_BODY_TEMPLATE
    for key, value in values.items():
        body = body.replace("{" + key + "}", value)
    return body


def fetch_job_page(url: str, web_client: Any) -> dict[str, str]:
    """Fetch a job page once, returning ``{title, description}``.

    ``description`` is always the extracted page text (never model-synthesized)
    and is trimmed to the research ``max_content_bytes`` when available.

    Args:
        url: The job posting URL to fetch.
        web_client: The WebClient (or test double) exposing ``fetch_url``.

    Returns:
        A dict with the page title and the trimmed extracted text.

    Raises:
        JobFetchError: When the fetch fails or yields no readable content.
    """
    result = web_client.fetch_url(url)
    if isinstance(result, dict):
        raise JobFetchError(_clean_str(result.get("error")) or "URL fetch failed")
    text = _clean_str(result.text)
    if not text:
        raise JobFetchError("No readable content extracted from the URL")
    max_bytes = getattr(
        getattr(web_client, "research", None), "max_content_bytes", None
    )
    if isinstance(max_bytes, int) and max_bytes > 0:
        text = text[:max_bytes]
    return {"title": _clean_str(result.title), "description": text}


def extract_job_from_url(url: str, web_client: Any) -> str:
    """Fetch a job posting URL and return its extracted description text.

    Shared by the triage webhook path and the responder's auto-detect path
    (spec 5.5).

    Args:
        url: The job posting URL to fetch.
        web_client: The WebClient (or test double) exposing ``fetch_url``.

    Returns:
        The extracted readable text of the posting.

    Raises:
        JobFetchError: When the URL cannot be fetched or yields no content.
    """
    return fetch_job_page(url, web_client)["description"]


def _parse_page_title(title: str) -> tuple[str, str]:
    """Best-effort ``(company, role)`` parse from a page title.

    Handles the common "Software Engineer at Acme" and "Acme — Careers"
    layouts before giving up on the raw title.

    Args:
        title: The page ``<title>`` text.

    Returns:
        A ``(company, role)`` tuple; either side may be empty.
    """
    title = _clean_str(title)
    if not title:
        return "", ""
    if " at " in title:
        role, _, company = title.partition(" at ")
        return company.strip(), role.strip()
    for separator in ("|", "—", "\u2013", "-", ":"):
        if separator in title:
            company, _, role = title.partition(separator)
            return company.strip(), role.strip()
    return "", title


def infer_job_details_from_page(
    page_text: str,
    page_title: str,
    url: str,
    llm_client: LLMClient,
) -> dict[str, str]:
    """Infer company/role/location/salary from a fetched job page.

    Runs a single LLM extraction call; when it fails or yields empty
    company/role, falls back to a best-effort page-title parse. The returned
    ``description`` is always ``page_text``, never model-synthesized.

    Args:
        page_text: The fetched posting text.
        page_title: The fetched page ``<title>``.
        url: The posting URL (an input to the extraction prompt).
        llm_client: The LLM client exposing ``extract_job_details``.

    Returns:
        A job-details dict with company, role, location, salary, and
        description keys.

    Raises:
        QuotaExceededError: When the LLM provider rate limit is exceeded.
    """
    extracted: dict[str, str] = {}
    try:
        extracted = llm_client.extract_job_details(page_text, page_title, url) or {}
    except QuotaExceededError:
        raise
    except Exception as e:
        logger.warning(
            "Job details extraction failed (%s); falling back to title parse", e
        )
        extracted = {}

    company = _clean_str(extracted.get("company"))
    role = _clean_str(extracted.get("role"))
    if not company or not role:
        title_company, title_role = _parse_page_title(page_title)
        if not company:
            company = title_company
        if not role:
            role = title_role

    return {
        "company": company or DEFAULT_COMPANY,
        "role": role or DEFAULT_ROLE,
        "location": _clean_str(extracted.get("location")) or DEFAULT_LOCATION,
        "salary": _clean_str(extracted.get("salary")) or DEFAULT_SALARY,
        "source": "manual",
        "description": page_text,
    }


def post_fetch_failure_comment(
    issue_number: int, gh_client: GitHubClient, url: str, error: str
) -> None:
    """Post a clear explanation when a job URL cannot be fetched.

    The issue is intentionally left open and unlabeled so a human can fix the
    URL or paste the description; triage never auto-closes on a fetch failure.
    """
    comment_body = (
        "### AI Triage: Could Not Fetch Job Posting\n\n"
        f"I couldn't fetch the job posting at `{url}` to evaluate it:\n"
        f"```\n{error}\n```\n"
        "The issue is left open and unlabeled. Please double-check the apply "
        "URL or paste the job description directly, then re-label with "
        "`triage-pending`."
    )
    gh_client.post_comment(issue_number, comment_body)


def get_category_mismatch_labels(triage_res: TriageResult) -> list[str]:
    """Return labels for fit dimensions scored below the mismatch threshold.

    Each dimension of the triage result whose score is below
    ``CATEGORY_MISMATCH_THRESHOLD`` maps to a corresponding reason label, so a
    rejected issue can carry multiple reason labels at once.

    Args:
        triage_res: The evaluated triage result.

    Returns:
        Sorted list of mismatch reason label names.
    """
    return sorted(
        label
        for attr, label in FIT_CATEGORY_MISMATCH_LABELS.items()
        if getattr(triage_res, attr) < CATEGORY_MISMATCH_THRESHOLD
    )


def _handle_mismatch(
    issue_number: int,
    issue_node_id: str | None,
    issue_labels: list[str],
    triage_res: TriageResult,
    fit_threshold: float,
    gh_client: GitHubClient,
) -> None:
    """Handle the mismatch workflow path (fit score below threshold)."""
    logger.info("Fit score below threshold. Rejecting job.")

    mismatch_labels = get_category_mismatch_labels(triage_res)

    # Post comment with mismatch reason
    labels_added = ", ".join(f"`{label}`" for label in mismatch_labels)
    comment_body = (
        f"### AI Triage: Mismatch Detected "
        f"(Fit Score: {triage_res.fit_score:.1f}/{fit_threshold:.1f})\n\n"
        f"**Reasoning:**\n{triage_res.reasoning}\n\n"
        f"#### Score Breakdown:\n"
        f"- **Tech Stack Match:** {triage_res.tech_stack_fit:.1f}/5.0\n"
        f"- **Experience & Years Fit:** {triage_res.experience_fit:.1f}/5.0\n"
        f"- **Location & Timezone Suitability:** "
        f"{triage_res.location_fit:.1f}/5.0\n"
        f"- **Salary Alignment:** {triage_res.salary_fit:.1f}/5.0\n"
        f"- **Industry Domain Familiarity:** {triage_res.industry_fit:.1f}/5.0\n"
    )
    if labels_added:
        comment_body += f"\n**Labels Added:** {labels_added}\n"
    gh_client.post_comment(issue_number, comment_body)

    # Update labels
    if "triage-pending" in issue_labels:
        gh_client.remove_label(issue_number, "triage-pending")
    # Prefer specific mismatch reason labels over the generic one
    # to reduce issue label clutter.
    labels_to_add = mismatch_labels if mismatch_labels else ["triage-mismatched"]
    gh_client.add_labels(issue_number, labels_to_add)

    # Close issue
    gh_client.close_issue(issue_number)

    # Update Projects V2
    if issue_node_id and gh_client.project_id:
        logger.info(
            "Updating Project status to '%s'",
            LABEL_TO_STATUS["triage-mismatched"],
        )
        applied = gh_client.update_project_status(
            issue_node_id, LABEL_TO_STATUS["triage-mismatched"]
        )
        if not applied:
            logger.warning("Projects V2 status update degraded — no re-assert.")
        else:
            # The close above races GitHub's built-in "item closed -> Done"
            # workflow, which fires ~1s later; re-assert so Mismatched/Closed wins.
            gh_client.ensure_project_status(
                issue_node_id, LABEL_TO_STATUS["triage-mismatched"]
            )


def _create_tailored_application_branch(
    repo_path: pathlib.Path,
    branch_name: str,
    tailored_resume: Any,
    job_details: dict[str, str],
) -> None:
    """Checkout tailored branch, write, compile, commit, and push changes."""
    # Track the original branch to return back cleanly
    try:
        original_branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path)
    except Exception as e:
        logger.warning(
            "Could not determine current git branch, defaulting to 'main': %s",
            e,
        )
        original_branch = "main"

    branch_switched = False
    try:
        # Checkout/create target branch
        create_or_checkout_branch(repo_path, branch_name)
        branch_switched = True

        # Write tailored resume files
        resumes_dir = repo_path / "resumes"
        resumes_dir.mkdir(parents=True, exist_ok=True)

        # 1. Overwrite resume.yaml
        yaml_content = render_resume_yaml(tailored_resume)
        with (resumes_dir / "resume.yaml").open("w", encoding="utf-8") as f:
            f.write(yaml_content)

        # 2. Compile JSON & PDF
        compile_resume(
            tailored_resume,
            resumes_dir / "template.html",
            resumes_dir / "resume.pdf",
            resumes_dir / "resume.json",
        )

        # Commit and push
        commit_changes(
            repo_path,
            [
                "resumes/resume.yaml",
                "resumes/resume.json",
                "resumes/resume.pdf",
            ],
            job_details["company"],
            job_details["role"],
        )
        push_branch(repo_path, branch_name)

    finally:
        # Only revert checkout if branch was actually switched
        if branch_switched and branch_name != original_branch:
            logger.info("Returning to original branch: %s", original_branch)
            try:
                # Use force checkout to clean any partially modified states
                run_git(["checkout", "-f", original_branch], cwd=repo_path)
            except Exception as e:
                logger.error(
                    "Failed to checkout back to original branch '%s': %s",
                    original_branch,
                    e,
                )


def _get_resume_yaml_diff(repo_path: pathlib.Path, branch_name: str) -> str:
    """Return the unified diff of the resume YAML between main and branch.

    Uses the three-dot form so the comparison matches GitHub's compare view
    (merge-base of main and the branch, up to the branch tip).
    """
    return run_git(
        ["diff", f"main...{branch_name}", "--", RESUME_YAML_PATH],
        cwd=repo_path,
    )


def _build_inline_diff_section(repo_path: pathlib.Path, branch_name: str) -> str:
    """Build a collapsible <details> section with the resume YAML diff.

    Returns an empty string when the diff cannot be computed or shows no
    changes, so this optional enhancement never breaks comment posting.
    """
    try:
        raw_diff = _get_resume_yaml_diff(repo_path, branch_name)
    except GitOpsError as e:
        logger.warning("Could not generate inline resume diff: %s", e)
        return ""
    if not raw_diff.strip():
        return ""

    # Four backticks so any triple-backtick fences inside the diff text
    # cannot prematurely close the code block.
    clean_diff = raw_diff.strip()
    truncated = len(clean_diff) > INLINE_DIFF_MAX_CHARS
    shown_diff = clean_diff[:INLINE_DIFF_MAX_CHARS]
    if truncated and "\n" in shown_diff:
        # Back up to the last complete line so the fence never ends on a
        # garbled partial hunk line.
        shown_diff = shown_diff[: shown_diff.rfind("\n")]
    truncation_note = (
        "\n_Diff truncated due to length. Use the compare link above._"
        if truncated
        else ""
    )
    return (
        "<details>\n"
        "<summary>Resume YAML Diff</summary>\n\n"
        f"````diff\n{shown_diff}\n````\n"
        f"{truncation_note}\n"
        "</details>\n"
    )


def get_fit_grade_label(fit_score: float) -> str:
    """Map a fit score to its tier label for approved matches.

    Tiers (scores are on a 1.0-5.0 scale):
        - fit:A+ when score > 4.5
        - fit:A   when 4.0 < score <= 4.5
        - fit:B   when 3.5 <= score <= 4.0
    """
    if fit_score > FIT_GRADE_A_PLUS_MIN:
        return "fit:A+"
    if fit_score > FIT_GRADE_A_MIN:
        return "fit:A"
    if fit_score >= FIT_GRADE_B_MIN:
        return "fit:B"
    raise ValueError(
        f"fit score {fit_score} is below the minimum applicable tier "
        f"({FIT_GRADE_B_MIN})."
    )


def _handle_approved_match(
    issue_number: int,
    issue_node_id: str | None,
    issue_labels: list[str],
    job_details: dict[str, str],
    triage_res: TriageResult,
    repo_path: pathlib.Path,
    gh_client: GitHubClient,
    settings: Any,
    resume: Any,
    llm_client: LLMClient,
    initial_status: str | None = None,
) -> None:
    """Handle the approved match and resume tailoring branch pipeline."""
    logger.info("Fit score meets threshold. Tailoring resume...")

    status_to_lifecycle = {
        "applied": "applied",
        "interviewing": "in-loop",
        "offer_received": "offer-received",
        "rejected": "rejected",
    }
    status_to_header = {
        "applied": "Already Applied",
        "interviewing": "Interviewing / In Loop",
        "offer_received": "Offer Received",
        "rejected": "Rejected",
    }

    try:
        # Pass 2: Resume Tailoring
        tailored_resume = llm_client.tailor_resume(job_details["description"], resume)

        # Git Branch & Commit/Push Pipeline
        branch_name = generate_branch_name(
            job_details["company"], job_details["role"], job_details["apply_url"]
        )
        logger.info("Target application branch: %s", branch_name)

        _create_tailored_application_branch(
            repo_path=repo_path,
            branch_name=branch_name,
            tailored_resume=tailored_resume,
            job_details=job_details,
        )

        # Post approval comment
        pdf_blob_url = (
            f"https://github.com/{gh_client.repo}/blob/{branch_name}/resumes/resume.pdf"
        )
        yaml_hash = hashlib.sha256(RESUME_YAML_PATH.encode("utf-8")).hexdigest()
        if initial_status:
            status_text = status_to_header.get(initial_status, "Status Updated")
            header = (
                f"### AI Triage: {status_text} "
                f"(Fit Score: {triage_res.fit_score:.1f})\n\n"
            )
        else:
            header = (
                f"### AI Triage: Match Approved! "
                f"(Fit Score: {triage_res.fit_score:.1f}/{settings.fit_threshold:.1f}, "
                f"Grade: {get_fit_grade_label(triage_res.fit_score)})\n\n"
            )

        comment_body = (
            header + f"**Reasoning:**\n{triage_res.reasoning}\n\n"
            f"#### Score Breakdown:\n"
            f"- **Tech Stack Match:** {triage_res.tech_stack_fit:.1f}/5.0\n"
            f"- **Experience & Years Fit:** {triage_res.experience_fit:.1f}/5.0\n"
            f"- **Location & Timezone Suitability:** "
            f"{triage_res.location_fit:.1f}/5.0\n"
            f"- **Salary Alignment:** {triage_res.salary_fit:.1f}/5.0\n"
            f"- **Industry Domain Familiarity:** "
            f"{triage_res.industry_fit:.1f}/5.0\n\n"
            f"---\n\n"
            f"### Resume Tailored Successfully\n"
            f"- **Application Branch:** [{branch_name}]"
            f"(https://github.com/{gh_client.repo}/tree/{branch_name})\n"
            f"- **Resume YAML Diff:** [Compare Changes]"
            f"(https://github.com/{gh_client.repo}/compare/main...{branch_name}"
            f"#diff-{yaml_hash})\n"
            f"- **Tailored Resume PDF:** [View/Download PDF]({pdf_blob_url})\n"
            f'- **Apply URL:** <a href="{job_details["apply_url"]}" '
            f'target="_blank">Link to Posting</a>\n'
        )
        inline_diff_section = _build_inline_diff_section(repo_path, branch_name)
        if inline_diff_section:
            comment_body += f"\n{inline_diff_section}"
        gh_client.post_comment(issue_number, comment_body)

        # Update labels
        if "triage-pending" in issue_labels:
            gh_client.remove_label(issue_number, "triage-pending")
        if initial_status:
            target_label = status_to_lifecycle[initial_status]
            gh_client.add_labels(issue_number, [target_label])
        else:
            target_label = "ready-to-apply"
            gh_client.add_labels(
                issue_number,
                [get_fit_grade_label(triage_res.fit_score), target_label],
            )

        # Update Projects V2 status
        if issue_node_id and gh_client.project_id:
            logger.info(
                "Updating Project status to '%s'",
                LABEL_TO_STATUS[target_label],
            )
            gh_client.update_project_status(
                issue_node_id, LABEL_TO_STATUS[target_label]
            )

    except Exception as e:
        logger.error("Error occurred during approved match tailoring: %s", e)
        # Post diagnostic failure comment to the issue
        err_comment = (
            f"### AI Triage & Tailoring: System Error\n\n"
            f"An error occurred while compiling or committing the tailored resume:\n"
            f"```\n{str(e)}\n```\n"
            f"Please inspect the workflow execution log for detailed traceback info."
        )
        try:
            gh_client.post_comment(issue_number, err_comment)
        except Exception as post_err:
            logger.error("Failed to post failure comment to issue: %s", post_err)
        raise


def run_triage(
    issue_number: int,
    issue_title: str,
    issue_body: str,
    issue_node_id: str | None,
    issue_labels: list[str],
    repo_path: pathlib.Path,
    gh_client: GitHubClient,
    settings: Any,
    resume: Any,
    llm_client: LLMClient,
    web_client: Any | None = None,
    initial_status: str | None = None,
) -> None:
    """Orchestrate the triage and tailoring process for a single issue.

    Args:
        issue_number: The issue number to triage.
        issue_title: The title of the issue.
        issue_body: The markdown body of the issue.
        issue_node_id: The node ID of the issue (needed for Projects V2).
        issue_labels: List of label names on the issue.
        repo_path: Path to the local git repository.
        gh_client: The GitHub client to interact with the repository.
        settings: Parsed Settings object.
        resume: Parsed base Resume object.
        llm_client: Instantiated LLM client.
        web_client: Optional WebClient used to fetch URL-only issues when the
            body has no job-description section (spec 5.5).
    """
    logger.info("Starting triage for issue #%d: %s", issue_number, issue_title)

    # 1. Parse details from issue body
    job_details = parse_job_details(issue_body, issue_title)
    logger.info(
        "Parsed job details: %s at %s",
        job_details["role"],
        job_details["company"],
    )

    # URL-aware parsing (spec 5.5): when the body has no job-description
    # section but carries an apply_url, fetch the posting and substitute its
    # extracted text (plus inferred company/role) before evaluation.
    if (
        not _has_job_description_section(issue_body)
        and job_details["apply_url"]
        and web_client is not None
    ):
        apply_url = job_details["apply_url"]
        try:
            page = fetch_job_page(apply_url, web_client)
        except JobFetchError as e:
            logger.warning(
                "Could not fetch job posting for issue #%d: %s", issue_number, e
            )
            post_fetch_failure_comment(issue_number, gh_client, apply_url, str(e))
            return
        job_details = infer_job_details_from_page(
            page_text=page["description"],
            page_title=page["title"],
            url=apply_url,
            llm_client=llm_client,
        )
        job_details["apply_url"] = _sanitize_apply_url(apply_url)
        logger.info(
            "Fetched job posting: %s at %s",
            job_details["role"],
            job_details["company"],
        )

    # 2. Triage evaluation
    logger.info("Evaluating job description with LLM...")
    triage_res = llm_client.triage_job(
        job_details["description"],
        resume,
        work_preference=settings.search.work_preference,
    )
    # Update issue title to follow the canonical [Company] Role format
    company = job_details.get("company", DEFAULT_COMPANY)
    role = job_details.get("role", DEFAULT_ROLE)
    new_title = f"[{company}] {role}"
    if issue_title != new_title:
        try:
            logger.info("Updating issue title to: %s", new_title)
            gh_client.update_issue_title(issue_number, new_title)
        except Exception as e:
            logger.warning("Failed to update issue title: %s", e)

    # 3. Act on fit score
    if triage_res.fit_score < settings.fit_threshold and initial_status is None:
        _handle_mismatch(
            issue_number=issue_number,
            issue_node_id=issue_node_id,
            issue_labels=issue_labels,
            triage_res=triage_res,
            fit_threshold=settings.fit_threshold,
            gh_client=gh_client,
        )
    else:
        _handle_approved_match(
            issue_number=issue_number,
            issue_node_id=issue_node_id,
            issue_labels=issue_labels,
            job_details=job_details,
            triage_res=triage_res,
            repo_path=repo_path,
            gh_client=gh_client,
            settings=settings,
            resume=resume,
            llm_client=llm_client,
            initial_status=initial_status,
        )


def run_all_pending(
    gh_client: GitHubClient,
    repo_path: pathlib.Path,
    settings: Any,
    resume: Any,
    llm_client: LLMClient,
    web_client: Any | None = None,
) -> ExitCode:
    """Triage every open issue carrying the ``triage-pending`` label.

    Used by the ``--all-pending`` mode invoked from the ``workflow_run``
    trigger (scraper completion). Individual issue failures are logged and
    do not abort the batch, so one broken posting never blocks the rest.

    Returns:
        A process exit code: ``0`` on full success, ``EXIT_QUOTA_EXCEEDED``
        when the LLM quota was exceeded, or ``1`` when any issue failed.
    """
    failed = 0
    page = 1
    total_pending = 0
    logger.info("Listing open issues labeled 'triage-pending'...")
    while True:
        try:
            issues = gh_client.list_issues(
                state="open",
                labels="triage-pending",
                per_page=BATCH_PAGE_SIZE,
                page=page,
                sort="created",
                direction="asc",
            )
        except Exception as e:
            logger.error("Failed to list triage-pending issues: %s", e)
            return EXIT_ERROR

        pending = [
            issue
            for issue in issues
            if "triage-pending" in extract_label_names(issue.get("labels", []))
        ]
        total_pending += len(pending)
        for issue in pending:
            issue_number = issue.get("number")
            if not issue_number:
                continue
            title = issue.get("title", "") or ""
            body = issue.get("body", "") or ""
            logger.info("Triage-all: processing issue #%d", issue_number)
            try:
                run_triage(
                    issue_number=issue_number,
                    issue_title=title,
                    issue_body=body,
                    issue_node_id=issue.get("node_id"),
                    issue_labels=extract_label_names(issue.get("labels", [])),
                    repo_path=repo_path,
                    gh_client=gh_client,
                    settings=settings,
                    resume=resume,
                    llm_client=llm_client,
                    web_client=web_client,
                )
            except QuotaExceededError as e:
                logger.warning(
                    "LLM API quota exceeded while triaging #%d: %s", issue_number, e
                )
                return EXIT_QUOTA_EXCEEDED
            except Exception as e:
                failed += 1
                logger.exception("Triage failed for issue #%d: %s", issue_number, e)

        if len(issues) < BATCH_PAGE_SIZE:
            break
        page += 1

    logger.info("Found %d triage-pending issue(s).", total_pending)

    if failed:
        return EXIT_ERROR

    return EXIT_SUCCESS


def main() -> None:
    """CLI entry point for triage and tailoring coordinator."""
    # Configure logging
    setup_logging()

    parser = argparse.ArgumentParser(description="Triage and tailoring coordinator.")
    parser.add_argument(
        "--issue", "-i", type=int, help="GitHub issue number to triage."
    )
    parser.add_argument(
        "--all-pending",
        action="store_true",
        help="Triage every open issue carrying the 'triage-pending' label.",
    )
    parser.add_argument(
        "--event-path",
        type=str,
        help="Path to GitHub webhook event JSON file (e.g. GITHUB_EVENT_PATH).",
    )
    add_repo_path_argument(parser)
    args = parser.parse_args()

    repo_path = resolve_repo_path(args.repo_path)

    # Load configurations and base resume
    try:
        settings = load_settings(repo_path / "config/settings.yaml")
        resume = load_resume(repo_path / "resumes/resume.yaml")
    except Exception as e:
        logger.error("Failed to load settings or base resume configuration: %s", e)
        sys.exit(EXIT_ERROR)

    # Load environmental tokens and details
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")

    # Read from event payload if available
    event_path = args.event_path or os.environ.get("GITHUB_EVENT_PATH")

    issue_number = args.issue or (
        int(os.environ.get("ISSUE_NUMBER")) if os.environ.get("ISSUE_NUMBER") else None
    )
    issue_title = ""
    issue_body = ""
    issue_node_id = None
    issue_labels: list[str] = []

    if event_path:
        logger.info("Loading GitHub event payload from: %s", event_path)
        try:
            with pathlib.Path(event_path).open("r", encoding="utf-8") as f:
                event = json.load(f)
            issue_data = event.get("issue", {})
            if not issue_number:
                issue_number = issue_data.get("number")
            issue_title = issue_data.get("title", "")
            issue_body = issue_data.get("body", "")
            issue_node_id = issue_data.get("node_id")

            # Extract labels from event payload
            issue_labels = extract_label_names(issue_data.get("labels", []))

            # Repository can be resolved from payload repository full_name
            # if env is missing
            if not repo and "repository" in event:
                repo = event["repository"].get("full_name")
        except Exception as e:
            logger.error("Failed to parse event payload JSON: %s", e)
            sys.exit(EXIT_ERROR)

    if not token:
        logger.error("GITHUB_TOKEN environment variable is missing.")
        sys.exit(EXIT_ERROR)

    if not repo:
        logger.error("GITHUB_REPOSITORY environment variable is missing.")
        sys.exit(EXIT_ERROR)

    if not issue_number and not args.all_pending:
        logger.error("No issue number or event payload specified to triage.")
        sys.exit(EXIT_ERROR)

    # Initialize GitHub client
    project_id = settings.projects_v2.project_id if settings.projects_v2 else None
    status_field = (
        settings.projects_v2.status_field_name if settings.projects_v2 else "Status"
    )

    try:
        gh_client = GitHubClient(
            token=token,
            repo=repo,
            project_id=project_id,
            status_field_name=status_field,
        )
    except Exception as e:
        logger.error("Failed to initialize GitHubClient: %s", e)
        sys.exit(EXIT_ERROR)

    # Batch mode (workflow_run trigger): triage all triage-pending issues.
    if args.all_pending:
        try:
            llm_client = get_llm_client()
            web_client = WebClient(settings.research)
            exit_code = run_all_pending(
                gh_client=gh_client,
                repo_path=repo_path,
                settings=settings,
                resume=resume,
                llm_client=llm_client,
                web_client=web_client,
            )
        except QuotaExceededError as e:
            logger.warning("LLM API quota exceeded: %s", e)
            sys.exit(EXIT_QUOTA_EXCEEDED)
        except Exception as e:
            logger.exception("Triage-all coordinator failed: %s", e)
            sys.exit(EXIT_ERROR)

        if exit_code:
            sys.exit(exit_code)
        return

    # Fetch issue details if we didn't get them from a payload file
    if not issue_title or not issue_body:
        logger.info("Fetching issue details for #%d from GitHub API...", issue_number)
        try:
            issue = gh_client.get_issue(issue_number)
            issue_title = issue.get("title", "")
            issue_body = issue.get("body", "")
            issue_node_id = issue.get("node_id")

            # Extract labels from API response
            issue_labels = extract_label_names(issue.get("labels", []))
        except Exception as e:
            logger.error("Failed to fetch issue #%d: %s", issue_number, e)
            sys.exit(EXIT_ERROR)

    # Execute triage orchestration
    try:
        llm_client = get_llm_client()
        web_client = WebClient(settings.research)
        run_triage(
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            issue_node_id=issue_node_id,
            issue_labels=issue_labels,
            repo_path=repo_path,
            gh_client=gh_client,
            settings=settings,
            resume=resume,
            llm_client=llm_client,
            web_client=web_client,
            initial_status=None,
        )
    except QuotaExceededError as e:
        logger.warning("LLM API quota exceeded: %s", e)
        sys.exit(EXIT_QUOTA_EXCEEDED)
    except Exception as e:
        logger.exception("Triage coordinator failed: %s", e)
        sys.exit(EXIT_ERROR)


if __name__ == "__main__":
    main()
