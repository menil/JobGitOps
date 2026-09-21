"""Gmail integration orchestrator CLI entry point (spec `gmail-integration.md` §5.2/§7).

Wires together the whole Gmail-sync run: load config/secrets, load the
de-dup cursor from the dedicated `gmail-sync-state` orphan branch, resolve
the configured Gmail label, fetch new label-tagged messages (capped and
de-duplicated), run each through the DMARC gate -> deterministic pre-filter
(`gmail_match.py`) -> single `EMAIL_MATCH_PROMPT` LLM call (`llm.py`), apply
a resolved match through the existing `respond.execute_action` side-effect
path, and finally commit the advanced cursor back to `gmail-sync-state`.

No existing module's side-effect *behavior* changes here: this script is
purely a second caller of `respond.execute_action`, exactly like a
conversational comment already is (spec §6.3).
"""

import argparse
import contextlib
import email.utils
import json
import logging
import os
import pathlib
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from jobgitops.assistant import (
    ACTION_STATUS_UPDATE,
    GMAIL_NOTICE_MARKER,
    STATUS_CONFIRMATION_MARKER,
    VALID_STATUSES,
    AgentAction,
)
from jobgitops.cli import add_repo_path_argument, resolve_repo_path, setup_logging
from jobgitops.cli.respond import execute_action
from jobgitops.git_ops import GitOpsError, run_git
from jobgitops.github_client import GitHubClient, extract_label_names
from jobgitops.gmail_client import (
    GmailClient,
    GmailMessageNotFoundError,
    build_gmail_permalink,
    is_authentic,
)
from jobgitops.gmail_match import Candidate, get_candidate_pool, prefilter_candidates
from jobgitops.llm import (
    QuotaExceededError,
    ValidationError,
    get_llm_client,
    match_email_to_candidate,
)
from jobgitops.loader import load_resume, load_settings
from jobgitops.schema import GmailConfig, Resume, Settings

logger = logging.getLogger("jobgitops.gmail_sync")

# POSIX exit code for temporary quota/rate-limit failure (EX_TEMPFAIL),
# matching respond.py's/triage.py's existing convention.
EXIT_QUOTA_EXCEEDED = 75

# Per-run processing cap, oldest-eligible-message-first (spec §4.1/§5.2 step
# 4). Any remainder is simply left unprocessed -- nothing this run adds to
# the cursor covers it, so it's picked up automatically by the next run.
MAX_MESSAGES_PER_RUN = 50

# The dedicated orphan branch the sync cursor is committed to (spec §7).
# Never shares history with `main`; a plain `main`-based helper like
# `git_ops.create_or_checkout_branch` does not apply here.
STATE_BRANCH = "gmail-sync-state"
STATE_FILE_REL_PATH = "data/gmail-state.json"

# Fixed, mechanical commit message -- never templated with email-derived
# content (spec §7), so no extracted company/summary text ever lands in a
# commit message.
COMMIT_MESSAGE = "chore(gmail): advance sync cursor"

# The three secrets required when `settings.gmail.enabled` is true (spec §4.1/§8.2).
_GMAIL_SECRET_ENV_VARS = (
    "GMAIL_CLIENT_ID",
    "GMAIL_CLIENT_SECRET",
    "GMAIL_REFRESH_TOKEN",
)


class GmailSyncFatalError(Exception):
    """Raised for a fatal configuration error surfaced mid-run (spec §5.2 step 3):
    e.g. the configured Gmail label doesn't exist. Caught by `main()` and turned
    into a loud exit 1, the same treatment a missing LLM key gets today."""


def _missing_gmail_secrets() -> list[str]:
    """Return the names of any required Gmail secret env vars that are unset."""
    return [name for name in _GMAIL_SECRET_ENV_VARS if not os.environ.get(name)]


