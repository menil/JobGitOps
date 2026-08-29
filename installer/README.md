# jobgitops-installer

Interactive bootstrap installer for
[JobGitOps](https://github.com/menil/JobGitOps) — a serverless, GitOps-driven
job application and tracking system where GitHub Actions scrape roles,
AI-triage fit, and tailor your resume, all inside your own repository.

## Quick start

Requires Node.js 20+ and the [GitHub CLI](https://cli.github.com/) installed
and authenticated (`gh auth login`).

```bash
npx jobgitops-installer
```

The installer walks you through:

1. Creating a new private GitHub repository from the latest JobGitOps release
2. Choosing an LLM provider (Gemini, OpenRouter, or Claude Code) and
   storing its API key or token as a GitHub Secret
3. Optionally configuring extra search integrations (Tavily, Brave, Jina)
4. Optionally creating a linked GitHub Projects V2 Kanban board

After installation, push your resume (`resumes/resume.yaml`, JSON Resume
format) and the automation takes over: scheduled scrapes file candidate roles
as issues, an LLM scores each against your resume, and matching roles get a
tailored, print-ready resume rendered on a dedicated application branch.

See the [main repository](https://github.com/menil/JobGitOps) for the full
daily workflow, configuration reference, and architecture.

## CLI options

```
Usage: jobgitops-installer [options] [repo-name]
```

| Option | Description |
| ------ | ----------- |
| `-y, --yes` | Skip interactive questions and accept defaults |
| `--dry-run` | Output commands that would run without executing them |
| `--provider <provider>` | LLM provider to use (`gemini`, `openrouter`, or `claude`) |
| `--gemini-key <key>` | Gemini API key |
| `--openrouter-key <key>` | OpenRouter API key |
| `--claude-key <key>` | Claude Code subscription token (`claude setup-token`) or standard Anthropic API key |
| `--tavily-key <key>` | Tavily API key (optional search integration) |
| `--brave-key <key>` | Brave API key (optional search integration) |
| `--jina-key <key>` | Jina API key (optional search integration) |
| `--projects` | Integrate with GitHub Projects V2 |
| `--visibility <visibility>` | Repository visibility: `private` or `public` (default: `private`) |
| `--tag <ref>` | Install from a specific JobGitOps tag, branch, or commit |
| `--token <token>` | GitHub Personal Access Token (PAT) |

## Non-interactive example

```bash
npx jobgitops-installer my-job-search \
  --yes --provider gemini --gemini-key "$GEMINI_API_KEY"
```

## Pinning a template version

By default the installer uses the latest JobGitOps release. To install a
specific version:

```bash
npx jobgitops-installer my-job-search --tag v1.2.3
```
