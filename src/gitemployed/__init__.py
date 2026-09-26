"""GitEmployed source package."""

from gitemployed.git_ops import (
    GitOpsError,
    build_commit_message,
    commit_changes,
    create_or_checkout_branch,
    generate_branch_name,
    push_branch,
    run_git,
    slugify,
)
from gitemployed.github_client import GitHubClient, GitHubClientError
from gitemployed.llm import (
    ClaudeClient,
    GeminiClient,
    LiteLLMClient,
    LLMClient,
    OpenRouterClient,
    TriageResult,
    get_llm_client,
)
from gitemployed.loader import load_resume, load_settings
from gitemployed.renderer import (
    ThemeInstallError,
    compile_resume,
    compile_resume_json,
    compile_resume_pdf,
    ensure_theme_installed,
)
from gitemployed.schema import Resume, Settings, ValidationError
from gitemployed.scraper import ScrapedJob, parse_job_row, run_scraper

__all__ = [
    "TriageResult",
    "LLMClient",
    "LiteLLMClient",
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
    "ensure_theme_installed",
    "ThemeInstallError",
    "run_scraper",
    "ScrapedJob",
    "parse_job_row",
]
