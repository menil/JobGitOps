"""Unit tests for the job scraper bot (jobgitops/scraper.py)."""

import os
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from jobgitops.scraper import (
    ScrapedJob,
    build_issue_body,
    fetch_existing_jobs_cache,
    format_salary,
    generate_queries,
    parse_job_row,
    publish_job,
    run_scraper,
)


def test_format_salary() -> None:
    """Verify salary formatting handles various inputs and edge cases."""
    # Both min and max provided
    val1 = format_salary(100000, 150000, "USD", "yearly")
    assert val1 == "$100,000 - $150,000 (yearly)"

    # Only min provided
    assert format_salary(80000, None, "USD", "yearly") == "From $80,000 (yearly)"

    # Only max provided
    assert format_salary(None, 120000, "USD", "yearly") == "Up to $120,000 (yearly)"

    # Different currency
    val2 = format_salary(5000, 8000, "EUR", "monthly")
    assert val2 == "EUR 5,000 - EUR 8,000 (monthly)"

    # No salary info
    assert format_salary(None, None, "USD", "yearly") == "Not specified"

    # String coercion
    assert format_salary("90000", "110000", None, None) == "$90,000 - $110,000"

    # Bad values fallback
    assert format_salary("invalid", None, "USD", None) == "Not specified"

    # NaN handling
    assert format_salary(float("nan"), None, "USD", "yearly") == "Not specified"
    assert format_salary(None, float("nan"), "USD", None) == "Not specified"
    assert format_salary(float("nan"), float("nan"), "USD", None) == "Not specified"
    assert (
        format_salary(100000, 150000, float("nan"), float("nan"))
        == "$100,000 - $150,000"
    )

    # Pandas Series handling (duplicate columns)
    assert (
        format_salary(
            pd.Series([100000, 120000]),
            pd.Series([150000]),
            "USD",
            "yearly",
        )
        == "$100,000 - $150,000 (yearly)"
    )


def test_build_issue_body() -> None:
    """Verify markdown issue body construction and fallback values."""
    body = build_issue_body(
        company="Acme Corp",
        title="Python Engineer",
        location="Remote",
        salary="$100k - $120k",
        source="Indeed",
        apply_url="https://acme.com/apply",
        description="Write python code.",
    )
    assert "# Python Engineer at Acme Corp" in body
    assert "- **Company:** Acme Corp" in body
    assert "- **Role:** Python Engineer" in body
    assert "- **Location:** Remote" in body
    assert "- **Salary:** $100k - $120k" in body
    assert "- **Source:** Indeed" in body
    assert "- **Apply URL:** https://acme.com/apply" in body
    assert "Write python code." in body


def test_generate_queries_resume_driven() -> None:
    """Verify query generation from Resume object."""
    mock_resume = MagicMock()
    mock_resume.work = [MagicMock(position="Senior Python Developer")]
    mock_resume.skills = [
        MagicMock(keywords=["FastAPI", "Docker", "Git"]),
        # Duplicate keyword 'Docker', and whitespace padded keyword
        MagicMock(keywords=["Kubernetes", "AWS", " FastAPI "]),
    ]

    queries = generate_queries(mock_resume, None)

    expected = [
        "Senior Python Developer FastAPI",
        "Senior Python Developer Docker",
        "Senior Python Developer Git",
        "Senior Python Developer Kubernetes",
        "Senior Python Developer AWS",
    ]
    assert queries == expected


def test_generate_queries_resume_driven_fallback() -> None:
    """Verify query generation fallback when work and skills are sparse."""
    mock_resume = MagicMock()
    mock_resume.work = []
    mock_resume.basics.label = "Staff Engineer"
    mock_resume.skills = []

    queries = generate_queries(mock_resume, None)
    assert queries == ["Staff Engineer"]


def test_generate_queries_resume_driven_ultimate_fallback() -> None:
    """Verify query generation ultimate fallback to Software Engineer."""
    mock_resume = MagicMock()
    mock_resume.work = []
    mock_resume.basics.label = None
    mock_resume.skills = []

    queries = generate_queries(mock_resume, None)
    assert queries == ["Software Engineer"]


