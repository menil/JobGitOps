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

### Enabling Projects V2

Enabling the Projects V2 synchronization is a three-step process:

1. **Get your project's node ID** — the `PVT_...` identifier, *not* the `N` number shown in the board URL. In a terminal:

   ```bash
   gh api graphql -f query='query { viewer { projectV2(number: 1) { id } } }'
   ```

   (Replace `1` with your board's number.) Copy the returned `"PVT_..."` string into `config/settings.yaml`.

2. **Add a `GH_PAT` secret** with `Projects` (read) plus `Issues`/`Contents`/`Pull requests` (write) scopes — see [API Key Setup](#api-key-setup). 

   > [!IMPORTANT]
   > This token is mandatory for full two-way sync: the built-in `GITHUB_TOKEN` cannot write to Projects V2, so without it the workflows fall back to label-only tracking even when `project_id` is set.

3. **Sync the board and options** — the lifecycle columns (`Triage Pending`, `Ready to Apply`, `Applied`, `In Loop`, `Offer Received`, `Rejected`, `Mismatched/Closed`) must exist on your Status field before cards can move. 

   Since user-provisioned repositories do not run the Python source code locally, you should trigger these commands via GitHub Actions **workflow_dispatch** under the **Actions** tab on your repository (triggering the `Sync Project Status to Label` workflow).
   
   If you are a developer testing inside a clone of the core engine repo, you can run them locally in a nix/devenv shell:
   ```bash
   devenv shell -- python -m jobgitops.cli.project_sync field-options   # create missing options
   devenv shell -- python -m jobgitops.cli.project_sync backfill --reverse   # one-time reconciliation
   ```

   > [!WARNING]
   > `backfill` moves every card to the column its labels dictate, overwriting manual column positions, and `field-options --prune` permanently deletes Status options not in the lifecycle model. Run them only when you intend labels to be the source of truth for the board.

   **How the two-way sync works:** labels are the single source of truth in the project's state model (see [DEVELOPMENT.md](DEVELOPMENT.md#issue-labels--lifecycle) for details). Adding a lifecycle label moves the card; moving a card applies the matching label; `backfill` converges the whole board idempotently, and `backfill --reverse` recovers any column move whose webhook event was dropped.

---

## API Key Setup

Add the following under **Settings > Secrets and variables > Actions** in your fork.

### Secrets

| Secret | Required | Purpose |
| --- | --- | --- |
| `GEMINI_API_KEY` | One of the two | Gemini provider key (from [Google AI Studio](https://aistudio.google.com/)) |
| `OPENROUTER_API_KEY` | One of the two | OpenRouter provider key — also powers the automated PR review |
| `GH_PAT` | Optional | GitHub Personal Access Token. Serves as the single pipeline token for Projects V2, Gist status badges, and repository mutations. Falls back to `GITHUB_TOKEN` only when **unset** |
| `TAVILY_API_KEY` | Optional | Enables the `tavily` search provider for the Issue Assistant's web research |
| `BRAVE_API_KEY` | Optional | Enables the `brave` search provider for the Issue Assistant's web research |
| `JINA_API_KEY` | Optional | Free key (jina.ai) for the Jina Reader fallback on JS-heavy job boards; raises the anonymous 20 RPM limit to 500 RPM |

> [!NOTE]
> At least one of `GEMINI_API_KEY` or `OPENROUTER_API_KEY` is required for triage. If `GH_PAT` is omitted, the workflows use the built-in `GITHUB_TOKEN` (enough for issues, contents, and PRs; Projects V2 automation and Gist status badges then degrade/skip) — provided **Settings > Actions > General > Workflow permissions** is set to *Read and write permissions*. Note that `${{ secrets.A || secrets.B }}` selects `GH_PAT` whenever it is non-empty: a stale, revoked, or under-scoped token is used preferentially and fails rather than falling back, so replace — don't just remove — a bad token. We recommend using **Fine-Grained Personal Access Tokens (Beta)** scoped strictly to your job search repository.

### Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `LLM_PROVIDER` | auto-detected | `gemini` or `openrouter` (auto-detected from keys when unset) |
| `GEMINI_MODEL` | `models/gemini-2.5-flash` | Gemini model (`models/` prefix recommended; bare `gemini-*` names also accepted) |
| `OPENROUTER_MODEL` | `google/gemini-2.5-flash` | OpenRouter model, provider prefix required. Also drives the PR-review action, where it defaults to `openrouter/free` |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1/chat/completions` | OpenRouter endpoint used by the PR-review action |
| `OPENROUTER_MAX_TOKENS` | `4096` | Max tokens for the PR-review action |

---

## Troubleshooting

- **Check workflow logs**: every run is visible under the **Actions** tab, with the exact command and error output per step.
- **Scraping works but nothing is triaged**: confirm at least one of `GEMINI_API_KEY` / `OPENROUTER_API_KEY` is set; otherwise triage fails on every run.
- **LLM quota / rate limit (Exit 75)**: the daily scraper stops triaging for the day when the LLM provider reports quota exhaustion; per-issue failures post a comment on the issue. To recover, wait for the daily quota reset and manually trigger the daily scrape workflow via Actions **workflow_dispatch**, or remove and re-apply the `triage-pending` label on stalled issues.
- **Setup pending / No jobs scraped**: verify `resumes/resume.yaml` has been updated with real content and no longer contains the `__JOBGITOPS_SETUP_PENDING__` sentinel string.
- **`applied` label set but the board card never moves**: the Projects V2 move only happens when `projects_v2` is configured in `config/settings.yaml` with a real `PVT_...` node ID; otherwise the label alone tracks state.
- **Board moves but the label never updates (or vice-versa)**: verify `GH_PAT` is set, active, and has required scopes. To reconcile out-of-sync board columns and issue labels, trigger the `Sync Project Status to Label` workflow manually via **workflow_dispatch** under the Actions tab (see [Enabling Projects V2](#enabling-projects-v2)).
- **Web research or job URL fetch fails**: if pages fail to parse due to anti-bot protection or rate limiting, add a `JINA_API_KEY` to your secrets (to raise the rate limit to 500 RPM) or configure a dedicated search provider like `tavily` or `brave` in `config/settings.yaml`.
- **`custom_queries` / `fit_threshold` seem ignored**: verify `custom_queries` is a top-level key in `config/settings.yaml` (a sibling of `search`), not nested under it.

---

## Upgrades & Maintenance

Updates to the core execution engine arrive automatically via the shared container image. Updates to the repository files (GitHub Actions workflow configurations, label definitions) are optional and can be synchronized by running the manual templates script: `scripts/sync-template.sh`. Pointers to release procedures and end-to-end suite guidelines can be found in [RELEASING.md](RELEASING.md) and [scripts/e2e.md](scripts/e2e.md).

---

## Architecture & Development

For technical details about how JobGitOps works internally, including architecture diagrams, GitHub Actions workflows, issue label definitions, local environment setup, and repository layout, please see the [DEVELOPMENT.md](DEVELOPMENT.md) guide.

Detailed developer environment guidelines and validation/issue-tracking helper instructions can also be found in [AGENTS.md](AGENTS.md).
