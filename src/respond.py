"""Issue Assistant responder: webhook handler for issue comments and opened issues.

Responds to ``issue_comment`` (created) and ``issues`` (opened) webhook events
(spec 4.1):

- **Comment flow:** skips bot authors, empty comments, and the assistant's own
  confirmation marker; loads the issue thread context; runs the agent loop
  (``assistant.run_agent``); executes the returned side effect (reply comment,
  status label + marker-prefixed confirmation, triage, or nothing).
- **Opened-issue flow:** auto-detects bare job-URL submissions, fetches the
  posting, builds the canonical job body, and runs the shared triage core
  directly. Issues already labeled ``triage-pending`` are left to the
  ``triage-issue.yml`` webhook.

Projects V2 column moves are never performed here: the ``status_update`` action
only adds the label, and ``status-transition.yml`` owns the column (§4.3).
"""

import argparse
import json
import logging
import os
import pathlib
import re
import sys
from typing import Any

import triage
from jobgitops.assistant import (
    ACTION_REPLY,
    ACTION_SKIP,
    ACTION_STATUS_UPDATE,
    ACTION_TRIAGE,
    STATUS_CONFIRMATION_MARKER,
    STATUS_LABELS,
    AgentAction,
    run_agent,
)
from jobgitops.cli import add_repo_path_argument, resolve_repo_path, setup_logging
from jobgitops.github_client import GitHubClient, GitHubClientError, extract_label_names
from jobgitops.llm import QuotaExceededError, get_llm_client
from jobgitops.loader import load_resume, load_settings
from jobgitops.status_model import LIFECYCLE_LABELS, sync_lifecycle_label
from jobgitops.web import WebClient

logger = logging.getLogger("jobgitops.respond")

# POSIX exit code for temporary quota/rate-limit failure (EX_TEMPFAIL).
EXIT_QUOTA_EXCEEDED = 75

# Job pipeline labels. An issue carrying any of these is already owned by the
# scraper/triage/status machinery and must not be re-processed by the
# auto-detect guard (spec 4.1). Lifecycle labels are imported from the single
# source of truth (status_model) so the sets can never drift apart.
JOB_LABELS = LIFECYCLE_LABELS | frozenset({"fit:A+", "fit:A", "fit:B"})

BARE_URL_REGEX = re.compile(r"https?://[^\s]+")

# Markers that identify a scraper-created (already structured) issue body.
_STRUCTURED_BODY_REGEX = re.compile(
    r"(##\s*Job\s*Description|\*\*[Cc]ompany:\*\*|\*\*[Rr]ole:\*\*|\*\*Apply\s*[Uu][Rr][Ll]:\*\*)",
    re.IGNORECASE,
)


class RespondError(Exception):
    """Raised when the webhook event cannot be handled by the responder."""


