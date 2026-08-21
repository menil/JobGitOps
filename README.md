# JobGitOps

A serverless, GitOps-driven job application and tracking system. JobGitOps treats the job search like software deployment: GitHub Issues are your pipeline, GitHub Projects is your Kanban board, and GitHub Actions is the automation plane that scrapes roles, AI-triages fit, and tailors your resume — all running free on GitHub's infrastructure.

## Features

- 🤖 **Automated Role Discovery**: A scheduled Actions cron (`python -m jobgitops.cli.scrape`) scrapes LinkedIn, Indeed, and ZipRecruiter via `python-jobspy`, generating search queries from your resume skills, and files new roles as GitHub Issues labeled `triage-pending`.
- 🧠 **AI Triage & Tailoring**: A two-pass LLM engine (`python -m jobgitops.cli.triage`) scores each listing against your resume across 5 dimensions (tech stack, experience, location, salary, domain). Matches above your `fit_threshold` get a tailored resume; mismatches are auto-closed with a reasons comment.
- 📄 **Resume-as-Code**: Your base resume lives in versioned YAML (`resumes/resume.yaml`, JSON Resume schema). Tailored variants are rendered to HTML and print-ready PDFs with Jinja2 + WeasyPrint on dedicated application branches — every version you send is a clean, reviewable Git diff.
- 💬 **Issue Assistant**: A tool-using agent (`python -m jobgitops.cli.respond`) answers questions on issue threads via live web research (search + fetch with cited sources), recognizes conversational status intents ("I applied", "phone screen scheduled") to apply labels, and auto-triages issues opened with a bare job URL.
- 🗂️ **Kanban Lifecycle Tracking**: Roles flow through GitHub Issues + Projects V2 (`Triage Pending → Ready to Apply → Applied → In Loop → Rejected`) with label-based automation and a label-only fallback.

---

## Getting Started

### Prerequisites

