import { execa } from "execa";
import fs from "fs-extra";
import path from "path";
import os from "os";
import * as tar from "tar";
import pc from "picocolors";
import ora from "ora";

import { EXCLUDED_WORKFLOWS } from "./constants";
import { createProjectV2, fetchRepositoryNodeId, getGhCliToken } from "./api";

export interface InstallOptions {
  repoName: string;
  visibility: "private" | "main"; // Wait, 'private' or 'public'
  provider: "gemini" | "openrouter";
  primaryKey: string;
  optionalKeys: Record<string, string>;
  wantProjects: boolean;
  tag?: string;
  dryRun: boolean;
  token?: string;
}

export async function runInstallation(
  options: InstallOptions,
  owner: string,
): Promise<void> {
  const {
    repoName,
    visibility = "private",
    provider,
    primaryKey,
    optionalKeys,
    wantProjects,
    tag,
    dryRun,
    token,
  } = options;
  const resolvedToken = token || (await getGhCliToken());
  const targetTag = tag || (await resolveLatestTag(resolvedToken));

  console.log(
    pc.cyan(
      `\n🚀 Initializing installation using template version ${pc.bold(targetTag)}...`,
    ),
  );

  // 1. Create a safe temporary scratch directory
  const workDir = await fs.mkdtemp(
    path.join(os.tmpdir(), "jobgitops-install-"),
  );
  const tarballPath = path.join(workDir, `jobgitops-${targetTag}.tgz`);
  const extractDir = path.join(workDir, "extracted");
  const appDir = path.join(workDir, "app");

  await fs.ensureDir(extractDir);
  await fs.ensureDir(appDir);

  const env = { ...process.env, GH_TOKEN: resolvedToken };

  try {
    // 2 & 3. Download & Extract tarball
    const templateSourceDir = await downloadAndExtractTemplate(
      targetTag,
      tarballPath,
      extractDir,
      resolvedToken,
      dryRun,
    );

    // 4. Assemble workspace files
    await assembleFiles(templateSourceDir, appDir, owner, repoName, dryRun);

    // 5. GitHub Repository Provisioning
    await createGitHubRepository(owner, repoName, visibility, env, dryRun);

    // 5b. Create Projects V2 board if requested
    let projectNodeId: string | null = null;
    if (wantProjects && !dryRun) {
      const projectSpinner = ora(
        "Creating GitHub Projects V2 board...",
      ).start();
      const repoNodeId = await fetchRepositoryNodeId(
        owner,
        repoName,
        resolvedToken,
      );
      const projectResult = await createProjectV2(
        owner,
        repoName,
        repoNodeId,
        resolvedToken,
      );
      if (projectResult) {
        projectNodeId = projectResult.id;
        projectSpinner.succeed(`Project created: ${projectNodeId}`);
        patchReadmeWithProjectUrl(appDir, projectResult.url);
      } else {
        projectSpinner.warn(
          "Could not create project (see error above). Skipping project setup.",
        );
      }
    }

    // 6. Secrets Provisioning
    await provisionSecretsAndPermissions(
      owner,
      repoName,
      provider,
      primaryKey,
      optionalKeys,
      wantProjects,
      resolvedToken,
      env,
      dryRun,
    );

    // 7. Git Init, Commit & Push
    if (projectNodeId) {
      patchSettingsWithProjectId(appDir, projectNodeId, "Status");
    }
    await initializeGitAndPush(
      appDir,
      owner,
      repoName,
      targetTag,
      resolvedToken,
      dryRun,
    );

    // 8. Success Report
    console.log(
      pc.green(
        `\n✨ Done! Your JobGitOps repository is live: https://github.com/${owner}/${repoName}`,
      ),
    );
    console.log(`\n${pc.bold("Next steps:")}`);
    console.log(`  1. Clone your new repository or navigate to it.`);
    console.log(
      `  2. Edit ${pc.cyan("resumes/resume.yaml")} to replace the placeholders with your resume.`,
    );
    console.log(
      `  3. Commit and push your changes — the bootstrap scrape workflow will start automatically.`,
    );
    console.log(
      `  4. The setup status badge in your README will automatically update to green upon completion!`,
    );
  } finally {
    // 9. Clean up working scratch space
    await fs.remove(workDir).catch(() => {});
  }
}

