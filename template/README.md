# JobGitOps

[![Setup](https://img.shields.io/endpoint?url=https://gist.githubusercontent.com/__OWNER__/__GIST_ID__/raw/setup-status.json&maxAge=0)](https://github.com/__OWNER__/__REPO__/actions/workflows/check-setup.yml)
[![Resume Format](https://img.shields.io/endpoint?url=https://gist.githubusercontent.com/__OWNER__/__GIST_ID__/raw/format-status.json&maxAge=0)](https://github.com/__OWNER__/__REPO__/actions/workflows/format-resume.yml)
[![Daily Job Scraper](https://img.shields.io/endpoint?url=https://gist.githubusercontent.com/__OWNER__/__GIST_ID__/raw/scrape-status.json&maxAge=0)](https://github.com/__OWNER__/__REPO__/actions/workflows/scrape-jobs.yml)
[![Triage & Tailor](https://img.shields.io/endpoint?url=https://gist.githubusercontent.com/__OWNER__/__GIST_ID__/raw/triage-status.json&maxAge=0)](https://github.com/__OWNER__/__REPO__/actions/workflows/triage-issue.yml)
[![Issue Assistant](https://img.shields.io/endpoint?url=https://gist.githubusercontent.com/__OWNER__/__GIST_ID__/raw/respond-status.json&maxAge=0)](https://github.com/__OWNER__/__REPO__/actions/workflows/respond-issue.yml)


A serverless, GitOps-driven job application and tracking system. GitHub Issues are your pipeline, GitHub Projects is your Kanban board, and GitHub Actions is the automation plane that scrapes roles, AI-triages fit, and tailors your resume — all running free on GitHub's infrastructure.

## Getting Started

1. **Replace the placeholder resume** in `resumes/resume.yaml` (a YAML file in [JSON Resume](https://jsonresume.org/schema) format) with your real work history, then commit and push to `main`. The daily cron takes over from there.

## Configuration

Per-user options live in `config/settings.yaml` and are edited directly in that file — same workflow as replacing the placeholder resume. Notable defaults:

- `search.work_preference` defaults to `hybrid` (also accepts `remote` or `onsite`)
- Your resume's `basics.location` fields (`city`, `state`, `countryCode`) drive the search location — include all three for best results with `hybrid`/`onsite` modes
- `search.enabled` (set to `false` to pause daily scraping)
- `fit_threshold` (minimum fit score, `1.0`–`5.0`, default `3.5`)

## Docs

Full setup, configuration, and workflow documentation: <https://github.com/menil/jobgitops>

## Your Job Search on GitHub

### [Issues](https://github.com/__OWNER__/__REPO__/issues)

Each scraped job listing becomes a GitHub Issue. The AI triage engine scores every listing against your resume, tailors a fit resume for strong matches, and closes mismatches with a reasons comment.

### [Projects](https://github.com/__OWNER__/__REPO__/projects)

An optional Kanban board for tracking your application lifecycle (Triage Pending → Ready to Apply → Applied → In Loop → Rejected). Requires [Project V2 setup](https://github.com/menil/jobgitops#enabling-projects-v2); without it, issue labels track state instead.

### [Secrets & Variables](https://github.com/__OWNER__/__REPO__/settings/secrets/actions)

Configure these under **Settings > Secrets and variables > Actions**:

#### Secrets

| Secret | Purpose |
| --- | --- |
| `GEMINI_API_KEY` | Gemini LLM key for triage and tailoring ([Google AI Studio](https://aistudio.google.com/)) — one of Gemini, OpenRouter, or Claude is required |
| `OPENROUTER_API_KEY` | OpenRouter LLM key — also powers automated PR reviews |
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude provider credentials: Claude Code subscription token (`claude setup-token`) or standard Anthropic API key |
| `GH_PAT` | Optional. GitHub Personal Access Token. Enables status badges via Gist, and optionally Projects V2 Kanban board automation; falls back to GITHUB_TOKEN without it |
| `TAVILY_API_KEY` | Optional. Enables the Tavily search provider for the Issue Assistant's web research |
| `BRAVE_API_KEY` | Optional. Enables the Brave search provider for the Issue Assistant's web research |
| `JINA_API_KEY` | Optional. Raises page-fetch rate limits from 20 to 500 RPM for JS-heavy job boards |

#### Variables (Optional)

Configure these under the **Variables** tab:

| Variable | Default | Purpose |
| --- | --- | --- |
| `LLM_PROVIDER` | *auto-detected* | `gemini`, `openrouter`, or `claude` (auto-detected from keys when unset) |
| `OPENROUTER_MODEL` | `openrouter/free` | OpenRouter model (requires provider prefix, e.g., `openrouter/free` or `google/gemini-2.5-flash`). |
| `GEMINI_MODEL` | `models/gemini-2.5-flash` | Gemini model (e.g., `models/gemini-2.5-flash` or `gemini-2.5-flash`) |
| `CLAUDE_MODEL` | `claude-sonnet-5` | Claude model (e.g., `claude-sonnet-5` or `claude-haiku-4-5-20251001`) |

### [Actions](https://github.com/__OWNER__/__REPO__/actions)

| Workflow | What it does |
| --- | --- |
| `scrape-jobs.yml` | Daily cron that discovers new roles from LinkedIn, Indeed, and ZipRecruiter, then opens issues |
| `triage-issue.yml` | AI-scores each listing against your resume and tailors a fit resume if it passes the threshold |
| `respond-issue.yml` | Answers questions on issue threads via live web research, and applies status labels from conversation |
| `status-transition.yml` | Moves your board card when you apply a lifecycle label (`applied`, `in-loop`, ...), and closes the issue for terminal labels (`rejected`, `triage-mismatched`) while keeping their columns distinct |
| `project-status-sync.yml` | Reverse sync: applies the matching label when you drag a card to a new column |
