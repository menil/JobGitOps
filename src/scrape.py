"""Job scraper bot CLI entry point for JobGitOps.

Imports run_scraper from jobgitops.scraper and executes it with parsed arguments.
"""

import logging
import os
import sys

from jobgitops.scraper import run_scraper

if __name__ == "__main__":
    # Configure logging only when executed as the main script
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    is_dry = "DRY_RUN" in os.environ or "--dry-run" in sys.argv or "-d" in sys.argv
    run_scraper(dry_run=is_dry)