def _redact_gmail_secrets(text: str, *secrets: str) -> str:
    """Best-effort scrub of raw Gmail OAuth secret values out of `text`
    before it's logged (spec §9.5). Defense in depth for an underlying
    `google-auth`/`googleapiclient` exception message that happens to echo
    one of these values verbatim -- `git_ops.redact_sensitive_string` only
    recognizes GitHub token shapes, so it wouldn't catch these.
    """
    redacted = text
    # Longest first: if one secret is a substring of another, redacting the
    # shorter one first would leave a mangled remainder of the longer one
    # in the output instead of a clean [REDACTED].
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _format_iso(dt: datetime) -> str:
    """Format a datetime as UTC ISO 8601 with a trailing `Z`, matching the
    cursor's data-contract example (spec §7)."""
    return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp (with or without a trailing `Z`), or
    return None for anything absent/unparseable -- callers treat that as
    'unknown' rather than raising (this reads untrusted/aged cursor data)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _normalize_message_date(raw_date: str) -> str:
    """Coerce a Gmail `Date` header (RFC 2822) into the cursor's ISO 8601
    storage format (spec §7). 'Parse, don't validate': a missing or
    malformed header never raises here -- it falls back to "now", which at
    worst makes one cursor entry look slightly newer than it really was,
    never a crash over untrusted header text.
    """
    if raw_date:
        try:
            parsed = email.utils.parsedate_to_datetime(raw_date)
        except (TypeError, ValueError, OverflowError):
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return _format_iso(parsed)
    return _format_iso(datetime.now(UTC))


def _emit_gha_warning(message: str) -> None:
    """Print a GitHub Actions `::warning::` annotation.

    Printed directly to stdout rather than routed through the logging
    formatter (which prefixes a timestamp/level): GitHub Actions only
    recognizes the `::warning::` token when it is the first thing on the
    line. Also logged normally so it shows up in a plain log read too.
    """
    print(f"::warning::{message}")
    logger.warning(message)


def _check_staleness(last_synced_at: str | None, days_back: int, now: datetime) -> None:
    """Emit a staleness `::warning::` when the sync gap exceeds `days_back`
    (spec §5.2 step 2). Mail older than `days_back` from "now" can no longer
    be recovered by this run's query, so a gap this large means something
    may have been missed. Absent `last_synced_at` (first run, or a reset
    cursor) is treated as "unknown, assume stale".
    """
    last_synced = _parse_iso(last_synced_at)
    if last_synced is None:
        _emit_gha_warning(
            "Gmail sync has no recorded last_synced_at (first run, or the "
            "cursor was reset); treating the sync gap as unknown/stale."
        )
        return
    if now - last_synced > timedelta(days=days_back):
        _emit_gha_warning(
            f"Gmail sync last completed at {last_synced_at}, more than "
            f"days_back={days_back} days ago -- mail older than that window "
            "may have been permanently missed."
        )


def load_cursor(repo_path: pathlib.Path) -> dict[str, Any]:
    """Load `{processed, last_synced_at}` from `gmail-sync-state` (spec §7)
    without switching the caller's own working-tree branch.

    Fetches the branch, then reads the cursor file's blob content directly
    via `git show` rather than checking the branch out in place: unlike an
    `applications/*` branch (which shares history/files with `main`),
    `gmail-sync-state` is a disconnected orphan branch, so an in-place
    checkout here would replace every other file in the working directory
    (`config/settings.yaml`, this script's own source) for the rest of the
    run. The branch is only actually checked out once, right at the end
    (`_finalize_cursor`), immediately before the cursor commit.

    Returns:
        A dict with `processed` (a `{message_id: date}` map, possibly
        empty) and `last_synced_at` (a string or None).
    """
    try:
        run_git(["fetch", "origin", STATE_BRANCH], cwd=repo_path)
    except GitOpsError:
        logger.info(
            "%s does not exist on origin yet; starting with an empty cursor.",
            STATE_BRANCH,
        )
        return {"processed": {}, "last_synced_at": None}

    try:
        raw = run_git(
            ["show", f"origin/{STATE_BRANCH}:{STATE_FILE_REL_PATH}"], cwd=repo_path
        )
    except GitOpsError:
        logger.info(
            "%s has no %s yet; starting with an empty cursor.",
            STATE_BRANCH,
            STATE_FILE_REL_PATH,
        )
        return {"processed": {}, "last_synced_at": None}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(
            "Could not parse the Gmail sync cursor JSON (%s); starting with "
            "an empty cursor.",
            e,
        )
        return {"processed": {}, "last_synced_at": None}

    processed = data.get("processed") if isinstance(data, dict) else None
    if not isinstance(processed, dict):
        processed = {}
    last_synced_at = data.get("last_synced_at") if isinstance(data, dict) else None
    return {"processed": processed, "last_synced_at": last_synced_at}


