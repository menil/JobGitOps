"""Update the Projects V2 status column of an issue from a lifecycle label."""

import argparse
import dataclasses
import json
import logging
import os
import pathlib
import sys
from typing import Any

from jobgitops.cli import add_repo_path_argument, resolve_repo_path, setup_logging
from jobgitops.github_client import GitHubClient
from jobgitops.loader import load_settings
from jobgitops.schema import ProjectsV2Config
from jobgitops.status_model import (
    LABEL_TO_STATUS,
    resolve_closed_lifecycle_label,
    sync_lifecycle_label,
)

logger = logging.getLogger("status_transition")


@dataclasses.dataclass
class TransitionContext:
    """Resolved inputs needed to move an issue's Projects V2 column."""

    token: str
    repo: str
    issue_number: int
    issue_node_id: str | None
    label: str
    status: str


def main() -> None:
    """CLI entry point for label-driven status transitions.

    Supports resolution of the issue number and node ID via explicit CLI
    arguments (--issue) or directly from the GitHub Actions webhook event
    payload (--event-path / GITHUB_EVENT_PATH). The lifecycle label is read
    from --label or github.event.label.name.
    """
    # Configure logging
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

    event = _load_event_payload(args)
    label = _resolve_label(event, args.label)

    # No-op cleanly when Projects V2 is unconfigured (label-only fallback).
    # The label is validated first so unsupported input fails loudly instead
    # of being silently swallowed by this early exit.
    if not settings.projects_v2 or not settings.projects_v2.project_id:
        logger.warning(
            "Projects V2 is not configured in config/settings.yaml. "
            "Skipping status transition."
        )
        sys.exit(0)

    # Resolve the transition inputs, then apply the Projects V2 column move.
    context = _resolve_context(args, event, label)
    gh_client = _init_client(settings.projects_v2, context)
    _transition(gh_client, context)


def _parse_args() -> argparse.Namespace:
    """Parse CLI arguments for the status transition script."""
    parser = argparse.ArgumentParser(
        description="Transition issue status in Projects V2 from a lifecycle label."
    )
    parser.add_argument(
        "--issue", "-i", type=int, help="GitHub issue number to update."
    )
    parser.add_argument(
        "--label",
        type=str,
        help="Lifecycle label that was added (e.g. applied, in-loop, rejected).",
    )
    parser.add_argument(
        "--event-path",
        type=str,
        help="Path to GitHub webhook event JSON file (e.g. GITHUB_EVENT_PATH).",
    )
    add_repo_path_argument(parser)
    return parser.parse_args()


def _load_event_payload(args: argparse.Namespace) -> dict[str, Any]:
    """Load and parse the webhook event payload, if one was provided.

    Returns an empty dict when no event path is configured. Exits with a
    non-zero status when the payload cannot be parsed as JSON.
    """
    event_path = args.event_path or os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        return {}

    logger.info("Loading GitHub event payload from: %s", event_path)
    try:
        with pathlib.Path(event_path).open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("Failed to parse event payload JSON: %s", e)
        sys.exit(1)


def _resolve_label(event: dict[str, Any], cli_label: str | None) -> str:
    """Resolve the lifecycle label from --label or the webhook payload.

    Validates against the known lifecycle set so unsupported input fails
    loudly before the Projects V2 no-op path can swallow it.
    """
    label = cli_label
    if not label:
        if event.get("action") == "closed":
            issue_data = event.get("issue") or {}
            labels_list = issue_data.get("labels") or []
            current_labels = {
                label_dict.get("name")
                for label_dict in labels_list
                if isinstance(label_dict, dict) and label_dict.get("name")
            }
            label = resolve_closed_lifecycle_label(current_labels)
        elif isinstance(event.get("label"), dict):
            label = event["label"].get("name")

    if not label or label not in LABEL_TO_STATUS:
        logger.error(
            "Unsupported or missing label %r. Expected one of: %s.",
            label,
            ", ".join(sorted(LABEL_TO_STATUS)),
        )
        sys.exit(1)
    return label


def _resolve_context(
    args: argparse.Namespace, event: dict[str, Any], label: str
) -> TransitionContext:
    """Resolve token, repository, and issue details from CLI args and payload.

    Exits with a non-zero status when any required input is missing or invalid.
    """
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")

    issue_number = args.issue
    issue_node_id: str | None = None

    # Enrich from the GitHub webhook event payload when available.
    issue_data = event.get("issue", {})
    if not issue_number:
        issue_number = issue_data.get("number")
    issue_node_id = issue_data.get("node_id")

    # Repository can be resolved from payload repository full_name if the
    # env var is missing.
    if not repo and isinstance(event.get("repository"), dict):
        repo = event["repository"].get("full_name")

    # Payload numbers are JSON scalars and can arrive as strings; coerce them
    # to the int type the rest of the pipeline expects.
    if issue_number is not None:
        try:
            issue_number = int(issue_number)
        except (TypeError, ValueError):
            issue_number = None

    if not token:
        logger.error("GITHUB_TOKEN environment variable is missing.")
        sys.exit(1)

    if not repo:
        logger.error("GITHUB_REPOSITORY environment variable is missing.")
        sys.exit(1)

    if not issue_number:
        logger.error("No issue number or event payload specified.")
        sys.exit(1)

    return TransitionContext(
        token=token,
        repo=repo,
        issue_number=issue_number,
        issue_node_id=issue_node_id,
        label=label,
        status=LABEL_TO_STATUS[label],
    )


def _init_client(
    projects_config: ProjectsV2Config, context: TransitionContext
) -> GitHubClient:
    """Initialize the GitHub client for the Projects V2 column move."""
    status_field = projects_config.status_field_name or "Status"
    try:
        return GitHubClient(
            token=context.token,
            repo=context.repo,
            project_id=projects_config.project_id,
            status_field_name=status_field,
        )
    except Exception as e:
        logger.error("Failed to initialize GitHubClient: %s", e)
        sys.exit(1)


def _transition(gh_client: GitHubClient, context: TransitionContext) -> None:
    """Move the issue's Projects V2 column to its resolved status."""
    issue_node_id = _resolve_node_id(gh_client, context)
    logger.info(
        "Transitioning issue #%d (Node ID: %s) status to %s...",
        context.issue_number,
        issue_node_id,
        context.status,
    )
    try:
        # The lifecycle label that triggered this workflow is the source of
        # truth; drop stale sibling lifecycle labels so the issue carries a
        # single pipeline stage and the board matches the label state.
        sync_lifecycle_label(gh_client, context.issue_number, context.label)
        gh_client.update_project_status(issue_node_id, context.status)
        logger.info("Successfully updated status to %s.", context.status)
    except Exception as e:
        logger.error("Failed to update Projects V2 status: %s", e)
        sys.exit(1)


def _resolve_node_id(gh_client: GitHubClient, context: TransitionContext) -> str:
    """Resolve the issue node ID, fetching it from GitHub when the payload lacks it."""
    if context.issue_node_id:
        return context.issue_node_id

    logger.info(
        "Fetching issue details for #%d to retrieve node_id...",
        context.issue_number,
    )
    try:
        issue = gh_client.get_issue(context.issue_number)
    except Exception as e:
        logger.error("Failed to fetch issue #%d: %s", context.issue_number, e)
        sys.exit(1)

    if not issue.get("node_id"):
        logger.error("Failed to resolve issue node ID for #%d.", context.issue_number)
        sys.exit(1)

    return issue["node_id"]


if __name__ == "__main__":
    main()
