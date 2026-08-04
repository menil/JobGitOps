"""Two-way sync between issue labels and the Projects V2 status column.

The forward direction (label -> column) lives in ``status_transition.py`` and
is triggered by the label workflow. This script owns the reverse direction
(column -> label), the idempotent board backfill, and the Status field option
sync, so the board and the pipeline labels can never drift apart.
"""

import argparse
import json
import logging
import os
import pathlib
import sys
from typing import Any

from jobgitops.cli import add_repo_path_argument, resolve_repo_path, setup_logging
from jobgitops.github_client import GitHubClient, extract_label_names
from jobgitops.loader import load_settings
from jobgitops.status_model import (
    LABEL_TO_STATUS,
    LIFECYCLE_LABELS,
    REVERSE_SYNC_STATUSES,
    STATUS_TO_LABEL,
    resolve_closed_lifecycle_label,
    sync_lifecycle_label,
)

logger = logging.getLogger("project_sync")

BACKFILL_PAGE_SIZE = 100


def main() -> None:
    """CLI entry point for project-label synchronization."""
    setup_logging()

    args = _parse_args()

    # Load configurations
    try:
        settings = load_settings(
            resolve_repo_path(args.repo_path) / "config/settings.yaml"
        )
    except Exception as e:
        logger.error("Failed to load settings configuration: %s", e)
        sys.exit(1)

    # No-op cleanly when Projects V2 is unconfigured.
    if not settings.projects_v2 or not settings.projects_v2.project_id:
        logger.warning(
            "Projects V2 is not configured in config/settings.yaml. Exiting."
        )
        sys.exit(0)

    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token:
        logger.error("GITHUB_TOKEN environment variable is missing.")
        sys.exit(1)
    if not repo:
        logger.error("GITHUB_REPOSITORY environment variable is missing.")
        sys.exit(1)

    status_field_name = settings.projects_v2.status_field_name or "Status"
    gh_client = GitHubClient(
        token=token,
        repo=repo,
        project_id=settings.projects_v2.project_id,
        status_field_name=status_field_name,
    )

    if args.command == "event":
        _run_event(args, gh_client, status_field_name)
    elif args.command == "backfill":
        _run_backfill(gh_client, args.reverse)
    elif args.command == "field-options":
        _run_field_options(gh_client, args.prune)


