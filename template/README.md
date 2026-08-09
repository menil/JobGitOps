# JobGitOps

![Setup](https://img.shields.io/badge/setup%20required-replace%20resumes%2Fresume.yaml-orange)

A serverless, GitOps-driven job application and tracking system. GitHub Issues are your pipeline, GitHub Projects is your Kanban board, and GitHub Actions is the automation plane that scrapes roles, AI-triages fit, and tailors your resume — all running free on GitHub's infrastructure.

## Getting Started

This repository was bootstrapped by the JobGitOps installer. To activate it:

1. **Replace the placeholder resume** in `resumes/resume.yaml` (a YAML file in [JSON Resume](https://jsonresume.org/schema) format) with your real work history, then commit and push to `main`.
2. **Configure secrets & variables** — see the docs linked below.
3. **Run the bootstrap scrape** once (Actions → `scrape-jobs` → Run workflow), then remove this setup badge. The daily cron takes over from there.

## Configuration

Per-user options live in `config/settings.yaml` and are edited directly in that file — same workflow as replacing the placeholder resume. Notable defaults:

- `search.location` defaults to `Remote`
- `search.enabled` (set to `false` to pause daily scraping)
- `fit_threshold` (minimum fit score, `1.0`–`5.0`, default `3.5`)

## Docs

Full setup, configuration, and workflow documentation: <https://github.com/menil/jobgitops>
