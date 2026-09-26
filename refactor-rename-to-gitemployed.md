# AI Decision Record: Rename Project to GitEmployed

## Context & Goal
The project was originally named \`JobGitOps\`. To improve brand memorability, community adoption, and discoverability, it was rebranded to **GitEmployed**. The goal was to execute a systematic refactor across all active code, configurations, workflows, test suites, CLI entry points, and documentation while keeping historical specification documents in \`specs/\` untouched as archival design records.

## Architecture & Key Decisions
1. **Module & Package Migration**:
   - Moved \`src/jobgitops/\` to \`src/gitemployed/\`.
   - Updated \`pyproject.toml\` package metadata, CLI commands (\`gitemployed-scrape\`, \`gitemployed-triage\`, \`gitemployed-respond\`, \`gitemployed-project-sync\`, \`gitemployed-status-transition\`, \`gitemployed-validate-resume\`, \`gitemployed-esd-export\`), and coverage settings.
   - Regenerated \`uv.lock\` dependency graph.
   - Updated all module imports, logger namespaces, HTTP User-Agents, and test mock patch targets across \`src/\` and \`tests/\`.

2. **Backward Compatibility for Automation**:
   - Retained recognition for legacy \`<!-- jobgitops:status-update -->\` and \`<!-- jobgitops:gmail-notice -->\` markers in \`assistant.py\` alongside new \`<!-- gitemployed:... -->\` markers, preventing infinite bot comment loops on existing issues.

3. **Installer Renaming**:
   - Renamed npm package to \`gitemployed-installer\` in \`installer/package.json\`, rebuilt distribution bundle (\`dist/index.js\`), updated tests, and regenerated package lockfile.

4. **CI/CD & Templates**:
   - Updated workflow definitions, container image references (\`ghcr.io/menil/gitemployed\`), and template repos.

5. **Historical Preservation**:
   - All files under \`specs/\` were preserved completely unchanged as historical design records.

## Alternatives Considered & Rejected
- **Modifying \`specs/\`**: Rejected because specifications are historical documents capturing the architecture at the time of writing.
- **Dropping legacy markers immediately**: Rejected to prevent breaking ongoing triage and status transition comment flows on repositories initialized before the rename.
