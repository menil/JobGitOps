"""Job triage and tailoring coordinator for JobGitOps.

Parses issue details, triages the job description against the base resume,
and either rejects/closes the issue or tailors the resume on a Git branch.
"""

import argparse
import hashlib
import json
import logging
import os
import pathlib
import re
import sys
from typing import Any

from jobgitops.git_ops import (
    commit_changes,
    create_or_checkout_branch,
    generate_branch_name,
    push_branch,
    run_git,
)
from jobgitops.github_client import GitHubClient
from jobgitops.llm import LLMClient, QuotaExceededError, TriageResult, get_llm_client
from jobgitops.loader import load_resume, load_settings, render_resume_yaml
from jobgitops.renderer import compile_resume

logger = logging.getLogger("jobgitops.triage")

# POSIX exit code for temporary quota/rate-limit failure (EX_TEMPFAIL)
EXIT_QUOTA_EXCEEDED = 75

# Pre-compiled regex patterns for robust job detail parsing
COMPANY_REGEX = re.compile(r"\*\*[Cc]ompany:?\*\*:?\s*(.*)")
ROLE_REGEX = re.compile(r"\*\*[Rr]ole:?\*\*:?\s*(.*)")
LOCATION_REGEX = re.compile(r"\*\*[Ll]ocation:?\*\*:?\s*(.*)")
SALARY_REGEX = re.compile(r"\*\*[Ss]alary:?\*\*:?\s*(.*)")
SOURCE_REGEX = re.compile(r"\*\*[Ss]ource:?\*\*:?\s*(.*)")
APPLY_URL_REGEX = re.compile(r"\*\*[Aa]pply\s*[Uu][Rr][Ll]:?\*\*:?\s*(.*)")
MARKDOWN_LINK_REGEX = re.compile(r"^\[.*?\]\((.*?)\)$")
TITLE_COMPANY_ROLE_REGEX = re.compile(r"^\[(.*?)\]\s*(.*)$")


def parse_job_details(body: str | None, title: str | None = "") -> dict[str, str]:
    """Parse job details from the issue body or title.

    Args:
        body: The markdown body of the issue.
        title: The title of the issue.

    Returns:
        Dictionary of parsed job details (company, role, location, salary,
        source, apply_url, description).
    """
    body = body or ""
    title = title or ""

    company = ""
    role = ""
    location = "Remote"
    salary = "Not specified"
    source = ""
    apply_url = ""
    description = ""

    # Parse details from body using regex
    company_match = COMPANY_REGEX.search(body)
    if company_match:
        company = company_match.group(1).strip()

    role_match = ROLE_REGEX.search(body)
    if role_match:
        role = role_match.group(1).strip()

    location_match = LOCATION_REGEX.search(body)
    if location_match:
        location = location_match.group(1).strip()

    salary_match = SALARY_REGEX.search(body)
    if salary_match:
        salary = salary_match.group(1).strip()

    source_match = SOURCE_REGEX.search(body)
    if source_match:
        source = source_match.group(1).strip()

    apply_url_match = APPLY_URL_REGEX.search(body)
    if apply_url_match:
        apply_url = apply_url_match.group(1).strip()
        # Clean markdown link format [Text](url) or just url
        link_match = MARKDOWN_LINK_REGEX.match(apply_url)
        if link_match:
            apply_url = link_match.group(1).strip()

    # Extract Job Description, split at most once
    desc_split = re.split(
        r"##\s*Job\s*Description", body, maxsplit=1, flags=re.IGNORECASE
    )
    description = desc_split[1].strip() if len(desc_split) > 1 else body.strip()

    # Fallbacks from issue title if company or role are missing
    if not company or not role:
        title_match = TITLE_COMPANY_ROLE_REGEX.match(title)
        if title_match:
            if not company:
                company = title_match.group(1).strip()
            if not role:
                role = title_match.group(2).strip()
        elif " at " in title:
            parts = title.split(" at ", 1)
            if not role:
                role = parts[0].strip()
            if not company:
                company = parts[1].strip()
        else:
            if not role:
                role = title.strip()

    if not company:
        company = "Unknown Company"
    if not role:
        role = "Unknown Role"

    # Validate/Sanitize Apply URL to prevent markdown injection
    if apply_url and (
        not (apply_url.startswith("http://") or apply_url.startswith("https://"))
        or any(char in apply_url for char in " []\"'<>")
    ):
        logger.warning(
            "apply_url is invalid or unsafe: %s",
            apply_url,
        )
        apply_url = ""

    return {
        "company": company,
        "role": role,
        "location": location,
        "salary": salary,
        "source": source,
        "apply_url": apply_url,
        "description": description,
    }