def test_generate_queries_custom_override() -> None:
    """Verify custom queries override resume parsing entirely."""
    mock_resume = MagicMock()
    custom = ["Remote React Developer", "Data Scientist Seattle"]
    queries = generate_queries(mock_resume, custom)
    assert queries == custom


def test_fetch_existing_jobs_cache() -> None:
    """Verify issue titles are fetched and parsed correctly for duplicate check."""
    mock_client = MagicMock()
    # Construct 100 items for page 1 to trigger loading page 2
    page1 = [{"title": f"[Company {i}] Role {i}"} for i in range(100)]
    # Wayne Enterprises on page 2
    page2 = [{"title": "[Wayne Enterprises] CEO"}]
    mock_client.list_issues.side_effect = [page1, page2, []]

    cache = fetch_existing_jobs_cache(mock_client, max_pages=3)
    assert len(cache) == 101
    assert ("company 0", "role 0") in cache
    assert ("wayne enterprises", "ceo") in cache


def test_fetch_existing_jobs_cache_failure() -> None:
    """Verify cache fetching handles GitHub API failure gracefully."""
    mock_client = MagicMock()
    mock_client.list_issues.side_effect = Exception("API rate limit exceeded")

    cache = fetch_existing_jobs_cache(mock_client)
    assert cache == set()


def test_parse_job_row() -> None:
    """Verify parse_job_row handles normal values, NaNs, and whitespace fallbacks."""
    # Test valid row
    row1 = {
        "company": "Acme",
        "title": "Engineer",
        "location": "Boston",
        "description": "Short desc",
        "job_url": "https://acme.com",
        "site": "linkedin",
    }
    job1 = parse_job_row(row1)
    assert job1.company == "Acme"
    assert job1.apply_url == "https://acme.com"
    assert job1.salary == "Not specified"

    # Test NaN / Null row
    row2 = {
        "company": float("nan"),
        "title": float("nan"),
        "location": float("nan"),
        "description": float("nan"),
        "job_url": float("nan"),
        "job_url_direct": float("nan"),
        "site": float("nan"),
    }
    job2 = parse_job_row(row2)
    assert job2.company == "Unknown Company"
    assert job2.title == "Unknown Role"
    assert job2.location == "Unknown Location"
    assert job2.description == "No job description provided."
    assert job2.apply_url == "Not available"
    assert job2.source == "Unknown"

    # Test whitespace / empty string URL selection fallback
    row3 = {
        "company": "Google",
        "title": "Developer",
        "job_url": "   ",
        "job_url_direct": "https://google.com/apply",
    }
    job3 = parse_job_row(row3)
    assert job3.apply_url == "https://google.com/apply"


def test_publish_job_dry_run() -> None:
    """Verify publish_job skips API operations in dry-run mode."""
    mock_client = MagicMock()
    job = ScrapedJob(
        company="Acme",
        title="Engineer",
        location="Remote",
        salary="Not specified",
        source="indeed",
        apply_url="Not available",
        description="Write code.",
    )
    res = publish_job(mock_client, job, dry_run=True)
    assert res is None
    mock_client.create_issue.assert_not_called()


def test_publish_job_success() -> None:
    """Verify publish_job issues API requests and Project status update."""
    mock_client = MagicMock()
    mock_client.project_id = "PROJ_123"
    mock_client.create_issue.return_value = {
        "number": 101,
        "node_id": "ND101",
    }
    job = ScrapedJob(
        company="Acme",
        title="Engineer",
        location="Remote",
        salary="Not specified",
        source="indeed",
        apply_url="Not available",
        description="Write code.",
    )
    res = publish_job(mock_client, job, dry_run=False)
    assert res == {"number": 101, "node_id": "ND101"}
    mock_client.create_issue.assert_called_once()
    mock_client.update_project_status.assert_called_once_with("ND101", "Triage Pending")


