"""Candidate-pool assembly for Gmail-integration issue matching.

Builds the list of open, actively-pursued job issues (spec
`specs/gmail-integration.md` §5.3) that later Gmail-matching tasks narrow
via a deterministic pre-filter (§6.1) and resolve via a single LLM call
(§6.2). This module is intentionally standalone: it only lists and filters
issues and extracts already-parsed job details, with no Gmail, LLM, or
orchestration logic here.
"""

import logging
from dataclasses import dataclass

from jobgitops.cli.triage import parse_job_details
from jobgitops.github_client import GitHubClient, extract_label_names
from jobgitops.status_model import CLOSURE_LABELS, LIFECYCLE_LABELS

logger = logging.getLogger("jobgitops.gmail_match")

# Number of issues requested per GitHub API page. Mirrors the pagination
# pattern already used by `triage.py` (`BATCH_PAGE_SIZE`) and `scraper.py`:
# loop on `page` with `per_page=100` until a page comes back short.
PAGE_SIZE = 100

# Lifecycle labels representing an issue with an application still in
# flight: every non-pending, non-terminal stage. `CLOSURE_LABELS` (
# `rejected`, `triage-mismatched`) already close the issue on the GitHub
# side, so the `state="open"` fetch below excludes them in practice; the
# explicit subtraction here documents that this pool is scoped to active
# applications rather than relying solely on issue state (spec §5.3).
ACTIVE_LIFECYCLE_LABELS: frozenset[str] = (
    LIFECYCLE_LABELS - {"triage-pending"} - CLOSURE_LABELS
)


@dataclass
class Candidate:
    """A single open, actively-pursued job issue eligible for email matching."""

    number: int
    title: str
    company: str
    role: str
    apply_url: str


def get_candidate_pool(gh_client: GitHubClient) -> list[Candidate]:
    """Assemble the pool of open, actively-pursued job-issue candidates.

    Lists every open issue via paginated `gh_client.list_issues` calls
    (looping on `page` with `per_page=100` until a page comes back short of
    `PAGE_SIZE`), then filters client-side to issues carrying one of
    `ACTIVE_LIFECYCLE_LABELS`. Server-side `labels=` filtering is
    deliberately not used: GitHub's `issues?labels=` query parameter is an
    AND filter across the listed labels, and lifecycle labels are mutually
    exclusive, so passing all four as one comma-separated `labels=` value
    would return zero results instead of their union (spec §5.3).

    Args:
        gh_client: Initialized GitHub client wrapper.

    Returns:
        One `Candidate` per matching open issue, in listing order. Callers
        that need a stable pool for an entire workflow run should call this
        once and reuse the result rather than recomputing it per message.
    """
    candidates: list[Candidate] = []
    page = 1
    while True:
        issues = gh_client.list_issues(
            state="open",
            per_page=PAGE_SIZE,
            page=page,
        )
        for issue in issues:
            labels = extract_label_names(issue.get("labels", []))
            if ACTIVE_LIFECYCLE_LABELS.isdisjoint(labels):
                continue

            number = issue.get("number")
            if not number:
                continue

            title = issue.get("title") or ""
            details = parse_job_details(issue.get("body"), title)
            candidates.append(
                Candidate(
                    number=number,
                    title=title,
                    company=details["company"],
                    role=details["role"],
                    apply_url=details["apply_url"],
                )
            )

        if len(issues) < PAGE_SIZE:
            break
        page += 1

    return candidates