def _handle_mismatch(
    issue_number: int,
    issue_node_id: str | None,
    issue_labels: list[str],
    triage_res: TriageResult,
    fit_threshold: float,
    gh_client: GitHubClient,
) -> None:
    """Handle the mismatch workflow path (fit score below threshold)."""
    logger.info("Fit score below threshold. Rejecting job.")

    # Post comment with mismatch reason
    comment_body = (
        f"### AI Triage: Mismatch Detected "
        f"(Fit Score: {triage_res.fit_score:.1f}/{fit_threshold:.1f})\n\n"
        f"**Reasoning:**\n{triage_res.reasoning}\n\n"
        f"#### Score Breakdown:\n"
        f"- **Tech Stack Match:** {triage_res.tech_stack_fit:.1f}/5.0\n"
        f"- **Experience & Years Fit:** {triage_res.experience_fit:.1f}/5.0\n"
        f"- **Location & Timezone Suitability:** "
        f"{triage_res.location_fit:.1f}/5.0\n"
        f"- **Salary Alignment:** {triage_res.salary_fit:.1f}/5.0\n"
        f"- **Industry Domain Familiarity:** {triage_res.industry_fit:.1f}/5.0\n"
    )
    gh_client.post_comment(issue_number, comment_body)

    # Update labels
    if "triage-pending" in issue_labels:
        gh_client.remove_label(issue_number, "triage-pending")
    gh_client.add_labels(issue_number, ["triage-mismatched"])

    # Close issue
    gh_client.close_issue(issue_number)

    # Update Projects V2
    if issue_node_id and gh_client.project_id:
        logger.info("Updating Project status to 'Mismatched/Closed'")
        gh_client.update_project_status(issue_node_id, "Mismatched/Closed")


def _create_tailored_application_branch(
    repo_path: pathlib.Path,
    branch_name: str,
    tailored_resume: Any,
    job_details: dict[str, str],
) -> None:
    """Checkout tailored branch, write, compile, commit, and push changes."""
    # Track the original branch to return back cleanly
    try:
        original_branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path)
    except Exception as e:
        logger.warning(
            "Could not determine current git branch, defaulting to 'main': %s",
            e,
        )
        original_branch = "main"

    branch_switched = False
    try:
        # Checkout/create target branch
        create_or_checkout_branch(repo_path, branch_name)
        branch_switched = True

        # Write tailored resume files
        resumes_dir = repo_path / "resumes"
        resumes_dir.mkdir(parents=True, exist_ok=True)

        # 1. Overwrite resume.yaml
        yaml_content = render_resume_yaml(tailored_resume)
        with (resumes_dir / "resume.yaml").open("w", encoding="utf-8") as f:
            f.write(yaml_content)

        # 2. Compile JSON & PDF
        compile_resume(
            tailored_resume,
            resumes_dir / "template.html",
            resumes_dir / "resume.pdf",
            resumes_dir / "resume.json",
        )

        # Commit and push
        commit_changes(
            repo_path,
            [
                "resumes/resume.yaml",
                "resumes/resume.json",
                "resumes/resume.pdf",
            ],
            job_details["company"],
            job_details["role"],
        )
        push_branch(repo_path, branch_name)

    finally:
        # Only revert checkout if branch was actually switched
        if branch_switched and branch_name != original_branch:
            logger.info("Returning to original branch: %s", original_branch)
            try:
                # Use force checkout to clean any partially modified states
                run_git(["checkout", "-f", original_branch], cwd=repo_path)
            except Exception as e:
                logger.error(
                    "Failed to checkout back to original branch '%s': %s",
                    original_branch,
                    e,
                )


