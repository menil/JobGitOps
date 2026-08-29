"""Job scraper module containing core logic and helper functions for JobGitOps.

Uses python-jobspy to search LinkedIn, Indeed, and ZipRecruiter for roles
based on skills/titles in resume.yaml, or custom queries in settings.yaml.
"""

import logging
import math
import os
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pandas as pd

from jobgitops import (
    GitHubClient,
    Resume,
    load_resume,
    load_settings,
)
from jobgitops.status_model import LABEL_TO_STATUS

logger = logging.getLogger("job_scraper")


HYBRID_INDICATORS = (
    "hybrid schedule",
    "hybrid model",
    "hybrid role",
    "hybrid position",
    "hybrid work",
    "hybrid setup",
    "hybrid format",
    "hybrid environment",
    "3 days/week in office",
    "3 days a week in office",
    "2 days/week in office",
    "2 days a week in office",
    "4 days/week in office",
    "4 days a week in office",
    "days/week in office",
    "days a week in office",
    "days in office",
    "in-office days",
    "hybrid",
)

REMOTE_INDICATORS = (
    "remote position",
    "remote role",
    "remote job",
    "remote work",
    "work from home",
    "work-from-home",
    "telecommute",
    "100% remote",
    "fully remote",
    "remote option",
    "remote-first",
    "remote first",
)

ONSITE_INDICATORS = (
    "onsite required",
    "on-site required",
    "must be onsite",
    "must be on-site",
    "required to be onsite",
    "required to be on-site",
    "not a remote position",
    "not a remote role",
    "not remote",
    "no remote option",
    "onsite only",
    "on-site only",
    "100% onsite",
    "100% on-site",
    "no work from home",
)


@dataclass
class ScrapedJob:
    """Data Transfer Object representing a normalized scraped job posting."""

    company: str
    title: str
    location: str
    salary: str
    source: str
    apply_url: str
    description: str


def _clean_str(val: Any) -> str | None:
    """Helper to safely clean Pandas/Python variables to string or None.

    Checks for Pandas NaNs, None, Pandas Series, and empty/whitespace-only values.
    """
    if val is None:
        return None
    # Handle case where value is a Pandas Series due to duplicate columns
    if isinstance(val, pd.Series):
        val = val.iloc[0] if not val.empty else None
    if val is None or pd.isna(val):
        return None
    s = str(val).strip()
    if not s or s.lower() in ("nan", "none", "<na>"):
        return None
    return s


def _to_float(val: Any) -> float | None:
    """Helper to safely coerce a variable to float or None."""
    if val is None:
        return None
    if isinstance(val, pd.Series):
        val = val.iloc[0] if not val.empty else None
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
        f = float(val)
        return None if math.isnan(f) or math.isinf(f) else f
    except (ValueError, TypeError):
        return None


def format_salary(
    min_amount: Any,
    max_amount: Any,
    currency: Any,
    interval: Any,
) -> str:
    """Format salary min/max amount, currency, and interval into a readable string.

    Args:
        min_amount: Minimum salary value.
        max_amount: Maximum salary value.
        currency: The currency of the salary (e.g., 'USD').
        interval: The pay interval (e.g., 'yearly', 'hourly').

    Returns:
        Formatted salary string.
    """
    min_val = _to_float(min_amount)
    max_val = _to_float(max_amount)

    if min_val is None and max_val is None:
        return "Not specified"

    curr = _clean_str(currency) or "USD"
    symbol = "$" if curr == "USD" else f"{curr} "

    parts = []
    if min_val is not None and max_val is not None:
        parts.append(f"{symbol}{min_val:,.0f} - {symbol}{max_val:,.0f}")
    elif min_val is not None:
        parts.append(f"From {symbol}{min_val:,.0f}")
    elif max_val is not None:
        parts.append(f"Up to {symbol}{max_val:,.0f}")

    interval_str = _clean_str(interval)
    if interval_str:
        parts.append(f"({interval_str})")

    return " ".join(parts)