async function downloadAndExtractTemplate(
  targetTag: string,
  tarballPath: string,
  extractDir: string,
  token: string | undefined,
  dryRun: boolean,
): Promise<string> {
  const spinner = ora("Downloading JobGitOps release assets...").start();
  if (!dryRun) {
    await downloadTarball(targetTag, tarballPath, token);
    spinner.succeed("Download complete.");
  } else {
    spinner.info("[Dry Run] Download skipped.");
  }

  const extractSpinner = ora("Extracting template archive...").start();
  if (!dryRun) {
    await tar.x({
      file: tarballPath,
      cwd: extractDir,
    });
    extractSpinner.succeed("Extraction complete.");
  } else {
    extractSpinner.info("[Dry Run] Extraction skipped.");
  }

  let templateSourceDir = "";
  if (!dryRun) {
    const topDirs = await fs.readdir(extractDir);
    const rootDirName = topDirs.find((d) =>
      fs.statSync(path.join(extractDir, d)).isDirectory(),
    );
    if (!rootDirName) {
      throw new Error(
        "Invalid archive: no root directory found inside the tarball.",
      );
    }
    templateSourceDir = path.join(extractDir, rootDirName);
  }
  return templateSourceDir;
}

async function assembleFiles(
  templateSourceDir: string,
  appDir: string,
  owner: string,
  repoName: string,
  dryRun: boolean,
): Promise<void> {
  const assembleSpinner = ora("Assembling repository files...").start();
  if (!dryRun) {
    await fs.ensureDir(path.join(appDir, "config"));
    await fs.ensureDir(path.join(appDir, "resumes"));
    await fs.ensureDir(path.join(appDir, ".github", "workflows"));
    await fs.ensureDir(path.join(appDir, ".github", "badges"));

    // Copy configs and assets
    await fs.copy(
      path.join(templateSourceDir, "template", "config", "settings.yaml"),
      path.join(appDir, "config", "settings.yaml"),
    );
    await fs.copy(
      path.join(templateSourceDir, "template", "resumes", "resume.yaml"),
      path.join(appDir, "resumes", "resume.yaml"),
    );
    await fs.copy(
      path.join(templateSourceDir, "template", "resumes", "template.html"),
      path.join(appDir, "resumes", "template.html"),
    );
    await fs.copy(
      path.join(templateSourceDir, "template", "resumes", "style.css"),
      path.join(appDir, "resumes", "style.css"),
    );
    await fs.copy(
      path.join(templateSourceDir, "template", "README.md"),
      path.join(appDir, "README.md"),
    );
    await fs.copy(
      path.join(templateSourceDir, "template", ".gitignore"),
      path.join(appDir, ".gitignore"),
    );
    await fs.copy(
      path.join(templateSourceDir, ".github", "labels.yml"),
      path.join(appDir, ".github", "labels.yml"),
    );

    // Copy setup status SVGs
    await fs.copy(
      path.join(templateSourceDir, ".github", "badges", "setup-status.svg"),
      path.join(appDir, ".github", "badges", "setup-status.svg"),
    );
    await fs.copy(
      path.join(templateSourceDir, ".github", "badges", "setup-required.svg"),
      path.join(appDir, ".github", "badges", "setup-required.svg"),
    );
    await fs.copy(
      path.join(templateSourceDir, ".github", "badges", "setup-complete.svg"),
      path.join(appDir, ".github", "badges", "setup-complete.svg"),
    );

    // Copy workflows (except maintainer-only ones)
    const workflowSrc = path.join(templateSourceDir, ".github", "workflows");
    const workflowDest = path.join(appDir, ".github", "workflows");
    const workflows = await fs.readdir(workflowSrc);
    for (const wf of workflows) {
      if (!EXCLUDED_WORKFLOWS.includes(wf)) {
        await fs.copy(path.join(workflowSrc, wf), path.join(workflowDest, wf));
      }
    }

    // Replace placeholders in README
    const readmePath = path.join(appDir, "README.md");
    let readmeContent = await fs.readFile(readmePath, "utf-8");
    readmeContent = readmeContent
      .replace(/__OWNER__/g, owner)
      .replace(/__REPO__/g, repoName);
    await fs.writeFile(readmePath, readmeContent, "utf-8");

    assembleSpinner.succeed("Files assembled successfully.");
  } else {
    assembleSpinner.info("[Dry Run] Assembly skipped.");
  }
}