def _handle_approved_match(
    issue_number: int,
    issue_node_id: str | None,
    issue_labels: list[str],
    job_details: dict[str, str],
    triage_res: TriageResult,
    repo_path: pathlib.Path,
    gh_client: GitHubClient,
    settings: Any,
    resume: Any,
    llm_client: LLMClient,
) -> None:
    """Handle the approved match and resume tailoring branch pipeline."""
    logger.info("Fit score meets threshold. Tailoring resume...")

    try:
        # Pass 2: Resume Tailoring
        tailored_resume = llm_client.tailor_resume(job_details["description"], resume)

        # Git Branch & Commit/Push Pipeline
        branch_name = generate_branch_name(
            job_details["company"], job_details["role"], job_details["apply_url"]
        )
        logger.info("Target application branch: %s", branch_name)

        _create_tailored_application_branch(
            repo_path=repo_path,
            branch_name=branch_name,
            tailored_resume=tailored_resume,
            job_details=job_details,
        )

        # Post approval comment
        pdf_blob_url = (
            f"https://github.com/{gh_client.repo}/blob/{branch_name}/resumes/resume.pdf"
        )
        yaml_path = "resumes/resume.yaml"
        yaml_hash = hashlib.sha256(yaml_path.encode("utf-8")).hexdigest()
        comment_body = (
            f"### AI Triage: Match Approved! "
            f"(Fit Score: {triage_res.fit_score:.1f}/{settings.fit_threshold:.1f})\n\n"
            f"**Reasoning:**\n{triage_res.reasoning}\n\n"
            f"#### Score Breakdown:\n"
            f"- **Tech Stack Match:** {triage_res.tech_stack_fit:.1f}/5.0\n"
            f"- **Experience & Years Fit:** {triage_res.experience_fit:.1f}/5.0\n"
            f"- **Location & Timezone Suitability:** "
            f"{triage_res.location_fit:.1f}/5.0\n"
            f"- **Salary Alignment:** {triage_res.salary_fit:.1f}/5.0\n"
            f"- **Industry Domain Familiarity:** "
            f"{triage_res.industry_fit:.1f}/5.0\n\n"
            f"---\n\n"
            f"### Resume Tailored Successfully\n"
            f"- **Application Branch:** [{branch_name}]"
            f"(https://github.com/{gh_client.repo}/tree/{branch_name})\n"
            f"- **Resume YAML Diff:** [Compare Changes]"
            f"(https://github.com/{gh_client.repo}/compare/main...{branch_name}"
            f"#diff-{yaml_hash})\n"
            f"- **Tailored Resume PDF:** [View/Download PDF]({pdf_blob_url})\n"
            f'- **Apply URL:** <a href="{job_details["apply_url"]}" '
            f'target="_blank">Link to Posting</a>\n'
        )
        gh_client.post_comment(issue_number, comment_body)

        # Update labels
        if "triage-pending" in issue_labels:
            gh_client.remove_label(issue_number, "triage-pending")
        gh_client.add_labels(issue_number, ["grade-A", "ready-to-apply"])

        # Update Projects V2 status
        if issue_node_id and gh_client.project_id:
            logger.info("Updating Project status to 'Ready to Apply'")
            gh_client.update_project_status(issue_node_id, "Ready to Apply")

    except Exception as e:
        logger.error("Error occurred during approved match tailoring: %s", e)
        # Post diagnostic failure comment to the issue
        err_comment = (
            f"### AI Triage & Tailoring: System Error\n\n"
            f"An error occurred while compiling or committing the tailored resume:\n"
            f"```\n{str(e)}\n```\n"
            f"Please inspect the workflow execution log for detailed traceback info."
        )
        try:
            gh_client.post_comment(issue_number, err_comment)
        except Exception as post_err:
            logger.error("Failed to post failure comment to issue: %s", post_err)
        raise