@patch("jobgitops.scraper.time.sleep")
@patch("jobgitops.scraper.load_settings")
@patch("jobgitops.scraper.load_resume")
def test_run_scraper_success(
    mock_load_resume,
    mock_load_settings,
    mock_sleep,
) -> None:
    """Test successful scraper execution flow using dependency injection."""
    mock_settings = MagicMock()
    mock_settings.custom_queries = ["Python Developer"]
    mock_settings.search.platforms = ["linkedin"]
    mock_settings.search.location = "Remote"
    mock_settings.search.job_type = "fulltime"
    mock_settings.search.hours_old = 24
    mock_settings.projects_v2 = MagicMock(
        project_id="PROJ123", status_field_name="Status"
    )
    mock_load_settings.return_value = mock_settings

    mock_resume = MagicMock()
    mock_resume.work = []
    mock_resume.basics.label = "Software Engineer"
    mock_resume.skills = []
    mock_load_resume.return_value = mock_resume

    mock_github_client = MagicMock()
    mock_github_client.project_id = "PROJ123"
    # Return page 1 with active issues, page 2 empty to break pagination loop
    mock_github_client.list_issues.side_effect = [
        [{"title": "[Existing Company] Existing Role"}],
        [],
    ]
    mock_github_client.create_issue.return_value = {
        "number": 42,
        "node_id": "ISSUE_NODE_ID",
    }

    mock_scrape_jobs = MagicMock()
    # 3 mock postings: 1 duplicate, 1 valid new job, 1 NaN-sanitized job
    jobs_df = pd.DataFrame(
        [
            {
                "company": "Existing Company",
                "title": "Existing Role",
                "location": "Remote",
                "description": "Skip me",
                "job_url": "https://skip.com",
                "site": "linkedin",
            },
            {
                "company": "New Company",
                "title": "New Role",
                "location": "Remote",
                "description": "Scrape me",
                "job_url": "https://scrape.com",
                "site": "linkedin",
                "min_amount": 120000,
                "max_amount": 160000,
                "currency": "USD",
                "interval": "yearly",
            },
            {
                "company": float("nan"),
                "title": float("nan"),
                "location": float("nan"),
                "description": float("nan"),
                "job_url": "   ",
                "job_url_direct": "https://direct.com",
                "site": "linkedin",
            },
        ]
    )
    mock_scrape_jobs.return_value = jobs_df

    environ_mock = {"GITHUB_TOKEN": "test_token", "GITHUB_REPOSITORY": "owner/repo"}

    with patch.dict(os.environ, environ_mock):
        run_scraper(
            github_client=mock_github_client,
            scrape_fn=mock_scrape_jobs,
        )

    # Verify existing issues cache page-fetching is called for page 1
    # (stops early since len(issues) < 100)
    assert mock_github_client.list_issues.call_count == 1
    mock_github_client.list_issues.assert_called_once_with(
        state="all", per_page=100, page=1
    )

    # Verify only the new job and the NaN-sanitized job were created as issues
    assert mock_github_client.create_issue.call_count == 2

    # Verify Projects V2 field update was called
    mock_github_client.update_project_status.assert_any_call(
        "ISSUE_NODE_ID", "Triage Pending"
    )


@patch("jobgitops.scraper.load_settings")
@patch("jobgitops.scraper.load_resume")
def test_run_scraper_missing_env(
    mock_load_resume,
    mock_load_settings,
) -> None:
    """Verify scraper raises ValueError if env vars are missing."""
    mock_load_settings.return_value = MagicMock()

    with (
        patch.dict(os.environ, {}, clear=True),
        pytest.raises(ValueError) as exc_info,
    ):
        run_scraper()

    assert "Missing GITHUB_TOKEN" in str(exc_info.value)


