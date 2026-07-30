"""Script to update Project V2 status of an issue to 'Applied' when labeled."""

import argparse
import json
import logging
import os
import pathlib
import sys

from jobgitops.github_client import GitHubClient
from jobgitops.loader import load_settings

logger = logging.getLogger("applied_transition")


def main() -> None:
    """CLI entry point for applied status transition.

    Supports resolution of issue number and node ID via explicit CLI argument (--issue)
    or directly from GitHub Actions webhook event payload
    (--event-path / GITHUB_EVENT_PATH).
    """
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    parser = argparse.ArgumentParser(
        description="Transition issue status in Projects V2 to 'Applied'."
    )
    parser.add_argument(
        "--issue", "-i", type=int, help="GitHub issue number to update."
    )
    parser.add_argument(
        "--event-path",
        type=str,
        help="Path to GitHub webhook event JSON file (e.g. GITHUB_EVENT_PATH).",
    )
    parser.add_argument(
        "--repo-path",
        type=str,
        default=".",
        help="Path to the local git repository (defaults to '.').",
    )
    args = parser.parse_args()

    repo_path = pathlib.Path(args.repo_path).resolve()

    # 1. Load configurations
    try:
        settings = load_settings(repo_path / "config/settings.yaml")
    except Exception as e:
        logger.error("Failed to load settings configuration: %s", e)
        sys.exit(1)

    # 2. Check if Project V2 is configured
    if not settings.projects_v2 or not settings.projects_v2.project_id:
        logger.warning(
            "Projects V2 is not configured in config/settings.yaml. "
            "Skipping status transition."
        )
        sys.exit(0)

    # 3. Load environmental tokens and details
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    event_path = args.event_path or os.environ.get("GITHUB_EVENT_PATH")

    issue_number = args.issue
    issue_node_id = None

    # 4. Read from event payload if available
    if event_path:
        logger.info("Loading GitHub event payload from: %s", event_path)
        try:
            with pathlib.Path(event_path).open("r", encoding="utf-8") as f:
                event = json.load(f)
            issue_data = event.get("issue", {})
            if not issue_number:
                issue_number = issue_data.get("number")
            issue_node_id = issue_data.get("node_id")

            # Repository can be resolved from payload repository full_name
            # if env is missing
            if not repo and isinstance(event.get("repository"), dict):
                repo = event["repository"].get("full_name")
        except Exception as e:
            logger.error("Failed to parse event payload JSON: %s", e)
            sys.exit(1)

    if not token:
        logger.error("GITHUB_TOKEN environment variable is missing.")
        sys.exit(1)

    if not repo:
        logger.error("GITHUB_REPOSITORY environment variable is missing.")
        sys.exit(1)

    if not issue_number:
        logger.error("No issue number or event payload specified.")
        sys.exit(1)

    # 5. Initialize GitHub client
    project_id = settings.projects_v2.project_id
    status_field = settings.projects_v2.status_field_name or "Status"

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

    # 6. Retrieve node_id if not present in the payload (by querying GitHub API)
    if not issue_node_id:
        logger.info(
            "Fetching issue details for #%d to retrieve node_id...",
            issue_number,
        )
        try:
            issue = gh_client.get_issue(issue_number)
            issue_node_id = issue.get("node_id")
        except Exception as e:
            logger.error("Failed to fetch issue #%d: %s", issue_number, e)
            sys.exit(1)

    if not issue_node_id:
        logger.error("Failed to resolve issue node ID for #%d.", issue_number)
        sys.exit(1)

    # 7. Transition status to 'Applied'
    logger.info(
        "Transitioning issue #%d (Node ID: %s) status to 'Applied'...",
        issue_number,
        issue_node_id,
    )
    try:
        gh_client.update_project_status(issue_node_id, "Applied")
        logger.info("Successfully updated status to 'Applied'.")
    except Exception as e:
        logger.error("Failed to update Projects V2 status: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
