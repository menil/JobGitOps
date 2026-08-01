"""Unit tests for the triage and tailoring coordinator (triage.py)."""

import hashlib
import json
import os
import pathlib
import sys
from unittest import mock

import pytest

from jobgitops.github_client import GitHubClient
from jobgitops.llm import LLMClient, QuotaExceededError, TriageResult
from jobgitops.schema import Resume, Settings
from triage import (
    EXIT_QUOTA_EXCEEDED,
    FIT_CATEGORY_MISMATCH_LABELS,
    get_category_mismatch_labels,
    get_fit_grade_label,
    main,
    parse_job_details,
    run_triage,
)


@pytest.fixture
def mock_resume() -> Resume:
    """Mock Resume object for tests."""
    return Resume.from_dict(
        {
            "basics": {
                "name": "Jane Doe",
                "email": "jane@example.com",
                "summary": "Experienced engineer.",
            },
            "work": [
                {
                    "name": "Old Corp",
                    "position": "Staff Engineer",
                    "startDate": "2020-01-01",
                    "endDate": "2024-01-01",
                    "highlights": ["Led development of core features."],
                }
            ],
            "skills": [{"name": "Languages", "keywords": ["Python", "C++"]}],
        }
    )


@pytest.fixture
def mock_settings() -> Settings:
    """Mock Settings object for tests."""
    return Settings(fit_threshold=3.5)


def test_parse_job_details_standard() -> None:
    """Test parse_job_details with a standard well-formed issue body."""
    body = (
        "# Senior Staff Engineer at Acme\n\n"
        "## Job Details\n"
        "- **Company:** Acme Corporation\n"
        "- **Role:** Senior Staff Engineer\n"
        "- **Location:** Seattle, WA (Remote)\n"
        "- **Salary:** $180,000 - $220,000\n"
        "- **Source:** LinkedIn\n"
        "- **Apply URL:** [Apply Link](https://acme.com/apply)\n\n"
        "## Job Description\n"
        "We are looking for a backend engineer with Python skills."
    )
    details = parse_job_details(body, "Ignore Title")
    assert details["company"] == "Acme Corporation"
    assert details["role"] == "Senior Staff Engineer"
    assert details["location"] == "Seattle, WA (Remote)"
    assert details["salary"] == "$180,000 - $220,000"
    assert details["source"] == "LinkedIn"
    assert details["apply_url"] == "https://acme.com/apply"
    assert (
        details["description"]
        == "We are looking for a backend engineer with Python skills."
    )


def test_parse_job_details_raw_url() -> None:
    """Test parse_job_details when the Apply URL is not in markdown format."""
    body = (
        "## Job Details\n"
        "- **Company:** Acme Corporation\n"
        "- **Role:** Senior Staff Engineer\n"
        "- **Apply URL:** https://acme.com/apply\n"
    )
    details = parse_job_details(body)
    assert details["apply_url"] == "https://acme.com/apply"


def test_parse_job_details_markdown_injection_prevention() -> None:
    """Test parse_job_details ignores apply URLs that are link injection payloads."""
    body = (
        "## Job Details\n"
        "- **Company:** Acme\n"
        "- **Role:** Engineer\n"
        "- **Apply URL:** https://acme.com) [Click Here](https://malicious.com\n"
    )
    details = parse_job_details(body)
    assert details["apply_url"] == ""


def test_parse_job_details_empty_and_none() -> None:
    """Test parse_job_details handles None and empty inputs defensively."""
    details = parse_job_details(None, None)
    assert details["company"] == "Unknown Company"
    assert details["role"] == "Unknown Role"
    assert details["apply_url"] == ""
    assert details["description"] == ""


def test_parse_job_details_fallbacks() -> None:
    """Test parse_job_details fallbacks from issue title when body is missing fields."""
    body_empty = "Some random text that doesn't match standard details."

    # Fallback pattern 1: [Company] Role Title
    details1 = parse_job_details(body_empty, "[Acme Corp] Senior Python Developer")
    assert details1["company"] == "Acme Corp"
    assert details1["role"] == "Senior Python Developer"

    # Fallback pattern 2: Role at Company
    details2 = parse_job_details(body_empty, "Go Developer at Tech Giants")
    assert details2["company"] == "Tech Giants"
    assert details2["role"] == "Go Developer"

    # Fallback pattern 3: Just role
    details3 = parse_job_details(body_empty, "General Role")
    assert details3["company"] == "Unknown Company"
    assert details3["role"] == "General Role"