def _select_batch(message_ids: list[str], processed: dict[str, str]) -> list[str]:
    """Filter out already-`processed` IDs, then cap to `MAX_MESSAGES_PER_RUN`
    oldest-first (spec §4.1/§5.2 step 4).

    Gmail's `messages.list` returns newest-first; reversing the already-
    filtered list gives oldest-first before capping, so a backlog burst
    still gets its oldest, most-overdue mail processed first. Anything past
    the cap is simply left out -- since none of it is added to the cursor,
    it's automatically picked up by the next run.
    """
    unprocessed = [
        message_id for message_id in message_ids if message_id not in processed
    ]
    oldest_first = list(reversed(unprocessed))
    return oldest_first[:MAX_MESSAGES_PER_RUN]


def _prune_processed(
    processed: dict[str, str], days_back: int, now: datetime
) -> dict[str, str]:
    """Drop `processed` entries older than `days_back + 1` days (spec §5.2
    step 6) -- a message that old will never be returned by the label +
    `days_back` query again, so it's provably safe to forget. Entries with
    an unparseable date are kept rather than dropped (fail safe: an
    unparseable date is not proof the entry is actually stale).
    """
    cutoff = now - timedelta(days=days_back + 1)
    pruned: dict[str, str] = {}
    for message_id, date_str in processed.items():
        parsed = _parse_iso(date_str)
        if parsed is None or parsed >= cutoff:
            pruned[message_id] = date_str
    return pruned


def _permalink_already_commented(
    gh_client: GitHubClient, issue_number: int, permalink: str, marker: str
) -> bool:
    """True if some existing comment on `issue_number` already carries both
    `marker` and `permalink` -- the idempotency guard for the at-least-once
    processing case (spec §5.2 step 5f, §9.6): `GitHubClient.post_comment` is
    not itself idempotent, so a replay after a cursor-commit failure must not
    duplicate a comment that already landed.

    Checking for `marker` together with `permalink`, not permalink alone,
    matters: a heads-up comment (`GMAIL_NOTICE_MARKER`) and a confirmation
    comment (`STATUS_CONFIRMATION_MARKER`) about the *same* message can both
    end up referencing the same Gmail permalink on the same issue -- e.g. a
    multi-hit heads-up on an earlier run, followed by a confident resolution
    on a retry. A permalink-only check would make that earlier heads-up
    permanently suppress the real confirmation/`execute_action` call for
    that message, which is not what "already handled" is supposed to mean.
    """
    comments = gh_client.list_comments(issue_number)
    return any(
        isinstance(comment, dict)
        and marker in (comment.get("body") or "")
        and permalink in (comment.get("body") or "")
        for comment in comments
    )


def _post_heads_up_comments(
    gh_client: GitHubClient,
    candidates: list[Candidate],
    summary: str,
    permalink: str,
) -> None:
    """Post the multi-hit ambiguous-match heads-up comment (spec §5.2 step
    5e) on each narrowed candidate, skipping any that already has one for
    this message (idempotency, spec §9.6). Marked with `GMAIL_NOTICE_MARKER`
    (not `STATUS_CONFIRMATION_MARKER`) so respond.py's bot-loop guard
    recognizes it without treating it as a status-update confirmation (spec
    §9.7); no label/status/Projects V2 change is applied.
    """
    detail = f": {summary}" if summary else ""
    body = (
        f"{GMAIL_NOTICE_MARKER}\n\n"
        f"Detected a possible status update from [this email]({permalink})"
        f"{detail} -- but couldn't confidently tell which application it "
        "belongs to. Please check and update the label manually if this is "
        "about this role."
    )
    for candidate in candidates:
        if _permalink_already_commented(
            gh_client, candidate.number, permalink, GMAIL_NOTICE_MARKER
        ):
            continue
        gh_client.post_comment(candidate.number, body)