You must have the [GitHub CLI (`gh`)](https://cli.github.com/) installed and authenticated on your local machine.

### Quick Start (one-command install)

Install into a new private repository with a single command. The interactive installer will check your GitHub CLI connection, walk you through setting up credentials (including a GitHub token and a Gemini or OpenRouter LLM API key), and configure your initial resume:

```bash
npx jobgitops-installer
```

Then:

1. **Edit `resumes/resume.yaml`** in the new repo — fill in your real resume (JSON Resume format), replacing the placeholder.
2. **Commit and push to `main`.**
3. **The scraper job** will automatically start running once your resume is pushed to `main`, provided it adheres to the JSON Resume schema.

---

## The Daily Flow

Once setup is complete, your daily job search workflow operates as follows:

1. **Discovery** — The cron scrapes job boards, dedupes against the ~500 most recent roles already in your repo, and opens new candidates as issues labeled `triage-pending`.
2. **Triage** — The LLM scores each job against your base resume. Below `fit_threshold` → the issue is labeled `triage-mismatched` plus a red reason label for each dimension scored below 3 (e.g. `salary-mismatch`, `location-mismatch`), a mismatch breakdown is commented, and the issue is closed.
3. **Tailoring** — Above threshold → a dedicated branch `applications/<company>-<role>-<hash>` is created. `resumes/resume.yaml` is subtly tailored, a JSON version is generated, and a print-ready PDF is compiled. All three files are committed and pushed, and a comment links the fit score and the browser-viewable PDF.
4. **Apply & Track** — Review the diff, submit the PDF, then label the issue `applied` to move the card to `Applied` on your board. Track `in-loop` / `rejected` from there.

---

## Configuration

### `resumes/resume.yaml`

Your base resume, conforming to the [JSON Resume Schema](https://jsonresume.org/schema/). This is the source of truth for scraping queries, fit evaluation, and tailoring:

```yaml
basics:
  name: "John Doe"
  email: "john@example.com"
  phone: "+1-555-555-0100"
  url: "https://johndoe.dev"
  summary: "A brief summary..."
work:
  - name: "Acme Corp"
    position: "Senior Engineer"
    startDate: "2022-01-01"
    endDate: "2024-06-01"
    highlights:
      - "Built scalable services..."
skills:
  - name: "Languages"
    keywords:
      - "TypeScript"
      - "Python"
```

Overwrite this file with your own work history, education, and skills. The rendering templates (`resumes/template.html`, `resumes/style.css`) stay on the branch alongside every tailored variant.

### `config/settings.yaml`

Controls search preferences and the triage threshold:

```yaml
# Minimum fit score (1.0 to 5.0) required to tailor a resume and apply.
fit_threshold: 3.5

search:
  enabled: true                         # Enable or disable daily scraping
  work_preference: "hybrid"             # remote | onsite | hybrid
  job_type: "fulltime"                  # fulltime | contract | parttime | internship
  platforms:
    - linkedin                          # Job boards to search
  hours_old: 24                         # Only jobs posted in the last N hours

# Optional Issue Assistant research settings.
# research:
#   search_provider: duckduckgo       # duckduckgo | tavily | brave
#   max_results: 5
#   max_iterations: 6                 # agent tool-loop cap
#   max_context_comments: 10          # recent comments fed to the model
#   timeout_seconds: 15               # per-request fetch timeout
#   total_timeout_seconds: 30         # total request budget (incl. redirects)
#   max_redirects: 5
#   max_content_bytes: 1048576        # 1 MiB
#   request_delay: 1.0                # politeness delay between DDG requests
#   use_jina_reader: true             # fallback for JS-heavy / blocked pages
#   max_jina_calls: 5                 # Jina fallback fetches per agent run
#   block_private_ips: true
#   model: ""                         # optional override; empty = provider default
```

- **`custom_queries`**: When non-empty, the scraper uses these queries instead of auto-generating them from your resume — useful for targeting new stacks or domains.
- **`projects_v2`**: When configured, issue cards move through your Projects V2 board automatically and column moves are reflected back as labels. Without it (or while the placeholder is in place), the system falls back to repository labels (`ready-to-apply`, `applied`, `in-loop`, `rejected`).

---

## Troubleshooting

- **Check workflow logs**: every run is visible under the **Actions** tab, with the exact command and error output per step.
- **Scraping works but nothing is triaged**: confirm your LLM provider key is configured in repository secrets; otherwise triage fails on every run.
- **LLM quota / rate limit (Exit 75)**: the daily scraper stops triaging for the day when the LLM provider reports quota exhaustion; per-issue failures post a comment on the issue. To recover, wait for the daily quota reset and manually trigger the daily scrape workflow via Actions **workflow_dispatch**, or remove and re-apply the `triage-pending` label on stalled issues.
- **Setup pending / No jobs scraped**: verify `resumes/resume.yaml` has been updated with real content and no longer contains the `__JOBGITOPS_SETUP_PENDING__` sentinel string.
- **`applied` label set but the board card never moves**: the Projects V2 move only happens when `projects_v2` is configured in `config/settings.yaml` with a real `PVT_...` node ID; otherwise the label alone tracks state.
- **Board moves but the label never updates (or vice-versa)**: verify your configuration has Projects V2 enabled. To reconcile out-of-sync board columns and issue labels, see the manual sync procedures in [DEVELOPMENT.md](DEVELOPMENT.md#project-sync-and-reconciliation-cli).
- **Web research or job URL fetch fails**: if pages fail to parse due to anti-bot protection or rate limiting, add a Jina API key to your secrets or configure a dedicated search provider in `config/settings.yaml`.
- **`custom_queries` / `fit_threshold` seem ignored**: verify `custom_queries` is a top-level key in `config/settings.yaml` (a sibling of `search`), not nested under it.

---

## Upgrades & Maintenance

Updates to the core execution engine arrive automatically via the shared container image. Updates to the repository files (GitHub Actions workflow configurations, label definitions) are optional and can be synchronized by running the manual templates script: `scripts/sync-template.sh`. Pointers to release procedures and end-to-end suite guidelines can be found in [RELEASING.md](RELEASING.md) and [scripts/e2e.md](scripts/e2e.md).

---

## Architecture & Development

For technical details about how JobGitOps works internally, including architecture diagrams, GitHub Actions workflows, issue label definitions, local environment setup, and repository layout, please see the [DEVELOPMENT.md](DEVELOPMENT.md) guide.

Detailed developer environment guidelines and validation/issue-tracking helper instructions can also be found in [AGENTS.md](AGENTS.md).