def build_issue_body(
    company: str,
    title: str,
    location: str,
    salary: str,
    source: str,
    apply_url: str,
    description: str,
) -> str:
    """Construct a clean markdown body for the GitHub issue.

    Args:
        company: Company name.
        title: Job title.
        location: Job location.
        salary: Formatted salary string.
        source: Scraped job board source.
        apply_url: Original application URL.
        description: Job description text.

    Returns:
        Formatted markdown body.
    """
    return f"""# {title} at {company}

## Job Details
- **Company:** {company}
- **Role:** {title}
- **Location:** {location}
- **Salary:** {salary}
- **Source:** {source}
- **Apply URL:** {apply_url}

## Job Description
{description}
"""


def generate_queries(
    resume: Resume | None, settings_custom_queries: list[str] | None
) -> list[str]:
    """Generate job search queries using custom override or base resume data.

    Args:
        resume: Parsed Resume model (can be None if custom queries are set).
        settings_custom_queries: Optional list of custom queries override.

    Returns:
        List of query strings.
    """
    # 1. Custom Queries Override
    if settings_custom_queries:
        logger.info(
            "Using custom query override from settings: %s",
            settings_custom_queries,
        )
        return settings_custom_queries

    if not resume:
        raise ValueError(
            "Resume configuration is required when custom_queries are not set."
        )

    # 2. Resume-Driven Query Generation
    # Prefer applicant's most recent job title, falling back to summary label
    # if work history is empty.
    latest_title = resume.work[0].position if resume.work else resume.basics.label

    if not latest_title:
        latest_title = "Software Engineer"
    logger.info("Latest position title identified: %s", latest_title)

    # Extract top 3-5 unique skills/keywords
    # Cap at 5 unique keywords to keep total job search queries reasonable
    # and avoid triggering rate limits on job boards.
    unique_keywords = []
    for skill in resume.skills or []:
        for kw in getattr(skill, "keywords", None) or []:
            clean_kw = str(kw).strip() if kw else ""
            if clean_kw and clean_kw not in unique_keywords:
                unique_keywords.append(clean_kw)
                if len(unique_keywords) == 5:
                    break
        if len(unique_keywords) == 5:
            break

    # Build queries: [Position] [Skill]
    # Pair latest job position with individual tech skills to target niche,
    # stack-specific job postings.
    if not unique_keywords:
        logger.info("No skills found in resume, falling back to position title.")
        return [latest_title]

    queries = [f"{latest_title} {kw}" for kw in unique_keywords]
    logger.info("Auto-generated %d search queries: %s", len(queries), queries)
    return queries


def fetch_existing_jobs_cache(
    github_client: GitHubClient, max_pages: int = 5
) -> set[tuple[str, str]]:
    """Retrieve existing jobs from repository issues for deduplication.

    Args:
        github_client: Initialized GitHub client wrapper.
        max_pages: Maximum number of pages to fetch from the GitHub API.

    Returns:
        Set of lowercase (company, role) tuples.
    """
    # Fetch up to 100 recent issues per page to balance API response payload size
    # with deduplication freshness.
    logger.info("Fetching deduplication cache of recent issues...")
    existing = set()
    title_pattern = re.compile(r"^\[([^\]]+)\]\s+(.*)$")

    for page in range(1, max_pages + 1):
        try:
            issues = github_client.list_issues(state="all", per_page=100, page=page)
            if not issues:
                break
        except Exception as e:
            logger.error(
                "Failed to fetch issues (page %d) from GitHub: %s. Stopping cache.",
                page,
                e,
            )
            break

        for issue in issues:
            title = issue.get("title") or ""
            match = title_pattern.match(title)
            if match:
                company = match.group(1).strip().lower()
                role = match.group(2).strip().lower()
                existing.add((company, role))

        if len(issues) < 100:
            break

    logger.info("Successfully cached %d existing jobs.", len(existing))
    return existing