@pytest.mark.parametrize(
    ("fit_score", "expected"),
    [
        (4.6, "fit:A+"),
        (5.0, "fit:A+"),
        (4.5, "fit:A"),
        (4.1, "fit:A"),
        (4.0, "fit:B"),
        (3.6, "fit:B"),
        (3.5, "fit:B"),
    ],
)
def test_get_fit_grade_label(fit_score: float, expected: str) -> None:
    """Test fit score to tier label mapping at all boundaries."""
    assert get_fit_grade_label(fit_score) == expected


@pytest.mark.parametrize(
    ("scores", "expected"),
    [
        (
            (2.0, 3.0, 4.0, 5.0, 3.5),
            ["tech-stack-mismatch"],
        ),
        (
            (3.0, 4.0, 5.0, 2.0, 3.5),
            ["salary-mismatch"],
        ),
        (
            (2.5, 4.0, 2.0, 2.9, 3.0),
            ["location-mismatch", "salary-mismatch", "tech-stack-mismatch"],
        ),
        (
            (5.0, 5.0, 5.0, 5.0, 5.0),
            [],
        ),
        (
            (3.0, 3.0, 3.0, 3.0, 3.0),
            [],
        ),
        (
            (1.0, 1.0, 1.0, 1.0, 1.0),
            [
                "experience-mismatch",
                "industry-mismatch",
                "location-mismatch",
                "salary-mismatch",
                "tech-stack-mismatch",
            ],
        ),
    ],
)
def test_get_category_mismatch_labels(
    scores: tuple[float, float, float, float, float], expected: list[str]
) -> None:
    """Test mismatch reason labels for categories scored below the threshold."""
    triage_res = TriageResult(
        fit_score=sum(scores) / len(scores),
        tech_stack_fit=scores[0],
        experience_fit=scores[1],
        location_fit=scores[2],
        salary_fit=scores[3],
        industry_fit=scores[4],
        reasoning="Low scores across several dimensions.",
    )
    assert get_category_mismatch_labels(triage_res) == expected


def test_fit_category_mismatch_labels_keys_match_triage_result_fields() -> None:
    """Test every category label key maps to a real TriageResult field.

    Guards against a mistyped key silently breaking the dynamic attribute
    lookup inside get_category_mismatch_labels at runtime.
    """
    valid_fields = TriageResult.__dataclass_fields__
    assert set(FIT_CATEGORY_MISMATCH_LABELS).issubset(valid_fields)


def test_run_triage_mismatch(
    mock_resume: Resume,
    mock_settings: Settings,
) -> None:
    """Test run_triage mismatch path (fit score below threshold)."""
    mock_llm_client = mock.MagicMock(spec=LLMClient)
    mock_llm_client.triage_job.return_value = TriageResult(
        fit_score=3.4,
        tech_stack_fit=3.0,
        experience_fit=4.0,
        location_fit=2.5,
        salary_fit=2.0,
        industry_fit=3.5,
        reasoning="Insufficient Python experience.",
    )

    # Configure mock GitHubClient
    mock_gh_client = mock.MagicMock(spec=GitHubClient)
    mock_gh_client.repo = "owner/repo"
    mock_gh_client.project_id = "proj_123"

    body = (
        "**Company:** Acme\n"
        "**Role:** Python Dev\n"
        "**Apply URL:** https://acme.com\n"
        "## Job Description\n"
        "Need 10 years of Python."
    )

    run_triage(
        issue_number=12,
        issue_title="[Acme] Python Dev",
        issue_body=body,
        issue_node_id="node_abc",
        issue_labels=["triage-pending"],
        repo_path=pathlib.Path(),
        gh_client=mock_gh_client,
        settings=mock_settings,
        resume=mock_resume,
        llm_client=mock_llm_client,
    )

    # Verify LLM was called
    mock_llm_client.triage_job.assert_called_once_with(
        "Need 10 years of Python.", mock_resume
    )
    mock_llm_client.tailor_resume.assert_not_called()

    # Verify issue operations
    mock_gh_client.post_comment.assert_called_once()
    comment_arg = mock_gh_client.post_comment.call_args[0][1]
    assert "Mismatch Detected" in comment_arg
    assert "Insufficient Python experience" in comment_arg
    assert "3.4/3.5" in comment_arg
    assert "**Labels Added:** `location-mismatch`, `salary-mismatch`" in comment_arg

    mock_gh_client.remove_label.assert_called_once_with(12, "triage-pending")
    mock_gh_client.add_labels.assert_called_once_with(
        12, ["triage-mismatched", "location-mismatch", "salary-mismatch"]
    )
    mock_gh_client.close_issue.assert_called_once_with(12)
    mock_gh_client.update_project_status.assert_called_once_with(
        "node_abc", "Mismatched/Closed"
    )