def process_message(
    *,
    message_id: str,
    gmail_client: GmailClient,
    gh_client: GitHubClient,
    llm_client: Any,
    settings: Settings,
    resume: Resume,
    repo_path: pathlib.Path,
    candidate_pool: list[Candidate],
    processed: dict[str, str],
) -> None:
    """Handle a single Gmail message end to end (spec §5.2 step 5).

    Mutates `processed` in place, recording this message's normalized date
    for every outcome except an LLM/parse failure -- that one is
    deliberately left unprocessed so it's retried next run (spec §9.6),
    rather than aborting the whole batch. `QuotaExceededError` is the one
    exception that must NOT be swallowed here: it propagates so `main()`
    can exit 75.
    """
    try:
        message = gmail_client.get_message(message_id)
    except GmailMessageNotFoundError:
        # Deleted between listing and fetching -- nothing left to process
        # and nothing to retry either.
        processed[message_id] = _format_iso(datetime.now(UTC))
        return

    if not is_authentic(message.raw_auth_results):
        logger.debug("Message %s failed the DMARC gate; skipping.", message_id)
        processed[message_id] = _normalize_message_date(message.date)
        return

    prefilter_result = prefilter_candidates(
        message.sender, message.subject, message.body_text, candidate_pool
    )
    candidate_dicts = [
        {"number": c.number, "title": c.title, "company": c.company, "role": c.role}
        for c in prefilter_result.candidates
    ]

    try:
        match_result = match_email_to_candidate(
            llm_client,
            message.subject,
            message.sender,
            message.body_text,
            candidate_dicts,
            VALID_STATUSES,
        )
    except QuotaExceededError:
        raise
    except ValidationError as e:
        logger.warning(
            "Email match failed for message %s (left unprocessed for retry): %s",
            message_id,
            e,
        )
        return

    permalink = build_gmail_permalink(message_id)

    if match_result.status is None:
        # Authentic mail, not a lifecycle transition (spec §6.2 row 1).
        processed[message_id] = _normalize_message_date(message.date)
        return

    if match_result.issue_number is None:
        # Zero-hit/one-hit: quiet skip regardless of how large the full pool
        # was (spec §5.2 step 5d/§6.2) -- passing the whole pool because the
        # pre-filter found nothing is not itself evidence of ambiguity.
        # Multi-hit: real pre-filter signal on 2+ candidates the model still
        # couldn't resolve between -- heads-up, not silence (spec §5.2 step 5e).
        if prefilter_result.tier == "multi_hit":
            _post_heads_up_comments(
                gh_client, prefilter_result.candidates, match_result.summary, permalink
            )
        processed[message_id] = _normalize_message_date(message.date)
        return

    matched_candidate = next(
        (c for c in candidate_pool if c.number == match_result.issue_number), None
    )
    if matched_candidate is None:
        # `issue_number` is allowlisted against the candidates actually
        # passed into this call (llm.py's own §9.3 discipline), so this
        # should be unreachable; a quiet skip is the safe fallback either way.
        logger.warning(
            "Message %s resolved to issue #%s, which is not in the "
            "candidate pool; skipping.",
            message_id,
            match_result.issue_number,
        )
        processed[message_id] = _normalize_message_date(message.date)
        return

    if _permalink_already_commented(
        gh_client, matched_candidate.number, permalink, STATUS_CONFIRMATION_MARKER
    ):
        logger.info(
            "Message %s already has a confirmation comment on issue #%d; skipping.",
            message_id,
            matched_candidate.number,
        )
        processed[message_id] = _normalize_message_date(message.date)
        return

    issue = gh_client.get_issue(matched_candidate.number)
    issue_node_id = issue.get("node_id")
    current_labels = extract_label_names(issue.get("labels", []))

    reply = (
        f"{match_result.summary}\n\n{permalink}" if match_result.summary else permalink
    )
    action = AgentAction(
        action=ACTION_STATUS_UPDATE, status=match_result.status, reply=reply
    )
    execute_action(
        action,
        issue_number=matched_candidate.number,
        issue_title=matched_candidate.title,
        issue_body="",
        issue_node_id=issue_node_id,
        repo_path=repo_path,
        gh_client=gh_client,
        settings=settings,
        resume=resume,
        llm_client=llm_client,
        web_client=None,
        current_labels=current_labels,
    )
    processed[message_id] = _normalize_message_date(message.date)