def classify_work_type(location: str, description: str) -> str:
    """Classify job location and description into 'remote', 'hybrid', or 'onsite'."""
    loc_lower = location.lower()
    desc_lower = description.lower()

    # Check location string first
    if "hybrid" in loc_lower:
        return "hybrid"
    if "remote" in loc_lower:
        # Check if description contradicts and makes it hybrid
        if any(ind in desc_lower for ind in HYBRID_INDICATORS):
            return "hybrid"
        return "remote"

    # Check description for hybrid indicators
    if any(ind in desc_lower for ind in HYBRID_INDICATORS):
        return "hybrid"

    # Check description for remote indicators
    has_remote = any(ind in desc_lower for ind in REMOTE_INDICATORS)
    has_onsite = any(ind in desc_lower for ind in ONSITE_INDICATORS)

    if has_remote and not has_onsite:
        return "remote"

    # Default is onsite
    return "onsite"


def is_local_proximity_match(
    job_location: str,
    candidate_city: str,
    candidate_state: str,
) -> bool:
    """Check if the job location matches the candidate's city/state.

    Enforces word boundary matching for state abbreviations to prevent false-positives
    (e.g., 'Austin, TX' matching state 'in').
    """
    job_loc_lower = job_location.lower().strip()
    city_lower = candidate_city.lower().strip()
    state_lower = candidate_state.lower().strip()

    # Check city match
    if city_lower and city_lower in job_loc_lower:
        return True

    # Check state match
    if state_lower:
        if len(state_lower) <= 2:
            # Enforce word boundaries for short state/region abbreviations
            if re.search(r"\b" + re.escape(state_lower) + r"\b", job_loc_lower):
                return True
        else:
            # For full names (e.g., 'Washington' or 'Ontario'), substring check is safe
            if state_lower in job_loc_lower:
                return True

    return False


def parse_job_row(row: Any) -> ScrapedJob:
    """Extracts and sanitizes a ScrapedJob DTO from a pandas Series or dictionary.

    Args:
        row: The raw job row data (Series or dictionary).

    Returns:
        The parsed and sanitized ScrapedJob object.
    """
    company = _clean_str(row.get("company")) or "Unknown Company"
    title = _clean_str(row.get("title")) or "Unknown Role"
    location = _clean_str(row.get("location")) or "Unknown Location"
    description = _clean_str(row.get("description")) or "No job description provided."

    # Try job_url first, fallback to job_url_direct safely resolving empty strings
    apply_url = (
        _clean_str(row.get("job_url"))
        or _clean_str(row.get("job_url_direct"))
        or "Not available"
    )

    source = _clean_str(row.get("site")) or "Unknown"

    salary = format_salary(
        min_amount=row.get("min_amount"),
        max_amount=row.get("max_amount"),
        currency=row.get("currency"),
        interval=row.get("interval"),
    )

    return ScrapedJob(
        company=company,
        title=title,
        location=location,
        salary=salary,
        source=source,
        apply_url=apply_url,
        description=description,
    )


def publish_job(
    github_client: GitHubClient, job: ScrapedJob, dry_run: bool
) -> dict[str, Any] | None:
    """Publish a ScrapedJob to GitHub Issues and update Projects V2 status.

    Args:
        github_client: Initialized GitHubClient wrapper.
        job: The ScrapedJob to publish.
        dry_run: If True, log mock action without API requests.

    Returns:
        The created issue response dictionary, or None if dry_run.
    """
    issue_title = f"[{job.company}] {job.title}"
    issue_body = build_issue_body(
        company=job.company,
        title=job.title,
        location=job.location,
        salary=job.salary,
        source=job.source,
        apply_url=job.apply_url,
        description=job.description,
    )

    if dry_run:
        logger.info(
            "[DRY RUN] Would create issue: %s. Body length: %d chars",
            issue_title,
            len(issue_body),
        )
        return None

    logger.info("Creating issue for: %s", issue_title)
    issue = github_client.create_issue(
        title=issue_title,
        body=issue_body,
        labels=["triage-pending"],
    )

    # Update Projects V2 if configured
    if github_client.project_id:
        node_id = issue.get("node_id")
        if node_id:
            status = LABEL_TO_STATUS["triage-pending"]
            logger.info(
                "Adding node %s to status '%s'",
                node_id,
                status,
            )
            try:
                github_client.update_project_status(node_id, status)
            except Exception as pe:
                logger.warning("Failed to update Projects V2 status: %s", pe)
    return issue