def test_run_triage_mismatch_no_reason_labels(
    mock_resume: Resume,
    mock_settings: Settings,
) -> None:
    """Test mismatch path when no category is scored below the threshold.

    An overall fit score below threshold with every dimension at or above 3.0
    should still add triage-mismatched, without any reason labels.
    """
    mock_llm_client = mock.MagicMock(spec=LLMClient)
    mock_llm_client.triage_job.return_value = TriageResult(
        fit_score=3.4,
        tech_stack_fit=3.0,
        experience_fit=4.0,
        location_fit=3.2,
        salary_fit=3.1,
        industry_fit=3.5,
        reasoning="Experience scope does not align.",
    )

    mock_gh_client = mock.MagicMock(spec=GitHubClient)
    mock_gh_client.repo = "owner/repo"
    mock_gh_client.project_id = None

    body = (
        "**Company:** Acme\n"
        "**Role:** Python Dev\n"
        "**Apply URL:** https://acme.com\n"
        "## Job Description\n"
        "Need 10 years of Python."
    )

    run_triage(
        issue_number=13,
        issue_title="[Acme] Python Dev",
        issue_body=body,
        issue_node_id="node_abc",
        issue_labels=["triage-pending"],
        repo_path=pathlib.Path(),
        gh_client=mock_gh_client,
        settings=mock_settings,
        resume=mock_resume,
        llm_client=mock_llm_client,
    )

    comment_arg = mock_gh_client.post_comment.call_args[0][1]
    assert "**Labels Added:**" not in comment_arg
    mock_gh_client.add_labels.assert_called_once_with(13, ["triage-mismatched"])
    mock_gh_client.close_issue.assert_called_once_with(13)
    mock_gh_client.update_project_status.assert_not_called()


