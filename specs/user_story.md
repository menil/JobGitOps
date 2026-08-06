# User Story: Martin's GitOps-Driven Job Search

Martin sat in the quiet study of his Seattle townhouse, staring out the window at the familiar drizzle. A steaming mug of black coffee rested beside his keyboard, its warmth contrasting with the cold shock that had settled in his chest.

For five years, Martin had poured his expertise into a local tech company as a senior software engineer. He knew the codebase inside out. He had mentored junior devs, designed robust microservices, and navigated complex deployments. But this morning, in a brief, ten-minute video call, it all came to an end. A sudden round of corporate restructuring had eliminated his department. He was laid off, effective immediately. It took him by total, absolute surprise.

Once the initial dust settled, Martin faced the daunting reality of modern job hunting. He opened the popular job boards and immediately felt a wave of frustration. The traditional job search workflow felt primitive, chaotic, and fundamentally broken for a developer:
- **No Version Control**: Copy-pasting resumes into obscure web portals, ending up with files like `Resume_Senior_SWE_v5_final_copy.pdf` scattered across his hard drive.
- **Manual Tailoring**: Spending hours manually tweaking bullet points to align with job descriptions, only to lose track of which company received which version.
- **Black-Box Pipelines**: Submitting applications into the void with zero transparency, tracking status on messy spreadsheets that had to be manually kept up to date.
- **Inefficient Triage**: Clicking through hundreds of listings that had mismatched stacks, wrong timezone expectations, or misaligned salary bands.

As a seasoned software engineer, Martin realized he didn't want to change how he worked just to find a job. He wanted to **manage his job search like a software engineering project**. He wanted version control, automation, and structured data. 

He wanted to manage his job search through **GitHub**.

---

## The Vision: JobGitOps

This is the purpose of **JobGitOps**. It is a serverless, GitOps-driven job application and tracking system designed for developers who want to manage their career search using the tools they already use every day. 

### The Foundation: Resume-as-Code

As a senior software engineer, Martin has always seen the value of rigorous standards, schema validation, and keeping track of everything in a structured, versioned format. He detests bloated word processor documents and unversioned PDFs. 

Instead, he maintains his base resume in his Git repository in a clean YAML format (`resumes/resume.yaml`), conforming strictly to the open-source [JSON Resume](https://github.com/jsonresume) schema. By representing his professional history as structured data, he gains the ability to:
- Maintain a single, authoritative "source of truth" for his entire career history.
- Programmatically parse his skills and work experience for matching and scraping.
- Track every modification, project detail, and role update with Git commit messages.
- Use automated templates and rendering engines (like WeasyPrint) to compile his resume on-the-fly.

With this foundation established, the daily automated workflows of JobGitOps can seamlessly read, evaluate, and tailor his profile. For Martin, the ideal day looks like this:

### 1. Automated Role Discovery (Scraping)
Every morning, while Martin is brewing his first cup of coffee, a scheduled GitHub Actions cron job runs the scraper module (`python -m jobgitops.cli.scrape`). 
- Using his base resume (`resumes/resume.yaml`) to infer his core skills and latest title—or utilizing custom queries defined in `config/settings.yaml` if he wants to target a new stack or specific domain—the bot generates search queries and scrapes platforms like LinkedIn, Indeed, and ZipRecruiter.
- To prevent duplicate work, it queries his own repository to deduplicate against roles he has already seen or applied to.
- It automatically creates new candidates as **GitHub Issues**, labeling them `triage-pending` and filling the issue body with structured markdown containing the job description, company, salary range, and source.

### 2. AI Triage
Instead of wading through hundreds of irrelevant jobs, Martin lets the AI Triage Engine (`python -m jobgitops.cli.triage`) do the initial filter.
- The bot evaluates each scraped job description against Martin’s base resume across five dimensions: tech stack, experience, location, salary, and domain.
- If a job doesn't meet his threshold (e.g., fit score < 4.0), the engine automatically posts a breakdown of the mismatches on the issue, adds a `triage-mismatched` label, and closes it.
- If it's a great match (score >= 4.0), the issue is labeled `ready-to-apply` and moves to the next phase.

### 3. GitOps Resume Tailoring
For the high-scoring roles, Martin needs a tailored resume. Instead of manually editing PDFs, the system automates this through Git:
- The AI Engine spawns a dedicated Git branch for the application: `applications/company-role-hash`.
- The engine rewrites `resumes/resume.yaml` on that branch, subtly adjusting his highlight bullets and skills to emphasize what the company is looking for.
- It renders a beautiful, print-ready PDF using WeasyPrint from standard HTML/CSS templates on the branch.
- The PDF and modified YAML are committed (adhering strictly to Conventional Commit standards to keep the repository log tidy) and pushed to the branch, leaving a clean Git diff that Martin can inspect to see exactly what changed.
- The bot posts a comment on the GitHub Issue with a direct link to the compiled PDF on the branch.

### 4. Kanban Lifecycle Tracking
Martin tracks his entire pipeline on a **GitHub Projects** board. 
- The column statuses (`Triage Pending`, `Ready to Apply`, `Applied`, `Interviewing`, `Rejected`) sync automatically with his repository's labels and issue states.
- When Martin clicks the link in the issue, reviews the Git diff on the branch, and officially submits the tailored PDF, he labels the issue `applied`.
- The automation moves the card to the `Applied` column, documenting his progress without requiring external tracking apps.

---

## Why This Matters

For Martin, JobGitOps is more than just a tool—it's a way to regain control. By treating his job search like software deployment, he has a version-controlled repository of every resume version he's ever sent, a clear audit trail of changes, and a fully automated assistant doing the tedious work of scraping and filtering.

He is no longer just a job seeker submitting to a black box. He is a developer running a highly optimized GitOps deployment pipeline—for his own career.
