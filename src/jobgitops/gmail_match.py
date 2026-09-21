"""Candidate-pool assembly for Gmail-integration issue matching.

Builds the list of open, actively-pursued job issues (spec
`specs/gmail-integration.md` §5.3) that later Gmail-matching tasks narrow
via a deterministic pre-filter (§6.1) and resolve via a single LLM call
(§6.2). This module is intentionally standalone: it only lists and filters
issues and extracts already-parsed job details, with no Gmail, LLM, or
orchestration logic here.
"""

import logging
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

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


# --- Deterministic pre-filter (spec `specs/gmail-integration.md` §6.1) -----
#
# `prefilter_candidates` below narrows -- but must NEVER itself resolve --
# which candidates the (separate, not-implemented-here) `EMAIL_MATCH_PROMPT`
# LLM call gets to see. A hit here is a cheap, spoofable signal (a forged
# `From` display name can say anything -- DMARC only authenticates the
# sending *domain*, §5.1.2/§9.2), so the strongest thing this module is
# allowed to return is "look at only these candidates"; deciding that one of
# them *is* the match is exclusively the LLM call's job.

# Matches http(s) URLs in free text. The excluded characters
# (whitespace, angle brackets, quotes, a closing paren/bracket) stop the
# match at whatever visually closes a URL in prose, e.g. "(https://a.co/b)"
# or a markdown link `<https://a.co/b>`. Punctuation that can legitimately
# sit inside a URL (`.`, `,`, `;`, `:`, `!`, `?`) is not excluded here, so a
# small trailing set is stripped separately below only when it appears at
# the very end of a match (see `_TRAILING_PUNCTUATION`).
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")

# Sentence-trailing punctuation that can immediately follow a URL with no
# separating whitespace (e.g. "...see https://acme.com/jobs."). Stripped
# from the end of each extracted match only; never from the middle.
_TRAILING_PUNCTUATION = ".,;:!?"

# Extracts the domain from a `From` header value, which may be a bare
# address ("jobs@acme.com") or a display-name form
# ("Acme Recruiting <jobs@acme.com>").
_EMAIL_ADDRESS_RE = re.compile(r"[\w.+-]+@([\w.-]+)")

# Legal-entity suffixes to strip during company-name normalization
# (case-insensitive), matched only at the end of the string along with any
# separating punctuation/whitespace. Limited to the suffixes named in spec
# §6.1 (Inc/Inc./LLC/L.L.C./Corp/Corp./Corporation); not an exhaustive
# legal-suffix list, and only a single trailing suffix is stripped.
_COMPANY_SUFFIX_RE = re.compile(
    r"[\s,.-]*\b(?:l\.l\.c\.?|llc|inc\.?|corp(?:oration)?\.?)\.?\s*$",
    re.IGNORECASE,
)

# Anything that isn't a "word" character or whitespace, collapsed to a
# space so e.g. "Acme, Inc." and "Acme & Co" normalize predictably.
_NON_WORD_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")


def _extract_urls(text: str) -> list[str]:
    """Extract http(s) URLs from free text, dropping trailing punctuation."""
    return [match.rstrip(_TRAILING_PUNCTUATION) for match in _URL_RE.findall(text)]


def _sender_domain(email_sender: str) -> str:
    """Parse the domain out of a `From` header value, or "" if none found."""
    match = _EMAIL_ADDRESS_RE.search(email_sender)
    return match.group(1).lower() if match else ""


def _url_path_key(url: str) -> tuple[str, tuple[str, ...]] | None:
    """Build a (netloc, path-segments) comparison key for a URL.

    Returns `None` for a URL with no netloc (nothing to compare). Path
    segments are the full, non-empty, lowercased `/`-split segments of
    `urlparse(url).path` -- not just the first one. A first-segment-only
    comparison cannot tell `acme.com/jobs/123` apart from
    `acme.com/jobs/1234` (both have first segment "jobs"); comparing the
    whole segment sequence for exact equality fixes that false-positive
    while still matching spec §6.1's own canonical example
    (`boards.greenhouse.io/acme-corp`), whose apply URL has only one
    segment to begin with -- so "share a host and the (whole) path" is a
    strictly safer reading of "share a host and first path segment" than a
    literal first-segment-only comparison, and a raw string-prefix check
    (e.g. `apply_url in body_text`) is avoided entirely, since that would
    also false-positive on the `/jobs/123` vs `/jobs/1234` case.
    """
    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    if not netloc:
        return None
    segments = tuple(segment.lower() for segment in parsed.path.split("/") if segment)
    return (netloc, segments)


def _has_url_hit(email_sender: str, email_body: str, apply_url: str) -> bool:
    """True if a body URL or the sender's domain matches `apply_url`'s
    host + path (see `_url_path_key`).

    The sender's domain is checked as well as body URLs, not instead of
    them, because ATS platforms commonly send from their own domain
    (`greenhouse.io`, `lever.co`) rather than the hiring company's -- for
    those, the posting's host+path only shows up in a body link, but for an
    ATS that *does* send from a domain matching the apply URL's host (or a
    company that emails directly from its own careers-page domain), the
    `From` domain alone is already a valid, cheap signal worth checking
    even if the body happens to contain no matching link at all.
    """
    apply_key = _url_path_key(apply_url)
    if apply_key is None:
        return False

    candidate_urls = _extract_urls(email_body)
    sender_domain = _sender_domain(email_sender)
    if sender_domain:
        candidate_urls.append(f"https://{sender_domain}")

    return any(_url_path_key(url) == apply_key for url in candidate_urls)