@mock.patch("triage.compile_resume")
@mock.patch("triage.commit_changes")
@mock.patch("triage.push_branch")
@mock.patch("triage.create_or_checkout_branch")
@mock.patch("triage.run_git")
@pytest.mark.parametrize(
    ("fit_score", "expected_label"),
    [
        (4.8, "fit:A+"),
        (4.2, "fit:A"),
        (3.8, "fit:B"),
    ],
)
def test_run_triage_match_approved(
    mock_run_git: mock.MagicMock,
    mock_checkout_branch: mock.MagicMock,
    mock_push_branch: mock.MagicMock,
    mock_commit: mock.MagicMock,
    mock_compile: mock.MagicMock,
    fit_score: float,
    expected_label: str,
    mock_resume: Resume,
    mock_settings: Settings,
    tmp_path: pathlib.Path,
) -> None:
    """Test run_triage match path (fit score >= threshold) across all fit tiers."""
    mock_llm_client = mock.MagicMock(spec=LLMClient)
    mock_llm_client.triage_job.return_value = TriageResult(
        fit_score=fit_score,
        tech_stack_fit=5.0,
        experience_fit=5.0,
        location_fit=4.0,
        salary_fit=5.0,
        industry_fit=5.0,
        reasoning="Perfect alignment with candidate's tech stack.",
    )
    tailored_res = Resume.from_dict(mock_resume.to_dict())
    mock_llm_client.tailor_resume.return_value = tailored_res

    mock_gh_client = mock.MagicMock(spec=GitHubClient)
    mock_gh_client.repo = "my-owner/my-repo"
    mock_gh_client.project_id = "proj_123"

    mock_run_git.return_value = "main-branch"

    body = (
        "**Company:** Google\n"
        "**Role:** Senior Py Dev\n"
        "**Apply URL:** https://google.com/apply\n"
        "## Job Description\n"
        "Looking for Python expert."
    )

    with mock.patch("pathlib.Path.open", mock.mock_open()) as mock_file:
        run_triage(
            issue_number=15,
            issue_title="[Google] Senior Py Dev",
            issue_body=body,
            issue_node_id="node_xyz",
            issue_labels=["triage-pending"],
            repo_path=tmp_path,
            gh_client=mock_gh_client,
            settings=mock_settings,
            resume=mock_resume,
            llm_client=mock_llm_client,
        )

        # Verify YAML overwrite
        mock_file.assert_called_once_with("w", encoding="utf-8")

    # Verify Git/Compile orchestrations
    mock_run_git.assert_any_call(["rev-parse", "--abbrev-ref", "HEAD"], cwd=tmp_path)
    mock_checkout_branch.assert_called_once_with(tmp_path, mock.ANY)
    mock_compile.assert_called_once()
    mock_commit.assert_called_once_with(
        tmp_path,
        ["resumes/resume.yaml", "resumes/resume.json", "resumes/resume.pdf"],
        "Google",
        "Senior Py Dev",
    )
    mock_push_branch.assert_called_once()
    # Checked out back to original
    mock_run_git.assert_any_call(["checkout", "-f", "main-branch"], cwd=tmp_path)

    # Verify issue comments and labels
    mock_gh_client.post_comment.assert_called_once()
    comment_arg = mock_gh_client.post_comment.call_args[0][1]
    assert "Match Approved!" in comment_arg
    assert f"Grade: {expected_label}" in comment_arg
    assert "Perfect alignment" in comment_arg
    assert "applications/google-senior-py-dev-" in comment_arg
    assert "https://github.com/my-owner/my-repo/blob/applications/" in comment_arg
    assert "https://github.com/my-owner/my-repo/tree/applications/" in comment_arg
    assert (
        "https://github.com/my-owner/my-repo/compare/main...applications/"
        in comment_arg
    )
    expected_yaml_hash = hashlib.sha256(b"resumes/resume.yaml").hexdigest()
    assert f"#diff-{expected_yaml_hash}" in comment_arg
    assert (
        '<a href="https://google.com/apply" '
        'target="_blank">Link to Posting</a>' in comment_arg
    )

    mock_gh_client.remove_label.assert_called_once_with(15, "triage-pending")
    mock_gh_client.add_labels.assert_called_once_with(
        15, [expected_label, "ready-to-apply"]
    )
    mock_gh_client.update_project_status.assert_called_once_with(
        "node_xyz", "Ready to Apply"
    )


@mock.patch("triage.compile_resume")
@mock.patch("triage.create_or_checkout_branch")
@mock.patch("triage.run_git")
def test_run_triage_tailoring_failure(
    mock_run_git: mock.MagicMock,
    mock_checkout_branch: mock.MagicMock,
    mock_compile: mock.MagicMock,
    mock_resume: Resume,
    mock_settings: Settings,
    tmp_path: pathlib.Path,
) -> None:
    """Test run_triage tailoring exception handling and branch revert safety."""
    mock_llm_client = mock.MagicMock(spec=LLMClient)
    mock_llm_client.triage_job.return_value = TriageResult(
        fit_score=4.8,
        tech_stack_fit=5.0,
        experience_fit=5.0,
        location_fit=5.0,
        salary_fit=5.0,
        industry_fit=5.0,
        reasoning="Good match",
    )
    tailored_res = Resume.from_dict(mock_resume.to_dict())
    mock_llm_client.tailor_resume.return_value = tailored_res

    # Simulate compilation failure during approved match pipeline
    mock_compile.side_effect = RuntimeError("Compilation failed")

    mock_gh_client = mock.MagicMock(spec=GitHubClient)
    mock_gh_client.repo = "my-owner/my-repo"

    mock_run_git.return_value = "main-branch"

    body = (
        "**Company:** FailCorp\n"
        "**Role:** Py Dev\n"
        "**Apply URL:** https://fail.com\n"
        "## Job Description\n"
        "Python developer needed."
    )

    with pytest.raises(RuntimeError) as exc_info:
        run_triage(
            issue_number=19,
            issue_title="[FailCorp] Py Dev",
            issue_body=body,
            issue_node_id=None,
            issue_labels=["triage-pending"],
            repo_path=tmp_path,
            gh_client=mock_gh_client,
            settings=mock_settings,
            resume=mock_resume,
            llm_client=mock_llm_client,
        )

    assert "Compilation failed" in str(exc_info.value)

    # Verify diagnostic comment posted to GitHub issue
    mock_gh_client.post_comment.assert_called_once()
    comment_arg = mock_gh_client.post_comment.call_args[0][1]
    assert "System Error" in comment_arg
    assert "Compilation failed" in comment_arg

    # Verify checkout back to main-branch was executed for safety
    mock_run_git.assert_any_call(["checkout", "-f", "main-branch"], cwd=tmp_path)