def run_scraper(
    settings_path: str = "config/settings.yaml",
    resume_path: str = "resumes/resume.yaml",
    dry_run: bool = False,
    github_client: GitHubClient | None = None,
    scrape_fn: Callable | None = None,
    work_preference_override: str | None = None,
    job_type_override: str | None = None,
    hours_old_override: int | None = None,
) -> None:
    """Main execution function for the job scraper bot.

    Args:
        settings_path: Filepath of the system settings.yaml.
        resume_path: Filepath of the base resume.yaml.
        dry_run: If True, skip all remote GitHub write/read operations.
        github_client: Optional injected GitHubClient instance.
        scrape_fn: Optional injected scraping function to override jobspy.
        work_preference_override: Optional work preference override to ignore settings.
        job_type_override: Optional job type override to ignore settings.
        hours_old_override: Optional hours old override to ignore settings.
    """
    logger.info("Loading settings from: %s", settings_path)
    settings = load_settings(settings_path)

    if not settings.search.enabled:
        logger.info("Scraper is disabled in settings.yaml. Skipping execution.")
        return

    # Always load resume to derive location for country/local filtering
    resume = load_resume(resume_path)

    # 1. Resolve GitHub client (with dependency injection fallback)
    if github_client is None:
        token = os.environ.get("GITHUB_TOKEN")
        repo = os.environ.get("GITHUB_REPOSITORY")

        if dry_run:
            logger.info(
                "Running in DRY RUN mode. GitHub API operations will be skipped."
            )
            token = token or "dummy_token"
            repo = repo or "dummy/repo"
        elif not token or not repo:
            raise ValueError(
                "Missing GITHUB_TOKEN or GITHUB_REPOSITORY environment variables."
            )

        project_id = None
        status_field = "Status"
        if settings.projects_v2:
            project_id = settings.projects_v2.project_id
            status_field = settings.projects_v2.status_field_name

        github_client = GitHubClient(
            token=token,
            repo=repo,
            project_id=project_id,
            status_field_name=status_field,
        )

    # 2. Resolve scraping method (dependency injection fallback)
    if scrape_fn is None:
        from jobspy import scrape_jobs

        scrape_fn = scrape_jobs

    # 3. Fetch existing issues cache for deduplication
    existing_jobs = set() if dry_run else fetch_existing_jobs_cache(github_client)

    # 4. Generate queries by passing the loaded Resume object
    queries = generate_queries(resume, settings.custom_queries)

    # 5. Extract scraper settings
    platforms = settings.search.platforms
    work_preference = (
        work_preference_override
        if work_preference_override is not None
        else settings.search.work_preference
    )
    work_preference = work_preference.lower().strip()
    if work_preference not in ("remote", "onsite", "hybrid"):
        raise ValueError(
            f"Invalid work preference '{work_preference}'. "
            "Must be one of: remote, onsite, hybrid."
        )

    job_type = (
        job_type_override if job_type_override is not None else settings.search.job_type
    )
    hours_old = (
        hours_old_override
        if hours_old_override is not None
        else settings.search.hours_old
    )

    # Map country code to search country name
    user_country_code = (
        resume.basics.location.country_code.upper() if resume.basics.location else "US"
    )

    # Derive python-jobspy query parameters from work preference and candidate location
    if work_preference == "remote":
        search_location = user_country_code
        is_remote = True
    else:
        is_remote = False
        city = resume.basics.location.city if resume.basics.location else ""
        state = resume.basics.location.state if resume.basics.location else ""
        if city and state:
            search_location = f"{city}, {state}"
        elif city:
            search_location = city
        else:
            search_location = user_country_code

    # Pre-resolve and lowercase candidate location attributes once outside the loop
    # (null-safe)
    cand_city = ""
    cand_state = ""
    if resume.basics.location:
        if resume.basics.location.city:
            cand_city = resume.basics.location.city.lower().strip()
        if resume.basics.location.state:
            cand_state = resume.basics.location.state.lower().strip()

    logger.info(
        "Scraper config: platforms=%s, work_preference='%s', "
        "search_location='%s', job_type=%s, hours_old=%d",
        platforms,
        work_preference,
        search_location,
        job_type,
        hours_old,
    )

    # 6. Sequential search loop
    for index, query in enumerate(queries):
        logger.info(
            "Executing query (%d/%d): '%s'",
            index + 1,
            len(queries),
            query,
        )
        try:
            extra_kwargs: dict[str, Any] = {}
            if "linkedin" in platforms:
                extra_kwargs["linkedin_fetch_description"] = True

            jobs_df = scrape_fn(
                site_name=platforms,
                search_term=query,
                location=search_location,
                is_remote=is_remote,
                job_type=job_type,
                hours_old=hours_old,
                results_wanted=15,
                **extra_kwargs,
            )

            if jobs_df is None or jobs_df.empty:
                logger.info("No job postings found for query: '%s'", query)
            else:
                logger.info("Found %d jobs for query: '%s'", len(jobs_df), query)
                new_listings_count = 0

                # Iterate over native dicts to avoid iterrows() Series allocation
                # overhead as recommended in review audits.
                for row in jobs_df.to_dict(orient="records"):
                    job = parse_job_row(row)

                    comp_lower = job.company.lower()
                    title_lower = job.title.lower()
                    if (comp_lower, title_lower) in existing_jobs:
                        logger.debug(
                            "Skipping duplicate role: [%s] %s",
                            job.company,
                            job.title,
                        )
                        continue

                    # Filter by work preference type
                    job_work_type = classify_work_type(job.location, job.description)
                    is_match = job_work_type == work_preference
                    # Onsite and hybrid are mutually compatible as both are local
                    # office-based
                    if work_preference in ("onsite", "hybrid") and job_work_type in (
                        "onsite",
                        "hybrid",
                    ):
                        is_match = True

                    if not is_match:
                        logger.info(
                            "Skipping role with mismatching work type "
                            "(got '%s', want '%s'): [%s] %s - Location: %s",
                            job_work_type,
                            work_preference,
                            job.company,
                            job.title,
                            job.location,
                        )
                        continue

                    # Local proximity check for onsite/hybrid
                    if (
                        work_preference in ("onsite", "hybrid")
                        and resume.basics.location
                        and not is_local_proximity_match(
                            job.location, cand_city, cand_state
                        )
                    ):
                        logger.info(
                            "Skipping local role outside candidate's "
                            "city/region: [%s] %s - Location: %s "
                            "(User location: %s, %s)",
                            job.company,
                            job.title,
                            job.location,
                            resume.basics.location.city or "",
                            resume.basics.location.state or "",
                        )
                        continue

                    try:
                        publish_job(github_client, job, dry_run)
                        new_listings_count += 1
                        existing_jobs.add((comp_lower, title_lower))
                    except Exception as ie:
                        logger.error(
                            "Failed to process job '%s' by '%s': %s",
                            job.title,
                            job.company,
                            ie,
                        )

                logger.info(
                    "Processed %d new roles for query: '%s'",
                    new_listings_count,
                    query,
                )

        except Exception as e:
            logger.error("Scraper failed for query '%s': %s. Continuing...", query, e)

        # Sleep/Throttling between queries (except for the last one)
        if index < len(queries) - 1:
            delay = random.uniform(2.0, 5.0)
            logger.info("Throttling: sleeping for %.2fs before next query...", delay)
            time.sleep(delay)

    logger.info("Scraper execution completed successfully.")
