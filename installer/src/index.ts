import { Command, Option } from "commander";
import pc from "picocolors";
import { execa } from "execa";
import { fetchGithubUser, validateApiKey } from "./api.js";
import ora from "ora";
import {
  promptRepoName,
  promptProjectsV2,
  promptProvider,
  promptApiKey,
  promptOptionalServices,
  promptOptionalKeys,
  promptRefreshScopes,
  OptionalService,
} from "./prompts.js";
import { runInstallation } from "./installer.js";

const program = new Command();

program
  .name("jobgitops-installer")
  .description("Interactive bootstrap installer for JobGitOps repositories")
  .argument("[repo-name]", "Name of the repository to create (e.g. job-search)")
  .option("-y, --yes", "Skip interactive questions and accept defaults", false)
  .option(
    "--dry-run",
    "Output commands that would run without executing them",
    false,
  )
  .option("--provider <provider>", "LLM provider to use (gemini or openrouter)")
  .option("--gemini-key <key>", "Gemini API key")
  .option("--openrouter-key <key>", "OpenRouter API key")
  .option("--tavily-key <key>", "Tavily API key")
  .option("--brave-key <key>", "Brave API key")
  .option("--jina-key <key>", "Jina API key")
  .option("--projects", "Integrate with GitHub Projects V2", false)
  .addOption(
    new Option(
      "--visibility <visibility>",
      "Repository visibility ('private' or 'public'); the Projects V2 board follows this setting",
    )
      .choices(["private", "public"])
      .default("private"),
  )
  .option(
    "--tag <ref>",
    "Specify a tag, branch, or commit of JobGitOps to install",
  )
  .option("--token <token>", "GitHub Personal Access Token (PAT)")
  .action(async (repoNameArg, options) => {
    try {
      await preflightChecks();

      const interactive = !options.yes && process.stdin.isTTY;

      // 1. Authenticate and retrieve user profile & scopes
      const githubToken = options.token || process.env.GH_TOKEN;
      const user = await fetchGithubUser(githubToken);
      const owner = user.login;
      const hasTokenEnv = !!githubToken;

      console.log(
        pc.green(`✓ Authenticated as GitHub user: ${pc.bold(owner)}`),
      );

      // 2. Resolve target repository name
      let repoName = repoNameArg || "";
      if (!repoName) {
        if (interactive) {
          repoName = await promptRepoName("job-search");
        } else {
          repoName = "job-search";
        }
      }

      // 3. Resolve Projects V2 integration
      let wantProjects = options.projects;
      if (!wantProjects && interactive) {
        wantProjects = await promptProjectsV2();
      }

      // 4. Validate and resolve token scopes
      const requiredScopes = ["repo", "workflow"];
      if (wantProjects) {
        requiredScopes.push("project", "write:discussion");
      }

      const missingScopes = requiredScopes.filter(
        (s) => !user.scopes.includes(s),
      );
      if (user.scopes.length > 0 && missingScopes.length > 0) {
        if (interactive && !hasTokenEnv) {
          const runRefresh = await promptRefreshScopes(missingScopes);
          if (runRefresh) {
            console.log(
              pc.cyan(
                `\n🔑 Running: gh auth refresh -s ${missingScopes.join(",")}`,
              ),
            );
            await execa(
              "gh",
              ["auth", "refresh", "-s", missingScopes.join(",")],
              { stdio: "inherit" },
            );

            // Re-fetch user profile to verify scopes
            const updatedUser = await fetchGithubUser(githubToken);
            const stillMissing = requiredScopes.filter(
              (s) => !updatedUser.scopes.includes(s),
            );
            if (stillMissing.length > 0) {
              throw new Error(
                `Scope refresh incomplete. Still missing: ${stillMissing.join(", ")}`,
              );
            }
          } else {
            throw new Error(
              `Installation aborted. Missing required scopes: ${missingScopes.join(", ")}`,
            );
          }
        } else {
          throw new Error(
            `Token is missing required scope(s): ${missingScopes.join(", ")}. Please re-run with a token that has these scopes.`,
          );
        }
      }

      // 5. LLM Provider and Keys resolution
      let provider = options.provider || process.env.JOBGITOPS_PROVIDER;
      if (!provider && interactive) {
        provider = await promptProvider();
      } else if (!provider) {
        provider = "gemini"; // default
      }

      if (provider !== "gemini" && provider !== "openrouter") {
        throw new Error(
          `Invalid provider '${provider}'. Supported values: 'gemini', 'openrouter'.`,
        );
      }

      let primaryKey =
        provider === "gemini"
          ? options.geminiKey || process.env.GEMINI_API_KEY
          : options.openrouterKey || process.env.OPENROUTER_API_KEY;

      if (!primaryKey && interactive) {
        primaryKey = await promptApiKey(provider);
      } else if (!primaryKey) {
        throw new Error(`No API key provided for LLM provider: ${provider}.`);
      }

      // 5.1 Validate the primary API key
      const validateSpinner = ora(
        `Verifying ${provider === "gemini" ? "Gemini" : "OpenRouter"} API key...`,
      ).start();
      try {
        await validateApiKey(provider, primaryKey);
        validateSpinner.succeed("API key verified successfully.");
      } catch (err: any) {
        validateSpinner.fail();
        throw err;
      }

      // 6. Optional Services and Keys resolution
      const optionalKeys: Record<string, string> = {};
      const serviceEnvMap: Record<OptionalService, string | undefined> = {
        tavily: options.tavilyKey || process.env.TAVILY_API_KEY,
        brave:
          options.braveKey ||
          process.env.BRAVE_KEY ||
          process.env.BRAVE_API_KEY,
        jina:
          options.jinaKey || process.env.JINA_KEY || process.env.JINA_API_KEY,
      };

      const pendingPromptServices: OptionalService[] = [];
      for (const service of ["tavily", "brave", "jina"] as OptionalService[]) {
        const val = serviceEnvMap[service];
        if (val) {
          optionalKeys[service] = val;
        } else if (interactive) {
          pendingPromptServices.push(service);
        }
      }

      if (pendingPromptServices.length > 0 && interactive) {
        const selectedPromptServices = await promptOptionalServices();
        const filteredPrompts = pendingPromptServices.filter((s) =>
          selectedPromptServices.includes(s),
        );
        if (filteredPrompts.length > 0) {
          const promptedKeys = await promptOptionalKeys(filteredPrompts);
          Object.assign(optionalKeys, promptedKeys);
        }
      }

      // 7. Execute the installation core logic
      await runInstallation(
        {
          repoName,
          visibility: options.visibility,
          provider,
          primaryKey,
          optionalKeys,
          wantProjects,
          tag: options.tag,
          dryRun: options.dryRun,
          token: options.token,
        },
        owner,
      );
    } catch (err: any) {
      console.error(pc.red(`\n❌ Error: ${err.message || err}`));
      process.exit(1);
    }
  });

async function preflightChecks(): Promise<void> {
  const binaryChecks = [
    {
      cmd: "gh",
      desc: "GitHub CLI (gh) - install from https://cli.github.com/",
    },
    { cmd: "git", desc: "Git VCS tool" },
    { cmd: "tar", desc: "Tar archiver utility" },
  ];

  for (const check of binaryChecks) {
    try {
      await execa("which", [check.cmd]);
    } catch {
      throw new Error(
        `Preflight check failed: ${check.desc} is required but could not be located in your system PATH.`,
      );
    }
  }
}

program.parse(process.argv);
