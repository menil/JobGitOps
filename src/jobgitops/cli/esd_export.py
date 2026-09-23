"""CLI entry point for the ESD job-search-activity export.

See specs/esd-export.md for the full design. Manually triggered only (no
scheduled cron); wired to template/.github/workflows/esd-export.yml.
"""

import argparse
import datetime as dt
import logging
import os
import sys

from jobgitops.cli import setup_logging
from jobgitops.esd_export import FORMATS, WEEKDAYS, export_rows, write_rows
from jobgitops.github_client import GitHubClient, GitHubClientError

logger = logging.getLogger("esd_export")


def main() -> None:
    """Parse arguments, run the export pipeline, and write the output file."""
    setup_logging()
    args = _parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token:
        logger.error("GITHUB_TOKEN environment variable is missing.")
        sys.exit(1)
    if not repo:
        logger.error("GITHUB_REPOSITORY environment variable is missing.")
        sys.exit(1)

    gh_client = GitHubClient(token=token, repo=repo)

    try:
        rows = export_rows(
            gh_client,
            group_by=args.group_by,
            week_start=args.week_start,
            max_per_period=args.max_per_period,
            start_date=args.start_date,
            end_date=args.end_date,
        )
    except (ValueError, GitHubClientError) as e:
        logger.error("Failed to build ESD export: %s", e)
        sys.exit(1)

    try:
        write_rows(rows, args.output, args.format)
    except (ValueError, OSError) as e:
        logger.error("Failed to write %s: %s", args.output, e)
        sys.exit(1)

    logger.info("Wrote %d row(s) to %s", len(rows), args.output)


def _parse_args() -> argparse.Namespace:
    """Parse CLI arguments for the ESD export script."""
    parser = argparse.ArgumentParser(
        description=(
            "Export job-search activity as a spreadsheet for filing with a "
            "state unemployment department."
        )
    )
    parser.add_argument(
        "--group-by",
        choices=("weekly", "monthly"),
        required=True,
        help="Reporting period granularity.",
    )
    parser.add_argument(
        "--week-start",
        choices=WEEKDAYS,
        default="monday",
        help="First day of the week (only used with --group-by weekly).",
    )
    parser.add_argument(
        "--format",
        choices=FORMATS,
        required=True,
        help="Output spreadsheet format.",
    )
    parser.add_argument(
        "--max-per-period",
        type=_parse_max_per_period,
        default=None,
        metavar="{N,unlimited}",
        help="Row cap per reporting period: a positive integer, or "
        "'unlimited' (default).",
    )
    parser.add_argument(
        "--start-date",
        type=_parse_date,
        default=None,
        help="Inclusive lower bound, YYYY-MM-DD. Defaults to unbounded.",
    )
    parser.add_argument(
        "--end-date",
        type=_parse_date,
        default=None,
        help="Inclusive upper bound, YYYY-MM-DD. Defaults to today.",
    )
    parser.add_argument(
        "--output",
        type=str,
        required=True,
        help="Output file path.",
    )
    args = parser.parse_args()

    if args.start_date and args.end_date and args.start_date > args.end_date:
        parser.error("--start-date must not be after --end-date.")

    return args


def _parse_max_per_period(value: str) -> int | None:
    """Parse --max-per-period: 'unlimited' (any case) -> None, else a positive int."""
    if value.lower() == "unlimited":
        return None
    try:
        parsed = int(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"must be a positive integer or 'unlimited', got {value!r}"
        ) from e
    if parsed <= 0:
        raise argparse.ArgumentTypeError(
            f"must be a positive integer or 'unlimited', got {value!r}"
        )
    return parsed


def _parse_date(value: str) -> dt.date:
    """Parse --start-date/--end-date as an ISO YYYY-MM-DD date."""
    try:
        return dt.date.fromisoformat(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"must be YYYY-MM-DD, got {value!r}") from e


if __name__ == "__main__":
    main()