def _write_cursor_file(repo_path: pathlib.Path, cursor: dict[str, Any]) -> None:
    """Write the cursor JSON to `data/gmail-state.json` under `repo_path`."""
    state_path = repo_path / STATE_FILE_REL_PATH
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(cursor, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _ensure_git_identity(repo_path: pathlib.Path) -> None:
    """Configure a local git identity only if one isn't already set
    (mirrors `git_ops.commit_changes`'s own check-then-set discipline, so a
    developer's real identity is never clobbered outside CI)."""
    try:
        run_git(["config", "user.name"], cwd=repo_path)
    except GitOpsError:
        run_git(["config", "user.name", "github-actions[bot]"], cwd=repo_path)
    try:
        run_git(["config", "user.email"], cwd=repo_path)
    except GitOpsError:
        run_git(
            ["config", "user.email", "github-actions[bot]@users.noreply.github.com"],
            cwd=repo_path,
        )


def _commit_cursor(repo_path: pathlib.Path) -> bool:
    """Stage and commit `data/gmail-state.json` with the fixed, mechanical
    commit message (spec §7). Returns False (no error) when there is
    nothing new to commit, e.g. an identical re-run.
    """
    run_git(["add", "--force", "--", STATE_FILE_REL_PATH], cwd=repo_path)
    diff_result = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={repo_path.resolve()}",
            "diff",
            "--cached",
            "--quiet",
        ],
        cwd=repo_path,
    )
    if diff_result.returncode == 0:
        return False
    _ensure_git_identity(repo_path)
    run_git(["commit", "-m", COMMIT_MESSAGE], cwd=repo_path)
    return True


def _checkout_state_branch(repo_path: pathlib.Path) -> None:
    """Fetch, then check out, `gmail-sync-state`, creating it as an orphan
    branch only on a genuine first-ever run (spec §5.2 step 7).

    The fetch MUST happen before the checkout: the existing
    `git_ops.create_or_checkout_branch` helper only checks *local* refs,
    which would treat every run on a fresh GitHub Actions runner as a first
    run and silently discard the cursor every single time -- a real bug an
    earlier design draft had (spec §13.8). This function intentionally does
    not reuse that helper, since `gmail-sync-state` is also an orphan
    branch (no shared history with `main`), unlike the `applications/*`
    branches that helper targets.

    When the branch already exists on origin, `checkout -B` (rather than a
    plain `checkout`) always resets the local branch to `origin/<branch>`
    right after the fetch above, instead of reusing whatever local ref might
    already be lying around -- on a non-ephemeral runner (or a re-run in the
    same checkout) that stale local history could otherwise get committed on
    top of, relying entirely on the push retry's own fetch+rebase to
    reconcile it after the fact.
    """
    try:
        run_git(["fetch", "origin", STATE_BRANCH], cwd=repo_path)
        remote_exists = True
    except GitOpsError:
        remote_exists = False

    if not remote_exists:
        run_git(["checkout", "--orphan", STATE_BRANCH, "--"], cwd=repo_path)
        _clear_working_tree(repo_path)
        return

    run_git(
        ["checkout", "-B", STATE_BRANCH, f"origin/{STATE_BRANCH}", "--"],
        cwd=repo_path,
    )


def _clear_working_tree(repo_path: pathlib.Path) -> None:
    """Untrack and remove every file inherited from the previous branch
    after an orphan checkout, so `gmail-sync-state` only ever tracks the
    cursor file. Only reached for a genuine first-ever sync, and only after
    every other step of the run has already completed (see
    `_finalize_cursor`, the last thing this script does)."""
    # Nothing was tracked yet (e.g. a brand-new repository) is the only
    # expected failure here; suppressed rather than treated as fatal.
    with contextlib.suppress(GitOpsError):
        run_git(["rm", "-rf", "--", "."], cwd=repo_path)