@patch("jobgitops.scraper.time.sleep")
@patch("jobgitops.scraper.load_settings")
@patch("jobgitops.scraper.load_resume")
def test_run_scraper_robustness(
    mock_load_resume,
    mock_load_settings,
    mock_sleep,
) -> None:
    """Verify scraper robustness when query fails and issue creation fails."""
    mock_settings = MagicMock()
    mock_settings.custom_queries = ["Query 1", "Query 2"]
    mock_settings.search.platforms = ["linkedin"]
    mock_settings.search.location = "Seattle, WA"
    mock_settings.search.job_type = "fulltime"
    mock_settings.search.hours_old = 24
    mock_settings.projects_v2 = None
    mock_load_settings.return_value = mock_settings

    mock_github_client = MagicMock()
    mock_github_client.project_id = None
    mock_github_client.list_issues.return_value = []
    # simulate create_issue raising exception for Query 2
    mock_github_client.create_issue.side_effect = Exception("API rate limit exceeded")

    mock_scrape_jobs = MagicMock()
    # Query 1 fails; Query 2 returns a valid job
    mock_scrape_jobs.side_effect = [
        Exception("Rate limit on LinkedIn"),
        pd.DataFrame(
            [
                {
                    "company": "Failed Company",
                    "title": "Failed Role",
                    "location": "Seattle, WA",
                    "description": "Try me",
                    "job_url": "https://failed.com",
                    "site": "linkedin",
                }
            ]
        ),
    ]

    environ_mock = {"GITHUB_TOKEN": "test_token", "GITHUB_REPOSITORY": "owner/repo"}
    with patch.dict(os.environ, environ_mock):
        run_scraper(
            github_client=mock_github_client,
            scrape_fn=mock_scrape_jobs,
        )

    # Both scrape jobs should be attempted
    assert mock_scrape_jobs.call_count == 2
    mock_sleep.assert_called_once()


@patch("jobgitops.scraper.load_settings")
@patch("jobgitops.scraper.load_resume")
def test_run_scraper_dry_run(
    mock_load_resume,
    mock_load_settings,
) -> None:
    """Verify scraper dry-run mode skips API calls."""
    mock_settings = MagicMock()
    mock_settings.custom_queries = ["Dry Run Query"]
    mock_settings.search.platforms = ["linkedin"]
    mock_settings.search.location = "Remote"
    mock_settings.search.job_type = "fulltime"
    mock_settings.search.hours_old = 24
    mock_settings.projects_v2 = None
    mock_load_settings.return_value = mock_settings

    mock_resume = MagicMock()
    mock_load_resume.return_value = mock_resume

    mock_github_client = MagicMock()
    mock_scrape_jobs = MagicMock()

    mock_scrape_jobs.return_value = pd.DataFrame(
        [
            {
                "company": "Dry Run Company",
                "title": "Dry Run Role",
                "location": "Remote",
                "description": "Don't create me",
                "job_url": "https://dryrun.com",
                "site": "linkedin",
            }
        ]
    )

    # No environment variables set (should pass in dry-run mode)
    with patch.dict(os.environ, {}, clear=True):
        run_scraper(
            dry_run=True,
            github_client=mock_github_client,
            scrape_fn=mock_scrape_jobs,
        )

    # Verify existing issues were NOT listed (cache skipped)
    mock_github_client.list_issues.assert_not_called()

    # Verify scrape_jobs was still called
    mock_scrape_jobs.assert_called_once()

    # Verify create_issue was NOT called
    mock_github_client.create_issue.assert_not_called()


@patch("jobgitops.scraper.load_settings")
@patch("jobgitops.scraper.load_resume")
def test_run_scraper_overrides(
    mock_load_resume,
    mock_load_settings,
) -> None:
    """Verify run_scraper respects optional parameter overrides."""
    mock_settings = MagicMock()
    mock_settings.custom_queries = ["Query"]
    mock_settings.search.platforms = ["linkedin"]
    mock_settings.search.location = "Remote"
    mock_settings.search.job_type = "fulltime"
    mock_settings.search.hours_old = 24
    mock_settings.projects_v2 = None
    mock_load_settings.return_value = mock_settings

    mock_resume = MagicMock()
    mock_load_resume.return_value = mock_resume

    mock_github_client = MagicMock()
    mock_scrape_jobs = MagicMock()
    mock_scrape_jobs.return_value = pd.DataFrame([])

    run_scraper(
        dry_run=True,
        github_client=mock_github_client,
        scrape_fn=mock_scrape_jobs,
        location_override="Sunnyvale, CA",
        job_type_override="contract",
        hours_old_override=48,
    )

    # Check that mock_scrape_jobs was called with overridden parameters
    mock_scrape_jobs.assert_called_once_with(
        site_name=["linkedin"],
        search_term="Query",
        location="Sunnyvale, CA",
        is_remote=False,
        job_type="contract",
        hours_old=48,
        results_wanted=15,
        linkedin_fetch_description=True,
    )