def run_triage(
    issue_number: int,
    issue_title: str,
    issue_body: str,
    issue_node_id: str | None,
    issue_labels: list[str],
    repo_path: pathlib.Path,
    gh_client: GitHubClient,
    settings: Any,
    resume: Any,
    llm_client: LLMClient,
) -> None:
    """Orchestrate the triage and tailoring process for a single issue.

    Args:
        issue_number: The issue number to triage.
        issue_title: The title of the issue.
        issue_body: The markdown body of the issue.
        issue_node_id: The node ID of the issue (needed for Projects V2).
        issue_labels: List of label names on the issue.
        repo_path: Path to the local git repository.
        gh_client: The GitHub client to interact with the repository.
        settings: Parsed Settings object.
        resume: Parsed base Resume object.
        llm_client: Instantiated LLM client.
    """
    logger.info("Starting triage for issue #%d: %s", issue_number, issue_title)

    # 1. Parse details from issue body
    job_details = parse_job_details(issue_body, issue_title)
    logger.info(
        "Parsed job details: %s at %s",
        job_details["role"],
        job_details["company"],
    )

    # 2. Triage evaluation
    logger.info("Evaluating job description with LLM...")
    triage_res = llm_client.triage_job(job_details["description"], resume)
    logger.info(
        "Triage fit score: %.1f (threshold: %.1f)",
        triage_res.fit_score,
        settings.fit_threshold,
    )

    # 3. Act on fit score
    if triage_res.fit_score < settings.fit_threshold:
        _handle_mismatch(
            issue_number=issue_number,
            issue_node_id=issue_node_id,
            issue_labels=issue_labels,
            triage_res=triage_res,
            fit_threshold=settings.fit_threshold,
            gh_client=gh_client,
        )
    else:
        _handle_approved_match(
            issue_number=issue_number,
            issue_node_id=issue_node_id,
            issue_labels=issue_labels,
            job_details=job_details,
            triage_res=triage_res,
            repo_path=repo_path,
            gh_client=gh_client,
            settings=settings,
            resume=resume,
            llm_client=llm_client,
        )


def main() -> None:
    """CLI entry point for triage and tailoring coordinator."""
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    parser = argparse.ArgumentParser(description="Triage and tailoring coordinator.")
    parser.add_argument(
        "--issue", "-i", type=int, help="GitHub issue number to triage."
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

    # Load configurations and base resume
    try:
        settings = load_settings(repo_path / "config/settings.yaml")
        resume = load_resume(repo_path / "resumes/resume.yaml")
    except Exception as e:
        logger.error("Failed to load settings or base resume configuration: %s", e)
        sys.exit(1)

    # Load environmental tokens and details
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")

    # Read from event payload if available
    event_path = args.event_path or os.environ.get("GITHUB_EVENT_PATH")

    issue_number = args.issue or (
        int(os.environ.get("ISSUE_NUMBER")) if os.environ.get("ISSUE_NUMBER") else None
    )
    issue_title = ""
    issue_body = ""
    issue_node_id = None
    issue_labels: list[str] = []

    if event_path:
        logger.info("Loading GitHub event payload from: %s", event_path)
        try:
            with pathlib.Path(event_path).open("r", encoding="utf-8") as f:
                event = json.load(f)
            issue_data = event.get("issue", {})
            if not issue_number:
                issue_number = issue_data.get("number")
            issue_title = issue_data.get("title", "")
            issue_body = issue_data.get("body", "")
            issue_node_id = issue_data.get("node_id")

            # Extract labels from event payload
            labels_raw = issue_data.get("labels", [])
            issue_labels = [
                lbl["name"]
                for lbl in labels_raw
                if isinstance(lbl, dict) and "name" in lbl
            ]

            # Repository can be resolved from payload repository full_name
            # if env is missing
            if not repo and "repository" in event:
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
        logger.error("No issue number or event payload specified to triage.")
        sys.exit(1)

    # Initialize GitHub client
    project_id = settings.projects_v2.project_id if settings.projects_v2 else None
    status_field = (
        settings.projects_v2.status_field_name if settings.projects_v2 else "Status"
    )

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

    # Fetch issue details if we didn't get them from a payload file
    if not issue_title or not issue_body:
        logger.info("Fetching issue details for #%d from GitHub API...", issue_number)
        try:
            issue = gh_client.get_issue(issue_number)
            issue_title = issue.get("title", "")
            issue_body = issue.get("body", "")
            issue_node_id = issue.get("node_id")

            # Extract labels from API response
            labels_raw = issue.get("labels", [])
            issue_labels = [
                lbl["name"]
                for lbl in labels_raw
                if isinstance(lbl, dict) and "name" in lbl
            ]
        except Exception as e:
            logger.error("Failed to fetch issue #%d: %s", issue_number, e)
            sys.exit(1)

    # Execute triage orchestration
    try:
        llm_client = get_llm_client()
        run_triage(
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            issue_node_id=issue_node_id,
            issue_labels=issue_labels,
            repo_path=repo_path,
            gh_client=gh_client,
            settings=settings,
            resume=resume,
            llm_client=llm_client,
        )
    except QuotaExceededError as e:
        logger.warning("LLM API quota exceeded: %s", e)
        sys.exit(EXIT_QUOTA_EXCEEDED)
    except Exception as e:
        logger.exception("Triage coordinator failed: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