@mock.patch("triage.get_llm_client")
@mock.patch("triage.load_settings")
@mock.patch("triage.load_resume")
@mock.patch("triage.GitHubClient")
@mock.patch("triage.run_triage")
def test_main_cli_args(
    mock_run_triage: mock.MagicMock,
    mock_gh_class: mock.MagicMock,
    mock_load_resume: mock.MagicMock,
    mock_load_settings: mock.MagicMock,
    mock_get_llm: mock.MagicMock,
    mock_resume: Resume,
    mock_settings: Settings,
) -> None:
    """Test main entry point using command line arguments."""
    mock_load_settings.return_value = mock_settings
    mock_load_resume.return_value = mock_resume
    mock_llm_client = mock.MagicMock(spec=LLMClient)
    mock_get_llm.return_value = mock_llm_client

    mock_gh_client = mock.MagicMock(spec=GitHubClient)
    mock_gh_client.get_issue.return_value = {
        "title": "Fetched Title",
        "body": "Fetched Body",
        "node_id": "fetched_node",
        "labels": [{"name": "triage-pending"}],
    }
    mock_gh_class.return_value = mock_gh_client

    test_args = [
        "triage.py",
        "-i",
        "99",
        "--repo-path",
        "/my/path",
    ]

    with (
        mock.patch.object(sys, "argv", test_args),
        mock.patch.dict(
            "os.environ",
            {
                "GITHUB_TOKEN": "mock-token",
                "GITHUB_REPOSITORY": "owner/repo",
            },
        ),
    ):
        os.environ.pop("GITHUB_EVENT_PATH", None)
        main()

    mock_gh_class.assert_called_once_with(
        token="mock-token",
        repo="owner/repo",
        project_id=mock.ANY,
        status_field_name=mock.ANY,
    )
    mock_gh_client.get_issue.assert_called_once_with(99)
    mock_run_triage.assert_called_once_with(
        issue_number=99,
        issue_title="Fetched Title",
        issue_body="Fetched Body",
        issue_node_id="fetched_node",
        issue_labels=["triage-pending"],
        repo_path=pathlib.Path("/my/path").resolve(),
        gh_client=mock_gh_client,
        settings=mock_settings,
        resume=mock_resume,
        llm_client=mock.ANY,
    )


@mock.patch("triage.get_llm_client")
@mock.patch("triage.load_settings")
@mock.patch("triage.load_resume")
@mock.patch("triage.GitHubClient")
@mock.patch("triage.run_triage")
def test_main_event_path(
    mock_run_triage: mock.MagicMock,
    mock_gh_class: mock.MagicMock,
    mock_load_resume: mock.MagicMock,
    mock_load_settings: mock.MagicMock,
    mock_get_llm: mock.MagicMock,
    mock_resume: Resume,
    mock_settings: Settings,
) -> None:
    """Test main entry point parsing payload from GITHUB_EVENT_PATH."""
    mock_load_settings.return_value = mock_settings
    mock_load_resume.return_value = mock_resume
    mock_llm_client = mock.MagicMock(spec=LLMClient)
    mock_get_llm.return_value = mock_llm_client

    mock_gh_client = mock.MagicMock(spec=GitHubClient)
    mock_gh_class.return_value = mock_gh_client

    event_data = {
        "issue": {
            "number": 202,
            "title": "Google - SRE",
            "body": "Google SRE body details",
            "node_id": "node_sre_123",
            "labels": [{"name": "triage-pending"}],
        },
        "repository": {
            "full_name": "google/sre-repo",
        },
    }

    event_json = json.dumps(event_data)
    with mock.patch("pathlib.Path.open", mock.mock_open(read_data=event_json)):
        test_args = ["triage.py", "--event-path", "/path/to/event.json"]
        with (
            mock.patch.object(sys, "argv", test_args),
            mock.patch.dict(
                "os.environ",
                {
                    "GITHUB_TOKEN": "event-token",
                },
                clear=True,
            ),
        ):
            main()

    mock_gh_class.assert_called_once_with(
        token="event-token",
        repo="google/sre-repo",
        project_id=mock.ANY,
        status_field_name=mock.ANY,
    )
    mock_gh_client.get_issue.assert_not_called()
    mock_run_triage.assert_called_once_with(
        issue_number=202,
        issue_title="Google - SRE",
        issue_body="Google SRE body details",
        issue_node_id="node_sre_123",
        issue_labels=["triage-pending"],
        repo_path=pathlib.Path().resolve(),
        gh_client=mock_gh_client,
        settings=mock_settings,
        resume=mock_resume,
        llm_client=mock.ANY,
    )