@patch("scrape.run_scraper")
@patch(
    "sys.argv",
    [
        "scrape.py",
        "--location",
        "Seattle, WA",
        "--job-type",
        "parttime",
        "--hours-old",
        "72",
        "--dry-run",
    ],
)
def test_scrape_cli_args(mock_run_scraper) -> None:
    """Verify scrape.py CLI entry point parses overrides correctly."""
    from scrape import main as cli_main

    cli_main()
    mock_run_scraper.assert_called_once_with(
        settings_path="config/settings.yaml",
        resume_path="resumes/resume.yaml",
        dry_run=True,
        location_override="Seattle, WA",
        job_type_override="parttime",
        hours_old_override=72,
    )


def test_is_strictly_local_role() -> None:
    """Verify is_strictly_local_role matches hybrid/onsite indicator keywords."""
    from jobgitops.scraper import is_strictly_local_role

    # Remote location or mentions "remote" should NOT be strictly local
    assert not is_strictly_local_role("Remote", "This is hybrid")
    assert not is_strictly_local_role("San Francisco (Remote)", "hybrid work")

    # City location with hybrid description should be detected as strictly local
    assert is_strictly_local_role("San Francisco, CA", "This is a hybrid model role.")
    assert is_strictly_local_role("Seattle, WA", "Requires 3 days/week in office.")
    assert is_strictly_local_role("Dallas, TX", "This is an onsite required position.")

    # City location with purely remote description should NOT be strictly local
    assert not is_strictly_local_role(
        "New York, NY", "This is a fully remote position."
    )


@patch("jobgitops.scraper.load_settings")
@patch("jobgitops.scraper.load_resume")
def test_run_scraper_skips_hybrid(
    mock_load_resume,
    mock_load_settings,
) -> None:
    """Verify run_scraper filters out hybrid/onsite listings when location is Remote."""
    mock_settings = MagicMock()
    mock_settings.custom_queries = ["Query"]
    mock_settings.search.platforms = ["linkedin"]
    mock_settings.search.location = "Remote"
    mock_settings.search.job_type = "fulltime"
    mock_settings.search.hours_old = 24
    mock_settings.projects_v2 = None
    mock_load_settings.return_value = mock_settings

    mock_resume = MagicMock()
    mock_load_resume.return_value = mock_resume

    mock_github_client = MagicMock()
    mock_scrape_jobs = MagicMock()
    # Return two jobs: one fully remote, one hybrid
    mock_scrape_jobs.return_value = pd.DataFrame(
        [
            {
                "company": "Remote Co",
                "title": "Remote Engineer",
                "location": "San Francisco, CA",  # City name but description is remote
                "description": "This is a fully remote role.",
                "job_url": "https://remote.com",
                "site": "linkedin",
            },
            {
                "company": "Hybrid Co",
                "title": "Hybrid Engineer",
                "location": "San Francisco, CA",
                "description": "Requires 3 days/week in office.",
                "job_url": "https://hybrid.com",
                "site": "linkedin",
            },
        ]
    )

    environ_mock = {"GITHUB_TOKEN": "test_token", "GITHUB_REPOSITORY": "owner/repo"}
    with patch.dict(os.environ, environ_mock):
        run_scraper(
            dry_run=False,
            github_client=mock_github_client,
            scrape_fn=mock_scrape_jobs,
        )

    # Only Remote Engineer should be published, Hybrid Engineer should be skipped
    assert mock_github_client.create_issue.call_count == 1
    _, kwargs = mock_github_client.create_issue.call_args
    assert "Remote Co" in kwargs["title"]
    assert "Remote Engineer" in kwargs["title"]