async function createGitHubRepository(
  owner: string,
  repoName: string,
  visibility: string,
  env: any,
  dryRun: boolean,
): Promise<void> {
  const repoSpinner = ora(
    `Creating private GitHub repository ${owner}/${repoName}...`,
  ).start();
  if (!dryRun) {
    await execa("gh", ["repo", "create", repoName, `--${visibility}`], {
      env,
    });
    repoSpinner.succeed(
      `Created repository: https://github.com/${owner}/${repoName}`,
    );
  } else {
    repoSpinner.info(
      `[Dry Run] Will create repository: https://github.com/${owner}/${repoName}`,
    );
  }
}

async function provisionSecretsAndPermissions(
  owner: string,
  repoName: string,
  provider: "gemini" | "openrouter",
  primaryKey: string,
  optionalKeys: Record<string, string>,
  wantProjects: boolean,
  token: string | undefined,
  env: any,
  dryRun: boolean,
): Promise<void> {
  const secretSpinner = ora(
    "Uploading credentials securely to GitHub Secrets...",
  ).start();
  if (!dryRun) {
    // Primary API key
    const primarySecretName =
      provider === "gemini" ? "GEMINI_API_KEY" : "OPENROUTER_API_KEY";
    await uploadSecret(owner, repoName, primarySecretName, primaryKey, token);

    // Optional keys
    for (const [keyName, keyValue] of Object.entries(optionalKeys)) {
      await uploadSecret(
        owner,
        repoName,
        `${keyName.toUpperCase()}_API_KEY`,
        keyValue,
        token,
      );
    }

    // If Projects V2 is integrated, store PROJECT_V2_TOKEN secret
    if (wantProjects) {
      await uploadSecret(owner, repoName, "PROJECT_V2_TOKEN", token, token);
    }

    // Enable write permissions for GitHub actions
    await execa(
      "gh",
      [
        "api",
        "--method",
        "PUT",
        `repos/${owner}/${repoName}/actions/permissions`,
        "-F",
        "enabled=true",
        "-f",
        "allowed_actions=all",
        "-f",
        "default_workflow_permissions=write",
      ],
      { env },
    );

    secretSpinner.succeed("Secrets configured successfully.");
  } else {
    secretSpinner.info("[Dry Run] Secrets config skipped.");
  }
}

/**
 * Patches the assembled settings.yaml to uncomment and populate the projects_v2
 * section with the actual project node ID. Operates on the assembled appDir copy.
 */
function patchSettingsWithProjectId(
  appDir: string,
  projectId: string,
  statusFieldName: string,
): void {
  const settingsPath = path.join(appDir, "config", "settings.yaml");
  let content = fs.readFileSync(settingsPath, "utf8");

  // Replace the commented-out projects_v2 block with the live config
  const commentedPattern =
    /#[\s-]*projects_v2:[\s\S]*?#?\s*project_id:\s*"[^"]*"[\s\S]*?#?\s*status_field_name:\s*"[^"]*"/;
  const replacement = `projects_v2:\n  project_id: "${projectId}"\n  status_field_name: "${statusFieldName}"`;

  if (commentedPattern.test(content)) {
    content = content.replace(commentedPattern, replacement);
  } else {
    // Fallback: append at end of file
    content += `\nprojects_v2:\n  project_id: "${projectId}"\n  status_field_name: "${statusFieldName}"\n`;
  }

  fs.writeFileSync(settingsPath, content, "utf8");
}

/**
 * Patches the README.md in appDir to link to the specific project V2 board URL
 * instead of the generic repository /projects endpoint.
 */