def _parse_args() -> argparse.Namespace:
    """Parse CLI arguments for the project-label sync script."""
    parser = argparse.ArgumentParser(
        description="Sync issue labels with the Projects V2 status column."
    )
    add_repo_path_argument(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    event_parser = subparsers.add_parser(
        "event",
        help="Reverse-sync a column move back to its lifecycle label.",
    )
    event_parser.add_argument(
        "--event-path",
        type=str,
        help="Path to GitHub webhook event JSON file (e.g. GITHUB_EVENT_PATH).",
    )
    backfill_parser = subparsers.add_parser(
        "backfill",
        help="Populate the board from existing issue labels (idempotent).",
    )
    backfill_parser.add_argument(
        "--reverse",
        action="store_true",
        help=(
            "Reconcile the board -> label direction before populating: issues "
            "whose card sits on a lifecycle column that does not match their "
            "labels get the matching label applied (recovers dropped event "
            "syncs). Triage Pending is excluded, matching the event handler."
        ),
    )
    field_parser = subparsers.add_parser(
        "field-options",
        help="Sync Status field options with the lifecycle model.",
    )
    field_parser.add_argument(
        "--prune",
        action="store_true",
        help=(
            "Remove options absent from the lifecycle model. Run only after "
            "items have been moved onto the new options, since GitHub rejects "
            "removing options that are still in use."
        ),
    )

    return parser.parse_args()


def _load_event_payload(args: argparse.Namespace) -> dict[str, Any]:
    """Load and parse the webhook event payload, if one was provided.

    Exits with a non-zero status when no payload exists or it cannot be parsed.
    """
    event_path = args.event_path or os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        logger.error("No event path specified for the 'event' command.")
        sys.exit(1)

    logger.info("Loading GitHub event payload from: %s", event_path)
    try:
        with pathlib.Path(event_path).open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("Failed to parse event payload JSON: %s", e)
        sys.exit(1)


def _run_event(
    args: argparse.Namespace,
    gh_client: GitHubClient,
    status_field_name: str,
) -> None:
    """Reverse-sync a board column change back to the matching label.

    Reacts only to Issue items on the configured project whose status moved to
    one of the ``REVERSE_SYNC_STATUSES`` (Triage Pending is deliberately
    excluded so dragging a card back does not re-trigger an AI re-triage).
    """
    event = _load_event_payload(args)
    item = event.get("projects_v2_item") or {}

    if item.get("content_type") != "Issue":
        logger.info(
            "Ignoring non-issue project item (content_type=%r).",
            item.get("content_type"),
        )
        return

    project_node_id = item.get("project_node_id")
    if project_node_id != gh_client.project_id:
        logger.info(
            "Ignoring event for an unconfigured project (project_node_id=%r).",
            project_node_id,
        )
        return

    status = _changed_status(event, item, status_field_name)
    if not isinstance(status, str) or status not in REVERSE_SYNC_STATUSES:
        logger.info(
            "No actionable status change (status=%r); skipping reverse sync.",
            status,
        )
        return

    content_node_id = item.get("content_node_id")
    if not content_node_id:
        logger.error("Event payload is missing content_node_id.")
        sys.exit(1)

    try:
        issue_number = gh_client.resolve_issue_number(content_node_id)
    except Exception as e:
        logger.error("Failed to resolve issue for node %s: %s", content_node_id, e)
        sys.exit(1)

    target_label = STATUS_TO_LABEL[status]
    try:
        current_labels = set(gh_client.get_labels(issue_number))
    except Exception as e:
        logger.error("Failed to read labels for issue #%d: %s", issue_number, e)
        sys.exit(1)

    if target_label in current_labels and not (
        LIFECYCLE_LABELS & current_labels - {target_label}
    ):
        logger.info(
            "Issue #%d already carries label %r and no stale siblings; nothing to do.",
            issue_number,
            target_label,
        )
        return

    logger.info(
        "Syncing column %r -> label %r for issue #%d.",
        status,
        target_label,
        issue_number,
    )
    try:
        sync_lifecycle_label(gh_client, issue_number, target_label, current_labels)
    except Exception as e:
        logger.error(
            "Failed to apply label %r to issue #%d: %s",
            target_label,
            issue_number,
            e,
        )
        sys.exit(1)


def _changed_status(
    event: dict[str, Any], item: dict[str, Any], status_field_name: str
) -> str | None:
    """Extract the status the item was moved to, if any.

    Edited events carry the delta in ``changes.field_value``; created events
    have no delta, so the current value is read from ``field_value_by_name``.
    """
    changes = event.get("changes") or {}
    field_value = changes.get("field_value") or {}
    new_status = field_value.get("to")
    if new_status is not None:
        field_name = field_value.get("field_name")
        if field_name and field_name != status_field_name:
            return None
        return new_status

    field_values = item.get("field_value_by_name") or {}
    status_field = field_values.get(status_field_name) or {}
    return status_field.get("value")


def _run_backfill(gh_client: GitHubClient, reverse: bool) -> None:
    """Populate the board from existing labels, idempotently.

    Issues are fetched once, and their current board column is looked up from a
    single ``list_project_items`` call, so cards already in the correct column
    are skipped instead of re-moved. Open issues without a lifecycle label land
    in Triage Pending; closed ones land in Rejected. Individual failures are
    logged and reported, so a single bad issue cannot abort the whole backfill.

    With ``reverse=True`` the board -> label direction is reconciled first:
    cards sitting on a lifecycle column that does not match the issue's labels
    get the matching label applied. This recovers column moves whose webhook
    event was dropped, because the subsequent forward pass then sees matching
    labels and leaves the columns untouched.
    """
    issues = _list_all_issues(gh_client)
    board_statuses = gh_client.list_project_items()
    labels_by_number = _collect_labels(issues)

    if reverse:
        _reverse_reconcile(gh_client, labels_by_number, board_statuses)

    processed = 0
    moved = 0
    failed = 0

    for issue in issues:
        processed += 1
        issue_number = issue.get("number")
        issue_node_id = issue.get("node_id")
        if not issue_number or not issue_node_id:
            continue
        labels = labels_by_number.get(int(issue_number), set())
        status = _target_status(issue_number, issue.get("state"), labels)
        if not status:
            continue
        if board_statuses.get(issue_number) == status:
            continue
        try:
            gh_client.update_project_status(issue_node_id, status)
            moved += 1
        except Exception as e:
            failed += 1
            logger.exception("Backfill failed for issue #%d: %s", issue_number, e)

    logger.info(
        "Backfill complete: %d issue(s) processed, %d moved, %d failed.",
        processed,
        moved,
        failed,
    )
    if failed:
        sys.exit(1)


def _list_all_issues(gh_client: GitHubClient) -> list[dict[str, Any]]:
    """Fetch every issue in the repository across paginated list calls.

    GitHub's Issues REST API also returns pull requests, so those are filtered
    out: a Projects V2 board tracks job applications (issues), not development
    PRs.
    """
    issues: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = gh_client.list_issues(
            state="all", per_page=BACKFILL_PAGE_SIZE, page=page
        )
        if not batch:
            break
        issues.extend(item for item in batch if "pull_request" not in item)
        if len(batch) < BACKFILL_PAGE_SIZE:
            break
        page += 1
    return issues


def _collect_labels(issues: list[dict[str, Any]]) -> dict[int, set[str]]:
    """Build a number -> label set snapshot from the fetched issues."""
    labels_by_number: dict[int, set[str]] = {}
    for issue in issues:
        number = issue.get("number")
        if number is None:
            continue
        labels_by_number[int(number)] = set(
            extract_label_names(issue.get("labels", []))
        )
    return labels_by_number


def _reverse_reconcile(
    gh_client: GitHubClient,
    labels_by_number: dict[int, set[str]],
    board_statuses: dict[int, str | None],
) -> None:
    """Make issue labels match their board column for lifecycle statuses.

    Only ``REVERSE_SYNC_STATUSES`` columns are acted on (Triage Pending is
    excluded, matching the event handler, so a card dragged back never
    re-triggers an AI re-triage). Issues whose labels already match, or that
    are not on the board, are skipped. Failures are logged per issue instead of
    aborting the run. The passed-in ``labels_by_number`` map is updated in
    place, so the forward pass that follows sees the reconciled labels instead
    of stale snapshots.
    """
    reconciled = 0
    for issue_number, status in board_statuses.items():
        if status not in REVERSE_SYNC_STATUSES:
            continue
        target_label = STATUS_TO_LABEL[status]
        current = labels_by_number.get(int(issue_number))
        if current is None or (
            target_label in current
            and not (LIFECYCLE_LABELS & current - {target_label})
        ):
            continue
        try:
            sync_lifecycle_label(gh_client, int(issue_number), target_label, current)
            labels_by_number[int(issue_number)] = (current - LIFECYCLE_LABELS) | {
                target_label
            }
            reconciled += 1
        except Exception as e:
            logger.exception(
                "Reverse reconcile failed for issue #%d: %s",
                issue_number,
                e,
            )

    logger.info("Reverse reconcile complete: %d label(s) applied.", reconciled)


def _target_status(issue_number: int, state: str, labels: set[str]) -> str | None:
    """Resolve the board status an issue should occupy, from its labels.

    Closed issues always resolve to Mismatched/Closed or Rejected. Open issues
    resolve to their lifecycle label (falling back to Triage Pending if none).
    """
    if state == "closed":
        target_label = resolve_closed_lifecycle_label(labels)
        return LABEL_TO_STATUS[target_label]

    lifecycle = sorted(label for label in labels if label in LIFECYCLE_LABELS)

    if lifecycle:
        if len(lifecycle) > 1:
            logger.warning(
                "Issue #%s carries multiple lifecycle labels: %s; choosing %s.",
                issue_number,
                ", ".join(lifecycle),
                lifecycle[0],
            )
        return LABEL_TO_STATUS[lifecycle[0]]

    return LABEL_TO_STATUS["triage-pending"]


def _run_field_options(gh_client: GitHubClient, prune: bool) -> None:
    """Align the Status field options with the lifecycle model.

    Two-phase by design: without ``--prune`` only missing options are added
    (GitHub refuses to remove in-use options), then after ``backfill`` moves
    every item onto a lifecycle column ``--prune`` drops the stale defaults
    (e.g. ``Backlog``, ``In progress``).
    """
    desired = list(LABEL_TO_STATUS.values())
    try:
        current = gh_client.get_status_field_options()

        if prune:
            removed = [name for name in current if name not in desired]
            if not removed:
                logger.info("Status options already match the lifecycle model.")
                return
            names = desired
        else:
            missing = [name for name in desired if name not in current]
            if not missing:
                logger.info("All lifecycle status options are already present.")
                return
            names = current + missing

        logger.info("Setting status field options to: %s", names)
        gh_client.update_status_field_options(names)
    except Exception as e:
        logger.error("Failed to sync status field options: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
