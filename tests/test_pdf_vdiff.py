"""Integration tests for PDF visual diffing and triage workflow."""

import pathlib
from unittest import mock

import pytest

from gitemployed.cli.triage import run_triage
from gitemployed.github_client import GitHubClient
from gitemployed.llm import LLMClient, TriageResult
from gitemployed.renderer import generate_pdf_diff
from gitemployed.schema import Resume, Settings


@pytest.fixture
def base_resume() -> Resume:
    """Provide a base resume for diff testing."""
    return Resume.from_dict(
        {
            "basics": {
                "name": "Alex Smith",
                "email": "alex@example.com",
                "summary": "Full Stack Engineer",
                "location": {
                    "city": "San Francisco",
                    "region": "CA",
                    "countryCode": "US",
                },
            },
            "work": [
                {
                    "name": "Tech Corp",
                    "position": "Software Engineer",
                    "startDate": "2021-01-01",
                    "endDate": "2024-01-01",
                    "highlights": ["Built backend APIs in Go and Python."],
                }
            ],
            "skills": [{"name": "Languages", "keywords": ["Python", "Go"]}],
        }
    )


@pytest.fixture
def tailored_resume(base_resume: Resume) -> Resume:
    """Provide a tailored resume with modified highlights."""
    data = base_resume.to_dict()
    data["work"][0]["highlights"] = [
        "Architected distributed systems in Go and Python with 99.99% uptime."
    ]
    return Resume.from_dict(data)


def test_generate_pdf_diff_with_custom_options(tmp_path: pathlib.Path) -> None:
    """Verify generate_pdf_diff respects custom themes and granularity."""
    base_pdf = tmp_path / "base.pdf"
    tailored_pdf = tmp_path / "tailored.pdf"
    out_pdf = tmp_path / "out" / "custom_diff.pdf"

    base_pdf.write_bytes(b"%PDF-1.4 base")
    tailored_pdf.write_bytes(b"%PDF-1.4 tailored")

    with (
        mock.patch(
            "shutil.which",
            side_effect=lambda cmd: (
                "/usr/local/bin/pdf-vdiff" if cmd == "pdf-vdiff" else None
            ),
        ),
        mock.patch("gitemployed.renderer.subprocess.run") as mock_run,
    ):
        mock_run.return_value = mock.MagicMock(returncode=1, stderr="", stdout="")
        result = generate_pdf_diff(
            base_pdf,
            tailored_pdf,
            out_pdf,
            theme="intellij",
            granularity="line",
        )
        assert result == out_pdf

        mock_run.assert_called_once()
        cmd = mock_run.call_args.args[0]
        assert "--theme" in cmd
        assert "intellij" in cmd
        assert "--granularity" in cmd
        assert "line" in cmd


@mock.patch("gitemployed.cli.triage.generate_pdf_diff")
@mock.patch("gitemployed.cli.triage.compile_resume")
@mock.patch("gitemployed.cli.triage.ensure_theme_installed", return_value="theme-pkg")
@mock.patch("gitemployed.cli.triage.create_or_checkout_branch")
@mock.patch("gitemployed.cli.triage.run_git")
@mock.patch("gitemployed.cli.triage.commit_changes")
@mock.patch("gitemployed.cli.triage.push_branch")
def test_full_triage_visual_diff_integration(
    mock_push: mock.MagicMock,
    mock_commit: mock.MagicMock,
    mock_run_git: mock.MagicMock,
    mock_checkout: mock.MagicMock,
    mock_ensure_theme: mock.MagicMock,
    mock_compile: mock.MagicMock,
    mock_diff: mock.MagicMock,
    base_resume: Resume,
    tailored_resume: Resume,
    tmp_path: pathlib.Path,
) -> None:
    """Verify full end-to-end triage coordination with visual diff."""
    mock_run_git.return_value = "main"
    mock_llm = mock.MagicMock(spec=LLMClient)
    mock_llm.triage_job.return_value = TriageResult(
        fit_score=4.9,
        tech_stack_fit=5.0,
        experience_fit=5.0,
        location_fit=4.8,
        salary_fit=4.8,
        industry_fit=5.0,
        reasoning="Exceptional match for senior role.",
    )
    mock_llm.tailor_resume.return_value = tailored_resume

    mock_gh = mock.MagicMock(spec=GitHubClient)
    mock_gh.repo = "menil/GitEmployed"
    mock_gh.project_id = "PVT_123"

    settings = Settings.from_dict(
        {
            "fit_threshold": 3.8,
            "theme": "@jsonresume/jsonresume-theme-professional@1.0.22",
        }
    )

    issue_body = (
        "**Company:** Netflix\n"
        "**Role:** Senior Distributed Systems Engineer\n"
        "**Location:** Remote - US\n"
        "**Salary:** $250,000 - $350,000\n"
        "**Source:** linkedin\n"
        "**Apply URL:** https://jobs.netflix.com/jobs/123456\n"
        "## Job Description\n"
        "Build resilient streaming backend services."
    )

    run_triage(
        issue_number=42,
        issue_title="[Netflix] Senior Distributed Systems Engineer",
        issue_body=issue_body,
        issue_node_id="node_42",
        issue_labels=["triage-pending"],
        repo_path=tmp_path,
        gh_client=mock_gh,
        settings=settings,
        resume=base_resume,
        llm_client=mock_llm,
    )

    # 1. Diff generation called
    mock_diff.assert_called_once()

    # 2. Commit includes tailored and diff PDFs
    mock_commit.assert_called_once()
    committed_files = mock_commit.call_args.args[1]
    assert "resumes/alex_smith_resume.pdf" in committed_files
    assert "resumes/alex_smith_resume_diff.pdf" in committed_files

    # 3. Comment includes both links
    mock_gh.post_comment.assert_called_once()
    comment = mock_gh.post_comment.call_args.args[1]
    assert "- **Tailored Resume PDF:** [View/Download PDF](" in comment
    assert "/resumes/alex_smith_resume.pdf)" in comment
    assert "- **Visual Resume Diff:** [View Visual Diff PDF](" in comment
    assert "/resumes/alex_smith_resume_diff.pdf)" in comment