def _push_state_branch(repo_path: pathlib.Path) -> bool:
    """Push `gmail-sync-state` to origin; on rejection, fetch + rebase once
    and retry, then give up (spec §5.2 step 7).

    This branch is never force-pushed or amended, so
    `git_ops._push_with_lease_retry` (built for the force-push/amend case on
    `applications/*` branches) does not apply here -- an ordinary,
    non-force retry is both sufficient and correct.

    Returns:
        True if the push (initial or retried) succeeded, False if both
        attempts failed. A False return is logged by the caller, never
        raised: a cursor-push failure must not roll back the GitHub issue
        side effects already applied this run (spec §9.6).
    """
    try:
        run_git(["push", "--set-upstream", "origin", STATE_BRANCH], cwd=repo_path)
        return True
    except GitOpsError as first_error:
        logger.warning(
            "Initial push of %s was rejected (%s); fetching and rebasing "
            "once before retrying.",
            STATE_BRANCH,
            first_error,
        )

    try:
        run_git(["fetch", "origin", STATE_BRANCH], cwd=repo_path)
        run_git(["rebase", f"origin/{STATE_BRANCH}"], cwd=repo_path)
        run_git(["push", "--set-upstream", "origin", STATE_BRANCH], cwd=repo_path)
        return True
    except GitOpsError as retry_error:
        logger.error(
            "Push of %s failed again after the fetch+rebase retry: %s",
            STATE_BRANCH,
            retry_error,
        )
        return False


def _finalize_cursor(repo_path: pathlib.Path, cursor: dict[str, Any]) -> None:
    """Check out `gmail-sync-state`, write/commit/push the advanced cursor
    (spec §5.2 step 7).

    Every failure here -- checkout, write, commit, or push -- is logged, not
    raised: the GitHub issue side effects already applied this run must
    never be rolled back for a cursor-commit problem (spec §9.6) -- a stale
    cursor only means some messages get reprocessed next run, which the
    idempotency guards in `process_message` (already-processed /
    already-commented) make safe.
    """
    try:
        _checkout_state_branch(repo_path)
    except GitOpsError as e:
        logger.error(
            "Could not check out %s to commit the advanced cursor: %s", STATE_BRANCH, e
        )
        return

    try:
        _write_cursor_file(repo_path, cursor)
        committed = _commit_cursor(repo_path)
    except (GitOpsError, OSError) as e:
        logger.error(
            "Could not write/commit the advanced Gmail sync cursor to %s: "
            "%s; already-applied issue updates are unaffected.",
            STATE_BRANCH,
            e,
        )
        return

    if not committed:
        logger.info("Gmail sync cursor unchanged; nothing to commit.")
        return

    if _push_state_branch(repo_path):
        logger.info("Pushed the advanced Gmail sync cursor to %s.", STATE_BRANCH)
    else:
        logger.error(
            "Could not push the advanced Gmail sync cursor to %s after a "
            "retry; already-applied issue updates are unaffected, but this "
            "run's processed/dedup state was not persisted.",
            STATE_BRANCH,
        )


def run_sync(
    *,
    repo_path: pathlib.Path,
    gmail_config: GmailConfig,
    gh_client: GitHubClient,
    gmail_client: GmailClient,
    llm_client: Any,
    settings: Settings,
    resume: Resume,
    now: datetime | None = None,
) -> None:
    """Run one Gmail sync pass (spec §5.2 steps 2-7).

    Raises:
        GmailSyncFatalError: The configured Gmail label doesn't exist.
        QuotaExceededError: Propagated from a `match_email_to_candidate`
            call; the caller (`main`) maps this to exit 75.
    """
    now = now or datetime.now(UTC)

    cursor = load_cursor(repo_path)
    _check_staleness(cursor.get("last_synced_at"), gmail_config.days_back, now)

    label_id = gmail_client.resolve_label_id(gmail_config.label)
    if label_id is None:
        raise GmailSyncFatalError(
            f"Configured Gmail label {gmail_config.label!r} does not exist "
            "in this mailbox."
        )

    all_message_ids = gmail_client.list_message_ids(
        label_id, gmail_config.query, gmail_config.days_back
    )
    processed = cursor["processed"]
    batch_ids = _select_batch(all_message_ids, processed)
    unprocessed_total = sum(1 for mid in all_message_ids if mid not in processed)
    caught_up = unprocessed_total <= MAX_MESSAGES_PER_RUN

    candidate_pool = get_candidate_pool(gh_client)
    for message_id in batch_ids:
        process_message(
            message_id=message_id,
            gmail_client=gmail_client,
            gh_client=gh_client,
            llm_client=llm_client,
            settings=settings,
            resume=resume,
            repo_path=repo_path,
            candidate_pool=candidate_pool,
            processed=processed,
        )

    cursor["processed"] = _prune_processed(processed, gmail_config.days_back, now)
    if caught_up:
        cursor["last_synced_at"] = _format_iso(now)
    # else: a backlog bigger than MAX_MESSAGES_PER_RUN means this run did not
    # actually reach "now" -- leaving the previous last_synced_at in place
    # keeps _check_staleness's warning firing on every run until the
    # backlog clears, instead of the cap silently resetting the clock on a
    # gap that hasn't actually closed (spec §5.2 step 2/§13.8).

    _finalize_cursor(repo_path, cursor)