@mock.patch("triage.load_settings")
def test_main_load_settings_failure(mock_load_settings: mock.MagicMock) -> None:
    """Test main exits with sys.exit(1) when loading configuration settings fails."""
    mock_load_settings.side_effect = ValueError("Invalid YAML")
    test_args = ["triage.py", "-i", "10"]

    with (
        mock.patch.object(sys, "argv", test_args),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()

    assert exc_info.value.code == 1


@mock.patch("triage.get_llm_client")
@mock.patch("triage.load_settings")
@mock.patch("triage.load_resume")
@mock.patch("triage.GitHubClient")
@mock.patch("triage.run_triage")
def test_main_issue_number_env(
    mock_run_triage: mock.MagicMock,
    mock_gh_class: mock.MagicMock,
    mock_load_resume: mock.MagicMock,
    mock_load_settings: mock.MagicMock,
    mock_get_llm: mock.MagicMock,
    mock_resume: Resume,
    mock_settings: Settings,
) -> None:
    """Test main extracts issue number from ISSUE_NUMBER environment variable."""
    mock_load_settings.return_value = mock_settings
    mock_load_resume.return_value = mock_resume
    mock_llm_client = mock.MagicMock(spec=LLMClient)
    mock_get_llm.return_value = mock_llm_client

    mock_gh_client = mock.MagicMock(spec=GitHubClient)
    mock_gh_client.get_issue.return_value = {
        "title": "Title",
        "body": "Body",
        "node_id": "node",
        "labels": [],
    }
    mock_gh_class.return_value = mock_gh_client

    test_args = ["triage.py"]

    with (
        mock.patch.object(sys, "argv", test_args),
        mock.patch.dict(
            "os.environ",
            {
                "GITHUB_TOKEN": "token",
                "GITHUB_REPOSITORY": "owner/repo",
                "ISSUE_NUMBER": "777",
            },
        ),
    ):
        os.environ.pop("GITHUB_EVENT_PATH", None)
        main()

    mock_run_triage.assert_called_once_with(
        issue_number=777,
        issue_title="Title",
        issue_body="Body",
        issue_node_id="node",
        issue_labels=[],
        repo_path=pathlib.Path().resolve(),
        gh_client=mock_gh_client,
        settings=mock_settings,
        resume=mock_resume,
        llm_client=mock.ANY,
    )


@mock.patch("triage.get_llm_client")
@mock.patch("triage.load_settings")
@mock.patch("triage.load_resume")
@mock.patch("triage.GitHubClient")
@mock.patch("triage.run_triage")
def test_main_quota_exceeded(
    mock_run_triage: mock.MagicMock,
    mock_gh_class: mock.MagicMock,
    mock_load_resume: mock.MagicMock,
    mock_load_settings: mock.MagicMock,
    mock_get_llm: mock.MagicMock,
    mock_resume: Resume,
    mock_settings: Settings,
) -> None:
    """Test main exits with sys.exit(429) when QuotaExceededError is raised."""
    mock_load_settings.return_value = mock_settings
    mock_load_resume.return_value = mock_resume
    mock_llm_client = mock.MagicMock(spec=LLMClient)
    mock_get_llm.return_value = mock_llm_client

    mock_gh_client = mock.MagicMock(spec=GitHubClient)
    mock_gh_client.get_issue.return_value = {
        "title": "Title",
        "body": "Body",
        "node_id": "node",
        "labels": [],
    }
    mock_gh_class.return_value = mock_gh_client

    mock_run_triage.side_effect = QuotaExceededError("API quota hit")
    test_args = ["triage.py", "-i", "10"]

    with (
        mock.patch.object(sys, "argv", test_args),
        mock.patch.dict(
            "os.environ",
            {
                "GITHUB_TOKEN": "token",
                "GITHUB_REPOSITORY": "owner/repo",
            },
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        main()

    assert exc_info.value.code == EXIT_QUOTA_EXCEEDED
