"""Job scraper bot CLI entry point for JobGitOps.

Imports run_scraper from jobgitops.scraper and executes it with parsed arguments.
"""

import argparse
import logging
import os
import sys

from jobgitops.scraper import run_scraper


def main() -> None:
    """Parse command line arguments and launch the job scraper bot."""
    # Configure logging only when executed as the main script
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    parser = argparse.ArgumentParser(description="Job scraper bot CLI entry point.")
    parser.add_argument(
        "--settings",
        "-s",
        type=str,
        default="config/settings.yaml",
        help="Path to settings YAML file.",
    )
    parser.add_argument(
        "--resume",
        "-r",
        type=str,
        default="resumes/resume.yaml",
        help="Path to resume YAML file.",
    )
    parser.add_argument(
        "--dry-run",
        "-d",
        action="store_true",
        help="Skip writing to remote GitHub APIs.",
    )
    parser.add_argument(
        "--work-preference",
        "-w",
        type=str,
        default=None,
        help="Override job search work preference settings (remote, onsite, hybrid).",
    )
    parser.add_argument(
        "--job-type",
        "-t",
        type=str,
        default=None,
        help="Override job search type settings.",
    )
    parser.add_argument(
        "--hours-old",
        "-o",
        type=int,
        default=None,
        help="Override job search hours old settings.",
    )

    args = parser.parse_args()

    # Check environment variable truthiness safely
    is_dry = args.dry_run or os.environ.get("DRY_RUN", "").lower() in (
        "1",
        "true",
        "yes",
    )

    try:
        run_scraper(
            settings_path=args.settings,
            resume_path=args.resume,
            dry_run=is_dry,
            work_preference_override=args.work_preference,
            job_type_override=args.job_type,
            hours_old_override=args.hours_old,
        )
    except Exception as e:
        logger = logging.getLogger("scrape")
        logger.error("Scraper execution failed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
