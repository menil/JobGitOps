"""JobGitOps source package."""

from jobgitops.git_ops import (
    GitOpsError,
    build_commit_message,
    commit_changes,
    create_or_checkout_branch,
    generate_branch_name,
    push_branch,
    run_git,
    slugify,
)
from jobgitops.github_client import GitHubClient, GitHubClientError
from jobgitops.llm import (
    ClaudeClient,
    GeminiClient,
    LLMClient,
    OpenRouterClient,
    TriageResult,
    get_llm_client,
)
from jobgitops.loader import load_resume, load_settings
from jobgitops.renderer import (
    compile_resume,
    compile_resume_json,
    compile_resume_pdf,
    render_resume_to_html,
)
from jobgitops.schema import Resume, Settings, ValidationError
from jobgitops.scraper import ScrapedJob, parse_job_row, run_scraper

__all__ = [
    "TriageResult",
    "LLMClient",
    "ClaudeClient",
    "GeminiClient",
    "OpenRouterClient",
    "get_llm_client",
    "GitOpsError",
    "run_git",
    "slugify",
    "generate_branch_name",
    "build_commit_message",
    "create_or_checkout_branch",
    "commit_changes",
    "push_branch",
    "GitHubClient",
    "GitHubClientError",
    "Resume",
    "Settings",
    "ValidationError",
    "load_resume",
    "load_settings",
    "compile_resume",
    "compile_resume_pdf",
    "compile_resume_json",
    "render_resume_to_html",
    "run_scraper",
    "ScrapedJob",
    "parse_job_row",
]