def _normalize_company_text(text: str) -> str:
    """Normalize a company/sender/subject string for substring comparison.

    Strips a single trailing legal-entity suffix (see `_COMPANY_SUFFIX_RE`)
    along with its surrounding punctuation, lowercases, then collapses all
    remaining punctuation and whitespace so e.g. "Acme, Inc." and "ACME  Co"
    -style variants normalize predictably.
    """
    normalized = _COMPANY_SUFFIX_RE.sub("", text.strip())
    normalized = normalized.lower()
    normalized = _NON_WORD_RE.sub(" ", normalized)
    return _WHITESPACE_RE.sub(" ", normalized).strip()


def _has_company_hit(company: str, email_sender: str, email_subject: str) -> bool:
    """True if the normalized `company` appears, word-boundary-aware, in
    the normalized sender display name or subject.

    Word-boundary awareness is deliberate, not incidental: a raw substring
    check on a short/generic company name (e.g. "Wave") would false-positive
    on an unrelated sender whose name merely contains it as a substring
    (e.g. "Waveform Recruiting") -- see the regression test for this exact
    case. Requiring `\\b` boundaries around the full (possibly multi-word)
    normalized company name avoids that without losing genuine multi-word
    matches (e.g. "Acme Corp" still matches a sender named "Acme Corp
    Careers").
    """
    normalized_company = _normalize_company_text(company)
    if not normalized_company:
        return False

    pattern = re.compile(r"\b" + re.escape(normalized_company) + r"\b")
    return bool(
        pattern.search(_normalize_company_text(email_sender))
        or pattern.search(_normalize_company_text(email_subject))
    )


@dataclass
class PreFilterResult:
    """The tiered outcome of `prefilter_candidates` (spec §6.1).

    `tier` disambiguates what `candidates` means -- a bare list of size N is
    ambiguous between "zero-hit tier, pool just happens to have N issues"
    and "N-candidate multi-hit tier," a conflation an earlier spec draft
    made that incorrectly triggered downstream ambiguous-match handling
    (§6.2). Callers must key behavior off `tier`, never off
    `len(candidates)` alone.
    """

    tier: Literal["zero_hit", "one_hit", "multi_hit"]
    candidates: list[Candidate]


def prefilter_candidates(
    email_sender: str,
    email_subject: str,
    email_body: str,
    candidates: list[Candidate],
) -> PreFilterResult:
    """Narrow, but never resolve, which candidates the match call should see.

    For each candidate, checks two independent, cheap signals against the
    (already DMARC-authenticated, per §5.1.2) email: a URL/domain hit
    (`_has_url_hit`) and a company-string hit (`_has_company_hit`). A
    candidate counts as a hit if either signal matches. Results are grouped
    into the three tiers from spec §6.1's table:

    - Exactly one candidate hit: `tier="one_hit"`, `candidates` is just
      that one candidate.
    - Zero candidates hit: `tier="zero_hit"`, `candidates` is the full,
      unfiltered input `candidates` list unchanged -- the same coverage the
      match call would have had with no pre-filter at all.
    - Two or more candidates hit: `tier="multi_hit"`, `candidates` is just
      the narrowed subset that hit.

    This function is pure (no I/O, no side effects, does not mutate its
    inputs) and is intentionally *not* a match resolver: the company-string
    signal alone is spoofable (a forged sender display name can say
    anything; DMARC authenticates only the sending domain, not that
    free-text name -- §5.1.2/§9.2), so even a single unambiguous hit here
    must still be independently confirmed by the `EMAIL_MATCH_PROMPT` LLM
    call (§6.2) before anything executes. This function structurally cannot
    be misused as an auto-resolver: it only ever returns a candidate list
    for that later call to judge, never a single resolved answer or a
    boolean "is this the match."

    Args:
        email_sender: The email's `From` header value (display name and/or
            address).
        email_subject: The email's subject line.
        email_body: The email's body text.
        candidates: The full candidate pool (`get_candidate_pool`) to
            narrow.

    Returns:
        A `PreFilterResult` tagging the narrowed (or, on zero hits, full
        unfiltered) candidate list with its tier.
    """
    hits = [
        candidate
        for candidate in candidates
        if _has_url_hit(email_sender, email_body, candidate.apply_url)
        or _has_company_hit(candidate.company, email_sender, email_subject)
    ]

    if len(hits) == 0:
        return PreFilterResult(tier="zero_hit", candidates=candidates)
    if len(hits) == 1:
        return PreFilterResult(tier="one_hit", candidates=hits)
    return PreFilterResult(tier="multi_hit", candidates=hits)
