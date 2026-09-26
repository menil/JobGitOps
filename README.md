# GitEmployed

A serverless, GitOps-driven job application and tracking system. GitEmployed treats the job search like software deployment: GitHub Issues are your pipeline, GitHub Projects is your Kanban board, and GitHub Actions is the automation plane that scrapes roles, AI-triages fit, and tailors your resume — all running free on GitHub's infrastructure.

---

<p align="center">
  <img width="800" height="582" alt="Example of the GitEmployed issue view and kanban board" src="https://github.com/user-attachments/assets/86576825-9e25-4acd-b58a-3def59f34e5d" />
</p>

## Example job search repository: `gitemployed-example`

#### 🔍 [Issue View](https://github.com/menil/gitemployed-example/issues)
#### 🗂️ [Kanban Board View](https://github.com/users/menil/projects/13)

---

## Features

- 🤖 **Automated Role Discovery**: A scheduled Actions cron ([Daily Job Scraper](https://github.com/menil/gitemployed-example/actions/workflows/scrape-jobs.yml)) scrapes LinkedIn, Indeed, and ZipRecruiter via `python-jobspy`, generating search queries from your resume skills, and files new roles as GitHub Issues labeled `triage-pending`.
- 🧠 **AI Triage & Tailoring**: A two-pass LLM engine ([Triage and Tailor Issue](https://github.com/menil/gitemployed-example/actions/workflows/triage-issue.yml)) scores each listing against your resume across 5 dimensions (tech stack, experience, location, salary, domain). Matches above your `fit_threshold` get a tailored resume; mismatches are auto-closed with a reasons comment.
- 📄 **Resume-as-Code**: Your base resume lives in versioned YAML (`resumes/resume.yaml`, JSON Resume schema). Tailored variants are rendered to print-ready PDFs via a [JSON Resume theme](https://jsonresume.org/themes) of your choosing on dedicated application branches — every version you send is a clean, reviewable Git diff, complete with side-by-side visual PDF diffs via [pdf-vdiff](https://github.com/menil/pdf-vdiff).
- 💬 **Issue Assistant**: A tool-using agent ([Respond to Issue](https://github.com/menil/gitemployed-example/actions/workflows/respond-issue.yml)) answers questions on issue threads via live web research (search + fetch with cited sources), recognizes conversational status intents ("I applied", "phone screen scheduled") to apply labels, and auto-triages issues opened with a bare job URL.
- 📬 **Gmail Sync** *(optional)*: An hourly cron ([Gmail Sync](https://github.com/menil/gitemployed-example/actions/workflows/gmail-sync.yml)) reads a label-scoped, DMARC-authenticated slice of your Gmail inbox, matches lifecycle emails (interview invites, rejections, offers) to open applications via a single LLM call, and applies the same status-update side effects the Issue Assistant already provides. Off by default. The installer wizard can set this up for you (creates a one-time Google OAuth consent step in your browser, no copy-pasting); see [DEVELOPMENT.md](DEVELOPMENT.md#gmail-sync-setup-optional) for the one-time Google Cloud setup either path needs, and for adding it to an already-installed repo.
- 🗂️ **Kanban Lifecycle Tracking**: Roles flow through GitHub Issues + Projects V2 (`Triage Pending → Ready to Apply → Applied → In Loop → Rejected`) with label-based automation and a label-only fallback.
- 📤 **ESD Export**: A manually-triggered workflow ([ESD Export](https://github.com/menil/gitemployed-example/actions/workflows/esd-export.yml)) turns your `applied`/`in-loop` label history into a CSV or XLSX activity log — grouped weekly or monthly, with a configurable per-period row cap — for filing with a state unemployment department. Downloads as a workflow artifact; see [DEVELOPMENT.md](DEVELOPMENT.md#running-the-esd-export) for how to run it.

---

## Getting Started

### Prerequisites

You must have the [GitHub CLI (`gh`)](https://cli.github.com/) installed and authenticated on your local machine.

### Quick Start (one-command install)

Install into a new private repository with a single command. The interactive installer will check your GitHub CLI connection, walk you through setting up credentials (including a GitHub token and a Gemini, OpenRouter, or Claude Code LLM key), and configure your initial resume:

```bash
npx gitemployed-installer
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
3. **Tailoring** — Above threshold → a dedicated branch `applications/<company>-<role>-<hash>` is created. `resumes/resume.yaml` is subtly tailored, a JSON version is generated, and a print-ready PDF is compiled alongside a visual side-by-side diff PDF. All files are committed and pushed, and a comment links the fit score, the tailored PDF, and the visual diff PDF directly in the issue thread.
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

Overwrite this file with your own work history, education, and skills. Every tailored variant is written to the branch alongside a generated `resumes/resume.json` and `resumes/resume.pdf`.

### `config/settings.yaml`

Controls search preferences and the triage threshold:

```yaml
# Minimum fit score (1.0 to 5.0) required to tailor a resume and apply.
fit_threshold: 3.5

# Visual theme for resumes/resume.pdf, from the JSON Resume theme ecosystem.
theme: "@jsonresume/jsonresume-theme-professional@1.0.22"

search:
  enabled: true                         # Enable or disable daily scraping
  work_preference: "hybrid"             # remote | onsite | hybrid
  job_type: "fulltime"                  # fulltime | contract | parttime | internship
  desired_salary_min: 180000            # Optional: minimum annual USD salary for AI triage scoring
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

- **`theme`**: Selects the [JSON Resume theme](https://jsonresume.org/themes) used to render `resume.pdf` — browse [npm](https://www.npmjs.com/search?q=jsonresume-theme) or GitHub for options. Accepts either an npm package pinned to an exact version (`"<package>@<version>"`) or a GitHub-only theme pinned to a full 40-character commit SHA (`"github:<owner>/<repo>#<sha>"`). GitEmployed installs the theme package — running its own code in the process — and later renders with it, so always pin to an exact version or commit SHA, never a floating branch, tag, or dist-tag like `latest`. Omit this key to fall back to a pinned default theme.
- **`search.desired_salary_min`**: Optional minimum acceptable annual salary (positive integer in USD) passed to the AI triage prompt. When configured, jobs at or above your minimum receive top salary alignment scores (4.5–5.0), while jobs below it are scaled down proportionally. If omitted, the triage LLM evaluates compensation fit against market rates for your seniority level alone.
- **`custom_queries`**: When non-empty, the scraper uses these queries instead of auto-generating them from your resume — useful for targeting new stacks or domains.
- **`projects_v2`**: When configured, issue cards move through your Projects V2 board automatically and column moves are reflected back as labels. Without it (or while the placeholder is in place), the system falls back to repository labels (`ready-to-apply`, `applied`, `in-loop`, `rejected`).

### Gmail Sync (optional)

Off by default. Enabling it lets the hourly `gmail-sync.yml` cron read a label-scoped slice of your inbox and drive the same status updates the Issue Assistant already provides — see [DEVELOPMENT.md](DEVELOPMENT.md#gmail-sync-setup-optional) for the one-time OAuth setup and the three repo secrets it requires (`GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN`).

```yaml
# Optional Gmail integration. Off by default — enabling it requires the
# one-time manual OAuth setup in DEVELOPMENT.md and three repo secrets.
gmail:
  enabled: true
  label: "GitEmployed"        # required when enabled: only mail carrying this
                             # Gmail label is ever read (see DEVELOPMENT.md
                             # for how to create the label + a Gmail filter).
  query: ""                 # optional: further narrows every fetch beyond
                             # the label alone.
  days_back: 7               # lookback window re-queried on every run;
                              # also the retention window for the de-dup
                              # cursor.
```

---

## Troubleshooting

- **Check workflow logs**: every run is visible under the **Actions** tab, with the exact command and error output per step.
- **Scraping works but nothing is triaged**: confirm your LLM provider key is configured in repository secrets; otherwise triage fails on every run.
- **LLM quota / rate limit (Exit 75)**: the daily scraper stops triaging for the day when the LLM provider reports quota exhaustion; per-issue failures post a comment on the issue. To recover, wait for the daily quota reset and manually trigger the daily scrape workflow via Actions **workflow_dispatch**, or remove and re-apply the `triage-pending` label on stalled issues.
- **Setup pending / No jobs scraped**: verify `resumes/resume.yaml` has been updated with real content and no longer contains the `__GITEMPLOYED_SETUP_PENDING__` sentinel string.
- **`applied` label set but the board card never moves**: the Projects V2 move only happens when `projects_v2` is configured in `config/settings.yaml` with a real `PVT_...` node ID; otherwise the label alone tracks state.
- **Board moves but the label never updates (or vice-versa)**: verify your configuration has Projects V2 enabled. To reconcile out-of-sync board columns and issue labels, see the manual sync procedures in [DEVELOPMENT.md](DEVELOPMENT.md#project-sync-and-reconciliation-cli).
- **Web research or job URL fetch fails**: if pages fail to parse due to anti-bot protection or rate limiting, add a Jina API key to your secrets or configure a dedicated search provider in `config/settings.yaml`.
- **`custom_queries` / `fit_threshold` seem ignored**: verify `custom_queries` is a top-level key in `config/settings.yaml` (a sibling of `search`), not nested under it.
- **Gmail sync isn't picking up emails**: confirm `gmail.label` in `config/settings.yaml` exactly matches your Gmail label's spelling (case-sensitive); confirm the sender passes DMARC (a spoofed or improperly-forwarded message is silently skipped, by design); then check the `Gmail Sync` workflow's run log under **Actions** for the specific skip/error reason.

---

## Upgrades & Maintenance

Updates to the core execution engine arrive automatically via the shared container image. Updates to the repository files (GitHub Actions workflow configurations, label definitions) are optional and can be synchronized by running the manual templates script: `scripts/sync-template.sh`. Pointers to release procedures and end-to-end suite guidelines can be found in [RELEASING.md](RELEASING.md) and [scripts/e2e.md](scripts/e2e.md).

---

## Architecture & Development

For technical details about how GitEmployed works internally, including architecture diagrams, GitHub Actions workflows, issue label definitions, local environment setup, and repository layout, please see the [DEVELOPMENT.md](DEVELOPMENT.md) guide.

Detailed developer environment guidelines and validation/issue-tracking helper instructions can also be found in [AGENTS.md](AGENTS.md).