def _to_int(value: Any) -> int | None:
    """Coerce a webhook scalar (often a JSON string) to an int."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bot_logins_from_env() -> set[str]:
    """Parse the AGENT_BOT_LOGINS blocklist env var into a lowercased set."""
    raw = os.environ.get("AGENT_BOT_LOGINS", "")
    return {login.strip().lower() for login in raw.split(",") if login.strip()}


def classify_event(event: dict[str, Any]) -> str:
    """Classify the webhook event as the comment flow or the opened-issue flow.

    ``issue_comment`` events carry a ``comment`` object; ``issues`` events do
    not. The action is also checked so unrelated events that share a payload
    shape (e.g. an edited comment) are rejected rather than acted on.

    Returns:
        ``"comment"`` for ``issue_comment`` created, ``"opened"`` for
        ``issues`` opened.

    Raises:
        RespondError: When the event is not a supported shape for the
            responder.
    """
    if event.get("action") == "created" and "comment" in event:
        return "comment"
    if event.get("action") == "opened" and event.get("issue"):
        return "opened"
    raise RespondError("Unsupported webhook event for the responder.")


def is_bot_author(author: dict[str, Any] | None, bot_logins: set[str]) -> bool:
    """Return True when a comment author is a bot and should be skipped.

    Skips GitHub app/bot accounts (``user.type == "Bot"``) and any login in the
    ``AGENT_BOT_LOGINS`` blocklist — the self-reply guard (spec 9.1): the
    assistant posts as ``github-actions[bot]`` and must never reply to itself.

    Args:
        author: The comment ``user`` object from the webhook event.
        bot_logins: Lowercased bot-login blocklist (see ``_bot_logins_from_env``).
    """
    user = author or {}
    if user.get("type") == "Bot":
        return True
    return (user.get("login") or "").strip().lower() in bot_logins


def contains_confirmation_marker(comment_body: str | None) -> bool:
    """Return True when a comment carries the status-update confirmation marker.

    The marker prefixes the assistant's own confirmations, so matching on it is
    the deterministic re-trigger guard (spec 6.2/9.3): a confirmation that slips
    through the bot guard is still skipped.
    """
    return bool(comment_body) and STATUS_CONFIRMATION_MARKER in comment_body


def _extract_url(body: str | None) -> str | None:
    """Return the first http(s) URL in a body, or None."""
    match = BARE_URL_REGEX.search(body or "")
    if not match:
        return None
    return match.group(0).rstrip(".,;") or None


def should_auto_detect(issue_body: str | None, labels: list[str] | None) -> bool:
    """Apply the opened-issue auto-detect guard (spec 4.1).

    Returns True only when the body contains an http(s) URL, the issue carries
    no job labels, and the body does not already parse into structured job
    details (i.e. it is a bare URL submission, not a scraper-created issue).

    Args:
        issue_body: The issue body.
        labels: Label names currently on the issue.
    """
    if not _extract_url(issue_body):
        return False
    if any(label in JOB_LABELS for label in (labels or [])):
        return False
    return not _STRUCTURED_BODY_REGEX.search(issue_body or "")


def _confirmation_comment(reply: str) -> str:
    """Prefix a status-update confirmation with the hidden re-trigger marker."""
    if reply.strip():
        return f"{STATUS_CONFIRMATION_MARKER}\n\n{reply.strip()}"
    return STATUS_CONFIRMATION_MARKER


def execute_action(
    action: AgentAction,
    *,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    issue_node_id: str | None,
    repo_path: pathlib.Path,
    gh_client: GitHubClient,
    settings: Any,
    resume: Any,
    llm_client: Any,
    web_client: Any,
) -> None:
    """Execute the side effects of an agent action (spec 7).

    The column move for ``status_update`` is deliberately not performed here:
    the label-add emits ``issues: labeled``, which triggers
    ``status-transition.yml`` — the single owner of Projects V2 (§4.3).
    """
    if action.action == ACTION_SKIP:
        return

    if action.action == ACTION_REPLY:
        if action.reply.strip():
            gh_client.post_comment(issue_number, action.reply.strip())
        return

    if action.action == ACTION_STATUS_UPDATE:
        label = STATUS_LABELS[action.status]
        sync_lifecycle_label(gh_client, issue_number, label)
        if issue_node_id and settings.projects_v2 and settings.projects_v2.project_id:
            try:
                gh_client.update_project_status(issue_node_id, action.status)
                logger.info(
                    "Directly updated Projects V2 status to %s for issue #%d.",
                    action.status,
                    issue_number,
                )
            except GitHubClientError as e:
                logger.warning(
                    "Could not update Projects V2 status directly for issue #%d: %s",
                    issue_number,
                    e,
                )
        gh_client.post_comment(issue_number, _confirmation_comment(action.reply))
        return

    if action.action == ACTION_TRIAGE:
        current_labels = gh_client.get_labels(issue_number)
        if "triage-pending" in current_labels:
            logger.info(
                "Issue #%d is labeled triage-pending; the triage webhook owns it.",
                issue_number,
            )
            return
        triage.run_triage(
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            issue_node_id=issue_node_id,
            issue_labels=current_labels,
            repo_path=repo_path,
            gh_client=gh_client,
            settings=settings,
            resume=resume,
            llm_client=llm_client,
            web_client=web_client,
        )
        return

    logger.warning("Unknown action %r; doing nothing.", action.action)


def handle_comment_event(
    event: dict[str, Any],
    *,
    gh_client: GitHubClient,
    web_client: Any,
    llm_client: Any,
    settings: Any,
    resume: Any,
    repo_path: pathlib.Path,
    bot_logins: set[str],
) -> None:
    """Process an ``issue_comment`` created event (spec 4.1 comment job).

    Guards run before any work: bot authors, empty comments, and the assistant's
    own confirmation-marker comments are skipped. Otherwise the thread context is
    loaded (fresh labels via ``get_labels``, comments via ``list_comments``) and
    the agent loop is run with the triggering comment as the initial user turn.
    """
    comment = event.get("comment") or {}
    comment_body = comment.get("body", "") or ""
    author = comment.get("user") or {}

    if is_bot_author(author, bot_logins):
        logger.info("Skipping comment from bot author %r.", author.get("login"))
        return
    if not comment_body.strip():
        logger.info("Skipping empty comment.")
        return
    if contains_confirmation_marker(comment_body):
        logger.info("Skipping assistant confirmation-marker comment.")
        return

    issue = event.get("issue") or {}
    issue_number = _to_int(issue.get("number"))
    if not issue_number:
        raise RespondError("Comment event carries no issue number.")

    issue_title = issue.get("title", "") or ""
    issue_body = issue.get("body", "") or ""
    issue_node_id = issue.get("node_id")
    labels = gh_client.get_labels(issue_number)
    comments = [
        c.get("body", "") or ""
        for c in gh_client.list_comments(issue_number)
        if isinstance(c, dict)
    ]

    action = run_agent(
        llm_client,
        web_client,
        settings.research,
        issue_title=issue_title,
        issue_body=issue_body,
        labels=labels,
        trigger_text=comment_body,
        comments=comments,
        resume=resume,
    )
    logger.info("Agent chose action %r for issue #%d.", action.action, issue_number)
    execute_action(
        action,
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=issue_body,
        issue_node_id=issue_node_id,
        repo_path=repo_path,
        gh_client=gh_client,
        settings=settings,
        resume=resume,
        llm_client=llm_client,
        web_client=web_client,
    )


def handle_opened_event(
    event: dict[str, Any],
    *,
    gh_client: GitHubClient,
    web_client: Any,
    llm_client: Any,
    settings: Any,
    resume: Any,
    repo_path: pathlib.Path,
) -> None:
    """Process an ``issues`` opened event (spec 4.1 opened-issue job).

    Issues labeled ``triage-pending`` are left to the triage webhook. Bare
    job-URL submissions that pass the auto-detect guard are fetched, canonicalized
    into the shared body layout (spec 5.5), and run through the shared triage
    core directly — without ever adding ``triage-pending``, which would
    double-process the issue.
    """
    issue = event.get("issue") or {}
    issue_number = _to_int(issue.get("number"))
    if not issue_number:
        raise RespondError("Opened-issue event carries no issue number.")

    issue_title = issue.get("title", "") or ""
    issue_body = issue.get("body", "") or ""
    issue_node_id = issue.get("node_id")
    labels = extract_label_names(issue.get("labels", []))

    if "triage-pending" in labels:
        logger.info(
            "Issue #%d is labeled triage-pending; the triage webhook owns it.",
            issue_number,
        )
        return
    if not should_auto_detect(issue_body, labels):
        logger.info(
            "Issue #%d does not match the auto-detect guard; skipping.", issue_number
        )
        return

    url = _extract_url(issue_body) or ""
    try:
        page = triage.fetch_job_page(url, web_client)
    except triage.JobFetchError as e:
        logger.warning("Could not fetch job posting for issue #%d: %s", issue_number, e)
        triage.post_fetch_failure_comment(issue_number, gh_client, url, str(e))
        return

    job_details = triage.infer_job_details_from_page(
        page_text=page["description"],
        page_title=page["title"],
        url=url,
        llm_client=llm_client,
    )
    canonical_body = triage.build_canonical_body(
        company=job_details["company"],
        role=job_details["role"],
        location=job_details["location"],
        salary=job_details["salary"],
        url=url,
        description=job_details["description"],
    )

    logger.info(
        "Auto-detected job URL for issue #%d; running the shared triage core.",
        issue_number,
    )
    triage.run_triage(
        issue_number=issue_number,
        issue_title=issue_title,
        issue_body=canonical_body,
        issue_node_id=issue_node_id,
        issue_labels=labels,
        repo_path=repo_path,
        gh_client=gh_client,
        settings=settings,
        resume=resume,
        llm_client=llm_client,
        web_client=web_client,
    )


def _post_diagnostic_comment(
    gh_client: GitHubClient, event: dict[str, Any], error: Exception
) -> None:
    """Post a diagnostic comment for an unexpected failure (spec 5.1/9.4)."""
    issue = event.get("issue") or {}
    issue_number = _to_int(issue.get("number"))
    if not issue_number:
        return
    body = (
        "### Issue Assistant: Error\n\n"
        "I hit an error while processing this thread:\n"
        f"```\n{error}\n```\n"
        "Please check the workflow run log for details."
    )
    try:
        gh_client.post_comment(issue_number, body)
    except Exception as e:
        logger.error(
            "Failed to post diagnostic comment to issue #%d: %s", issue_number, e
        )


def _parse_args() -> argparse.Namespace:
    """Parse CLI arguments for the responder script."""
    parser = argparse.ArgumentParser(
        description="Respond to issue comments and opened job-URL issues."
    )
    parser.add_argument(
        "--event-path",
        type=str,
        help="Path to GitHub webhook event JSON file (e.g. GITHUB_EVENT_PATH).",
    )
    add_repo_path_argument(parser)
    return parser.parse_args()


def main() -> None:
    """CLI entry point: classify the event, dispatch, and execute side effects."""
    setup_logging()

    args = _parse_args()
    repo_path = resolve_repo_path(args.repo_path)

    # Load configurations and base resume once (spec 5.1).
    try:
        settings = load_settings(repo_path / "config/settings.yaml")
        resume = load_resume(repo_path / "resumes/resume.yaml")
    except Exception as e:
        logger.error("Failed to load settings or base resume configuration: %s", e)
        sys.exit(1)

    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")

    event_path = args.event_path or os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        logger.error("No event payload specified (--event-path or GITHUB_EVENT_PATH).")
        sys.exit(1)

    try:
        with pathlib.Path(event_path).open("r", encoding="utf-8") as f:
            event = json.load(f)
    except Exception as e:
        logger.error("Failed to parse event payload JSON: %s", e)
        sys.exit(1)

    # Repository can be resolved from the payload if the env var is missing.
    if not repo and isinstance(event.get("repository"), dict):
        repo = event["repository"].get("full_name")

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

    try:
        event_kind = classify_event(event)
        # research.model overrides the provider default for the responder only;
        # triage/tailor keep GEMINI_MODEL / OPENROUTER_MODEL (spec 8.1).
        model_override = getattr(settings.research, "model", "") or None
        llm_client = get_llm_client(model=model_override)
        web_client = WebClient(settings.research)

        if event_kind == "comment":
            handle_comment_event(
                event,
                gh_client=gh_client,
                web_client=web_client,
                llm_client=llm_client,
                settings=settings,
                resume=resume,
                repo_path=repo_path,
                bot_logins=_bot_logins_from_env(),
            )
        else:
            handle_opened_event(
                event,
                gh_client=gh_client,
                web_client=web_client,
                llm_client=llm_client,
                settings=settings,
                resume=resume,
                repo_path=repo_path,
            )
    except RespondError as e:
        logger.info("Ignoring event: %s", e)
        sys.exit(0)
    except QuotaExceededError as e:
        logger.warning("LLM API quota exceeded: %s", e)
        sys.exit(EXIT_QUOTA_EXCEEDED)
    except Exception as e:
        logger.exception("Responder failed: %s", e)
        _post_diagnostic_comment(gh_client, event, e)
        sys.exit(1)


if __name__ == "__main__":
    main()