def _parse_args() -> argparse.Namespace:
    """Parse CLI arguments for the Gmail sync script."""
    parser = argparse.ArgumentParser(
        description="Sync Gmail lifecycle signals into job-application issues."
    )
    add_repo_path_argument(parser)
    return parser.parse_args()


def main() -> None:
    """CLI entry point: load config/secrets, then orchestrate the sync run.

    Follows spec §5.2 step 1's gating precisely: absent/disabled
    `settings.gmail` exits 0 (silent, cheap no-op for every repo that hasn't
    opted in); enabled with a missing secret is a fatal config error (exit
    1) -- opting in implies the manual OAuth setup was supposed to happen.
    """
    setup_logging()
    args = _parse_args()
    repo_path = resolve_repo_path(args.repo_path)

    try:
        settings = load_settings(repo_path / "config/settings.yaml")
    except Exception as e:
        logger.error("Failed to load settings configuration: %s", e)
        sys.exit(1)

    gmail_config = settings.gmail
    if gmail_config is None or not gmail_config.enabled:
        logger.info("Gmail integration not configured or disabled; exiting.")
        sys.exit(0)

    missing_secrets = _missing_gmail_secrets()
    if missing_secrets:
        logger.error(
            "Gmail integration is enabled but missing required secret(s): %s",
            ", ".join(missing_secrets),
        )
        sys.exit(1)

    try:
        resume = load_resume(repo_path / "resumes/resume.yaml")
    except Exception as e:
        logger.error("Failed to load base resume configuration: %s", e)
        sys.exit(1)

    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token:
        logger.error("GITHUB_TOKEN environment variable is missing.")
        sys.exit(1)
    if not repo:
        logger.error("GITHUB_REPOSITORY environment variable is missing.")
        sys.exit(1)

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
        sys.exit(1)

    gmail_client_id = os.environ["GMAIL_CLIENT_ID"]
    gmail_client_secret = os.environ["GMAIL_CLIENT_SECRET"]
    gmail_refresh_token = os.environ["GMAIL_REFRESH_TOKEN"]
    try:
        gmail_client = GmailClient(
            client_id=gmail_client_id,
            client_secret=gmail_client_secret,
            refresh_token=gmail_refresh_token,
        )
    except Exception as e:
        # git_ops.redact_sensitive_string only knows GitHub token shapes;
        # Google's OAuth secrets have no such pattern, so an underlying
        # google-auth/googleapiclient error that happens to echo a request
        # parameter is scrubbed here explicitly (spec §9.5: these secrets
        # are never logged).
        logger.error(
            "Failed to initialize Gmail client: %s",
            _redact_gmail_secrets(
                str(e), gmail_client_id, gmail_client_secret, gmail_refresh_token
            ),
        )
        sys.exit(1)

    try:
        # research.model overrides the provider default, mirroring
        # respond.py's own precedent (spec 8.1).
        model_override = getattr(settings.research, "model", "") or None
        llm_client = get_llm_client(model=model_override)
    except Exception as e:
        logger.error("Failed to initialize LLM client: %s", e)
        sys.exit(1)

    try:
        run_sync(
            repo_path=repo_path,
            gmail_config=gmail_config,
            gh_client=gh_client,
            gmail_client=gmail_client,
            llm_client=llm_client,
            settings=settings,
            resume=resume,
        )
    except GmailSyncFatalError as e:
        logger.error(str(e))
        sys.exit(1)
    except QuotaExceededError as e:
        logger.warning("LLM API quota exceeded: %s", e)
        sys.exit(EXIT_QUOTA_EXCEEDED)
    except Exception:
        logger.exception("Gmail sync run failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
