import { input, confirm, select, password, checkbox } from "@inquirer/prompts";
import pc from "picocolors";

export async function promptRepoName(defaultName: string): Promise<string> {
  return input({
    message: "Enter the repository name:",
    default: defaultName,
    validate: (val) => {
      const trimmed = val.trim();
      const valid =
        /^[A-Za-z0-9._-]+$/.test(trimmed) &&
        !trimmed.startsWith("-") &&
        !trimmed.endsWith("-") &&
        !trimmed.startsWith(".") &&
        !trimmed.endsWith(".") &&
        !trimmed.includes("..") &&
        !trimmed.endsWith(".git");
      return (
        valid ||
        "Invalid name. Use 1-100 characters of letters, numbers, '.', '_', or '-', with no leading/trailing dot or hyphen."
      );
    },
  });
}

export async function promptProjectsV2(): Promise<boolean> {
  return confirm({
    message:
      "Do you want to create a GitHub Projects Kanban board to visually track job applications? (may require an additional scope on your GitHub token)",
    default: true,
  });
}

export async function promptProvider(): Promise<
  "gemini" | "openrouter" | "claude"
> {
  return select({
    message: "Which LLM provider do you want to use?",
    choices: [
      { name: `✨ ${pc.bold("Gemini")}`, value: "gemini" as const },
      { name: `🔌 ${pc.bold("OpenRouter")}`, value: "openrouter" as const },
      {
        name: `🧠 ${pc.bold("Claude Code")} (Pro/Max subscription via claude setup-token)`,
        value: "claude" as const,
      },
    ],
  });
}

export async function promptApiKey(
  provider: "gemini" | "openrouter" | "claude",
): Promise<string> {
  let name: string;
  if (provider === "gemini") {
    name = "Gemini (GEMINI_API_KEY)";
  } else if (provider === "openrouter") {
    name = "OpenRouter (OPENROUTER_API_KEY)";
  } else {
    name =
      "Claude Code Token (CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`)";
  }
  return password({
    message: `Enter your ${pc.cyan(name)}:`,
    mask: true,
    validate: (val) => val.trim().length > 0 || "API key cannot be empty.",
  });
}

export type OptionalService = "tavily" | "brave" | "jina";

export async function promptOptionalServices(): Promise<OptionalService[]> {
  return checkbox({
    message:
      "Select optional services to configure (Space to toggle, Enter to confirm):",
    choices: [
      {
        name: `🔍 ${pc.bold("Tavily")} (Search engine API)`,
        value: "tavily" as const,
      },
      {
        name: `🦁 ${pc.bold("Brave")} (Web search API)`,
        value: "brave" as const,
      },
      {
        name: `🌐 ${pc.bold("Jina")} (Reader API for web pages)`,
        value: "jina" as const,
      },
    ],
  });
}

export async function promptOptionalKeys(
  services: OptionalService[],
): Promise<Record<OptionalService, string>> {
  const keys = {} as Record<OptionalService, string>;
  for (const service of services) {
    const capitalized = service.charAt(0).toUpperCase() + service.slice(1);
    const key = await password({
      message: `Enter your ${pc.cyan(`${capitalized} API Key (${service.toUpperCase()}_API_KEY)`)}:`,
      mask: true,
      validate: (val) => val.trim().length > 0 || "API key cannot be empty.",
    });
    keys[service] = key.trim();
  }
  return keys;
}

export async function promptRefreshScopes(
  missingScopes: string[],
): Promise<boolean> {
  return confirm({
    message: `Token is missing scope(s): ${pc.bold(missingScopes.join(", "))}. Run "gh auth refresh -s ${missingScopes.join(",")}" now?`,
    default: true,
  });
}