function patchReadmeWithProjectUrl(appDir: string, projectUrl: string): void {
  const readmePath = path.join(appDir, "README.md");
  if (fs.existsSync(readmePath)) {
    let content = fs.readFileSync(readmePath, "utf8");
    const pattern =
      /https?:\/\/(?:www\.)?github\.com\/[^/]+\/[^/]+\/projects\/?/gi;
    content = content.replace(pattern, projectUrl);
    fs.writeFileSync(readmePath, content, "utf8");
  }
}

async function initializeGitAndPush(
  appDir: string,
  owner: string,
  repoName: string,
  targetTag: string,
  token: string | undefined,
  dryRun: boolean,
): Promise<void> {
  const gitSpinner = ora(
    "Initializing Git and pushing bootstrap commit...",
  ).start();
  if (!dryRun) {
    await execa("git", ["init", "-b", "main"], { cwd: appDir });
    await execa("git", ["config", "user.name", owner], { cwd: appDir });
    await execa(
      "git",
      ["config", "user.email", `${owner}@users.noreply.github.com`],
      { cwd: appDir },
    );
    await execa("git", ["add", "-A"], { cwd: appDir });
    await execa(
      "git",
      ["commit", "-m", `chore: bootstrap from JobGitOps template ${targetTag}`],
      { cwd: appDir },
    );

    // Use authenticated remote URL for initial push, then restore to standard URL
    const resolvedToken = token;
    const authedUrl = `https://x-access-token:${resolvedToken}@github.com/${owner}/${repoName}.git`;
    const cleanUrl = `https://github.com/${owner}/${repoName}.git`;

    await execa("git", ["remote", "add", "origin", authedUrl], {
      cwd: appDir,
    });
    await execa("git", ["push", "-u", "origin", "main"], { cwd: appDir });
    await execa("git", ["remote", "set-url", "origin", cleanUrl], {
      cwd: appDir,
    });

    gitSpinner.succeed("Git push complete.");
  } else {
    gitSpinner.info("[Dry Run] Git commands skipped.");
  }
}

async function resolveLatestTag(token?: string): Promise<string> {
  const env = token ? { ...process.env, GH_TOKEN: token } : process.env;
  try {
    const { stdout } = await execa(
      "gh",
      ["api", "repos/menil/jobgitops/releases/latest", "--jq", ".tag_name"],
      { env },
    );
    const tag = stdout.trim();
    if (!tag) throw new Error("Received empty tag name");
    return tag;
  } catch {
    // Fallback: Default to v0.6.0 if releases list cannot be queried
    return "v0.6.0";
  }
}

async function downloadTarball(
  tag: string,
  destPath: string,
  token?: string,
): Promise<void> {
  const codeloadUrl = `https://codeload.github.com/menil/jobgitops/tar.gz/refs/tags/${tag}`;
  const env = token ? { ...process.env, GH_TOKEN: token } : process.env;

  try {
    // Attempt fast anonymous public download first
    const response = await fetch(codeloadUrl);
    if (response.ok && response.body) {
      const fileStream = fs.createWriteStream(destPath);
      const reader = response.body.getReader();
      let reading = true;
      while (reading) {
        const { done, value } = await reader.read();
        if (done) {
          reading = false;
        } else {
          fileStream.write(Buffer.from(value));
        }
      }
      fileStream.end();
      return;
    }
  } catch {
    // Fall back to authenticated API endpoint
  }

  // Fallback: Private source repository download via GitHub CLI
  try {
    const { stdout } = await execa(
      "gh",
      ["api", `repos/menil/jobgitops/tarball/${tag}`],
      { env, encoding: "buffer" },
    );
    await fs.writeFile(destPath, stdout);
  } catch (error: any) {
    throw new Error(
      `Failed to download JobGitOps tarball for '${tag}': ${error.message || error}`,
    );
  }
}

async function uploadSecret(
  owner: string,
  repo: string,
  name: string,
  value: string,
  token?: string,
): Promise<void> {
  const env = token ? { ...process.env, GH_TOKEN: token } : process.env;
  // Pass the secret value securely through stdin to prevent leakage in process trees
  await execa("gh", ["secret", "set", name, "--repo", `${owner}/${repo}`], {
    env,
    input: value,
  });
}
