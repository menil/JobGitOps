import { execa } from "execa";

export interface GithubUser {
  login: string;
  scopes: string[];
}

/**
 * Invokes the gh CLI to fetch the current user profile and OAuth scopes.
 * Uses 'gh api user --include' to retrieve headers and body in one call.
 */
export async function fetchGithubUser(token?: string): Promise<GithubUser> {
  const env = token ? { ...process.env, GH_TOKEN: token } : process.env;

  try {
    const { stdout } = await execa("gh", ["api", "user", "--include"], { env });

    // Headers and body are separated by a blank line
    const splitIndex = stdout.indexOf("\r\n\r\n");
    const headersPart =
      splitIndex !== -1 ? stdout.slice(0, splitIndex) : stdout;
    const bodyPart = splitIndex !== -1 ? stdout.slice(splitIndex + 4) : "";

    // Extract X-OAuth-Scopes header (case-insensitive)
    let scopes: string[] = [];
    const scopeMatch = headersPart.match(/x-oauth-scopes:\s*([^\r\n]+)/i);
    if (scopeMatch && scopeMatch[1]) {
      scopes = scopeMatch[1]
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
    }

    // Parse the JSON body to retrieve login
    let login = "";
    if (bodyPart) {
      try {
        const parsed = JSON.parse(bodyPart);
        login = parsed.login || "";
      } catch {
        // Fallback: if JSON parse fails, try running gh api user --jq .login
        const { stdout: loginOut } = await execa(
          "gh",
          ["api", "user", "--jq", ".login"],
          { env },
        );
        login = loginOut.trim();
      }
    }

    if (!login) {
      throw new Error(
        "Could not determine your GitHub username from the API response.",
      );
    }

    return { login, scopes };
  } catch (error: any) {
    throw new Error(`GitHub authentication failed: ${error.message || error}`);
  }
}

/**
 * Checks if the required scopes are present. If not, requests user authorization
 * to execute 'gh auth refresh'.
 */
export async function verifyAndRefreshScopes(
  user: GithubUser,
  wantProjects: boolean,
  hasTokenEnv: boolean,
  interactive: boolean,
): Promise<GithubUser> {
  const required = ["repo", "workflow"];
  if (wantProjects) {
    required.push("project", "write:discussion");
  }

  // Classic tokens present X-OAuth-Scopes. For fine-grained tokens or browser auth
  // that lacks the header, we warn and proceed to let the action fail downstream.
  if (user.scopes.length === 0) {
    console.warn(
      "\n⚠️  Note: Could not verify OAuth scopes (fine-grained token or SSO configuration). Continuing...",
    );
    return user;
  }

  const missing = required.filter((scope) => !user.scopes.includes(scope));
  if (missing.length === 0) {
    return user;
  }

  const missingList = missing.join(", ");
  if (hasTokenEnv || !interactive) {
    throw new Error(
      `Your GitHub token is missing the required scope(s): ${missingList}. Please use a token with these scopes or run: gh auth refresh -s ${missing.join(",")}`,
    );
  }

  console.log(
    `\n🔑 Your active session is missing required scopes: ${picocolorsBold(missingList)}`,
  );

  // Prompt using interactive inquirer flow (handled by caller or helper)
  return user; // Handled dynamically in index.ts/prompts.ts flow
}

function picocolorsBold(str: string): string {
  // Simple helper to avoid importing colors inside api.ts if not needed,
  // but we can import it if we want.
  return `\x1b[1m${str}\x1b[22m`;
}

/**
 * Validates the primary LLM API key by executing a fast mock query against
 * the provider's official model-listing endpoints.
 */
export async function validateApiKey(
  provider: "gemini" | "openrouter",
  apiKey: string,
): Promise<void> {
  const url =
    provider === "gemini"
      ? `https://generativelanguage.googleapis.com/v1beta/models?key=${apiKey}`
      : "https://openrouter.ai/api/v1/auth/key";

  const headers =
    provider === "openrouter"
      ? { Authorization: `Bearer ${apiKey}` }
      : undefined;

  try {
    const response = await fetch(url, { headers });
    if (!response.ok) {
      if (
        response.status === 401 ||
        response.status === 403 ||
        response.status === 400
      ) {
        throw new Error(
          `${provider === "gemini" ? "Gemini" : "OpenRouter"} key rejected by the API (HTTP ${response.status})`,
        );
      }
      throw new Error(`API returned HTTP ${response.status}`);
    }
  } catch (error: any) {
    if (error.message && error.message.includes("rejected")) {
      throw error;
    }
    // Network or transport failure
    throw new Error(
      `Could not reach the ${provider === "gemini" ? "Gemini" : "OpenRouter"} API: ${error.message || error}`,
    );
  }
}
