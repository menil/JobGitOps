import { execa } from "execa";
import { LLMProvider, getProviderLabel } from "./constants.js";

export async function getGhCliToken(): Promise<string> {
  try {
    const { stdout } = await execa("gh", ["auth", "token"]);
    return stdout.trim();
  } catch {
    throw new Error(
      "Not authenticated with GitHub CLI. Please run 'gh auth login' or pass a --token flag.",
    );
  }
}

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
    throw new Error(`GitHub authentication failed: ${error.message || error}`, {
      cause: error,
    });
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
  const required = ["repo", "workflow", "gist"];
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

/**
 * Fetches the node ID of a repository via REST API.
 */
export async function fetchRepositoryNodeId(
  owner: string,
  repo: string,
  token?: string,
): Promise<string | null> {
  if (!owner || !repo) {
    return null;
  }
  const resolvedToken = token || (await getGhCliToken());
  const authHeaders = {
    Authorization: `Bearer ${resolvedToken}`,
    Accept: "application/vnd.github+json",
  };
  try {
    const resp = await fetch(`https://api.github.com/repos/${owner}/${repo}`, {
      headers: authHeaders,
    });
    if (!resp.ok) {
      console.error(`Failed to fetch repository node ID: HTTP ${resp.status}`);
      return null;
    }
    const data = (await resp.json()) as { node_id?: string };
    return data?.node_id || null;
  } catch (error: any) {
    console.error(
      `Failed to fetch repository node ID: ${error.message || error}`,
    );
    return null;
  }
}

const GITHUB_GRAPHQL_ENDPOINT = "https://api.github.com/graphql";

/**
 * Posts a GraphQL operation to the GitHub API. Centralizes the endpoint, auth
 * header propagation, and JSON envelope so callers only supply the operation
 * text and variables.
 */
async function postGraphql(
  authHeaders: Record<string, string>,
  query: string,
  variables: unknown,
): Promise<Response> {
  return fetch(GITHUB_GRAPHQL_ENDPOINT, {
    method: "POST",
    headers: { ...authHeaders, "Content-Type": "application/json" },
    body: JSON.stringify({ query, variables }),
  });
}

/**
 * Creates a GitHub Projects V2 board owned by the given user via the GraphQL API,
 * optionally linking it to the repository.
 * When `visibility` is "public", flips the board to public after creation so the
 * README link works for visitors of a public repository — the createProjectV2
 * mutation has no visibility input and always defaults to private.
 * Returns the project node ID and URL on success, or null on failure.
 */
export async function createProjectV2(
  ownerLogin: string,
  title: string,
  repositoryId?: string,
  token?: string,
  visibility?: "private" | "public",
): Promise<{ id: string; url: string } | null> {
  if (!ownerLogin || !title) {
    throw new Error("ownerLogin and title are required.");
  }

  const resolvedToken = token || (await getGhCliToken());
  const authHeaders = {
    Authorization: `Bearer ${resolvedToken}`,
    Accept: "application/vnd.github+json",
    "X-Github-Next-Global-ID": "1",
  };

  try {
    // Resolve the owner's node ID via REST
    const userResp = await fetch(`https://api.github.com/users/${ownerLogin}`, {
      headers: authHeaders,
    });
    if (!userResp.ok) {
      throw new Error(`Could not resolve owner (HTTP ${userResp.status}).`);
    }
    const userData = (await userResp.json()) as { node_id?: string };
    const ownerId = userData?.node_id;
    if (!ownerId) {
      throw new Error("Owner response missing node_id.");
    }

    // Create the project via GraphQL
    const query = `
      mutation($ownerId: ID!, $title: String!, $repositoryId: ID) {
        createProjectV2(input: { ownerId: $ownerId, title: $title, repositoryId: $repositoryId }) {
          projectV2 { id url }
        }
      }
    `;
    const graphqlResp = await postGraphql(authHeaders, query, {
      ownerId,
      title,
      repositoryId,
    });
    if (!graphqlResp.ok) {
      throw new Error(`GraphQL request failed (HTTP ${graphqlResp.status}).`);
    }

    const result = (await graphqlResp.json()) as {
      data?: {
        createProjectV2?: { projectV2?: { id?: string; url?: string } };
      };
      errors?: Array<{ message: string }>;
    };
    if (result.errors?.length) {
      throw new Error(`GraphQL error: ${result.errors[0].message}`);
    }
    const projectV2 = result?.data?.createProjectV2?.projectV2;
    if (!projectV2?.id || !projectV2?.url) {
      throw new Error(
        "GraphQL response missing projectV2.id or projectV2.url.",
      );
    }
    if (visibility === "public") {
      await setProjectV2Public(projectV2.id, authHeaders);
    }
    await configureProjectV2KanbanView(projectV2.id, authHeaders);
    return { id: projectV2.id, url: projectV2.url };
  } catch (error: any) {
    console.error(`Failed to create Projects V2: ${error.message || error}`);
    return null;
  }
}

/**
 * Best-effort flip of a Projects V2 board to public via GraphQL. Failures only
 * warn: a private board degrades the README link but does not break the install.
 */
async function setProjectV2Public(
  projectId: string,
  authHeaders: Record<string, string>,
): Promise<void> {
  const query = `
    mutation($projectId: ID!, $public: Boolean!) {
      updateProjectV2(input: { projectId: $projectId, public: $public }) {
        projectV2 { id }
      }
    }
  `;
  try {
    const resp = await postGraphql(authHeaders, query, {
      projectId,
      public: true,
    });
    if (!resp.ok) {
      console.error(
        `Could not set project visibility to public (HTTP ${resp.status}). The board stays private.`,
      );
      return;
    }
    const result = (await resp.json()) as {
      errors?: Array<{ message: string }>;
    };
    if (result.errors?.length) {
      console.error(
        `Could not set project visibility to public: ${result.errors[0].message}`,
      );
    }
  } catch (error: any) {
    console.error(
      `Could not set project visibility to public: ${error.message || error}`,
    );
  }
}

const KANBAN_VIEW_ERROR = "Could not switch the project board to a Kanban view";
const KANBAN_VIEW_WARNING = `${KANBAN_VIEW_ERROR}; keeping the default Table view`;

interface GraphqlResult {
  data?: unknown;
  errors?: Array<{ message: string }>;
}

/**
 * Parses a best-effort GraphQL response, warning and returning null when the
 * HTTP status or the GraphQL errors array indicates the operation failed.
 * `contextMessage` names the operation for log output.
 */
async function readGraphqlResult(
  resp: Response,
  contextMessage: string,
): Promise<GraphqlResult | null> {
  if (!resp.ok) {
    console.error(`${contextMessage} (HTTP ${resp.status})`);
    return null;
  }
  const result = (await resp.json()) as GraphqlResult;
  if (result.errors?.length) {
    console.error(`${contextMessage}: ${result.errors[0].message}`);
    return null;
  }
  return result;
}

/**
 * Best-effort switch of a freshly created Projects V2 board's default view
 * from the initial Table layout to a Kanban Board. Views are matched by
 * layout rather than name so renamed Table views are still found. GitHub
 * requires at least one live view per project, so the order is discover ->
 * create -> delete. Every failure path warns; a degraded board never breaks
 * the install.
 */
async function configureProjectV2KanbanView(
  projectId: string,
  authHeaders: Record<string, string>,
): Promise<void> {
  try {
    // 1. Discover the initial Table view. first: 10 is ample for a freshly
    // created board (one view); revisit with pagination if this ever runs on
    // existing boards.
    const viewsResp = await postGraphql(
      authHeaders,
      `
        query($projectId: ID!) {
          node(id: $projectId) {
            ... on ProjectV2 {
              views(first: 10) {
                nodes { id layout }
              }
            }
          }
        }
      `,
      { projectId },
    );
    const viewsResult = await readGraphqlResult(viewsResp, KANBAN_VIEW_WARNING);
    if (!viewsResult) {
      return;
    }
    const viewsData = viewsResult.data as
      | {
          node?: {
            views?: { nodes?: Array<{ id?: string; layout?: string }> };
          };
        }
      | undefined;
    const tableView = viewsData?.node?.views?.nodes?.find(
      (view) => view.layout === "TABLE_LAYOUT",
    );
    if (!tableView?.id) {
      console.error(
        `${KANBAN_VIEW_ERROR}: no Table layout view found to replace. The board keeps its current default view.`,
      );
      return;
    }

    // 2. Create the Kanban Board replacement — but only when the board has no
    // board view yet, otherwise we would leave two board layouts behind.
    // GitHub requires at least one live view per project, so a replacement
    // must exist before the Table view is deleted; an existing board view
    // already satisfies that invariant.
    const hasBoardView = viewsData?.node?.views?.nodes?.some(
      (view) => view.layout === "BOARD_LAYOUT",
    );
    if (!hasBoardView) {
      const createResp = await postGraphql(
        authHeaders,
        `
          mutation($projectId: ID!, $name: String!, $layout: ProjectV2ViewLayout!) {
            createProjectV2View(
              input: { projectId: $projectId, name: $name, layout: $layout }
            ) {
              projectV2View { id name }
            }
          }
        `,
        { projectId, name: "Kanban Board", layout: "BOARD_LAYOUT" },
      );
      const createResult = await readGraphqlResult(
        createResp,
        KANBAN_VIEW_WARNING,
      );
      if (!createResult) {
        return;
      }
      // A malformed creation payload must never trigger deleting the only
      // other live view on the board.
      const createdView = (
        createResult.data as
          | { createProjectV2View?: { projectV2View?: { id?: string } } }
          | undefined
      )?.createProjectV2View?.projectV2View;
      if (!createdView?.id) {
        console.error(
          `${KANBAN_VIEW_ERROR}: Board view creation returned no view ID. The default Table view is kept.`,
        );
        return;
      }
    }

    // 3. Delete the now-redundant Table view
    const deleteResp = await postGraphql(
      authHeaders,
      `
        mutation($viewId: ID!) {
          deleteProjectV2View(input: { viewId: $viewId }) {
            projectV2View { id name }
          }
        }
      `,
      { viewId: tableView.id },
    );
    await readGraphqlResult(deleteResp, KANBAN_VIEW_WARNING);
  } catch (error: any) {
    console.error(`${KANBAN_VIEW_ERROR}: ${error.message || error}`);
  }
}

/**
 * Creates a GitHub Gist with the provided files.
 * Returns the gist ID on success, or null on failure.
 */
export async function createGist(
  description: string,
  files: Record<string, { content: string }>,
  token?: string,
): Promise<string | null> {
  const resolvedToken = token || (await getGhCliToken());
  const authHeaders = {
    Authorization: `Bearer ${resolvedToken}`,
    Accept: "application/vnd.github+json",
    "Content-Type": "application/json",
  };

  try {
    const resp = await fetch("https://api.github.com/gists", {
      method: "POST",
      headers: authHeaders,
      body: JSON.stringify({
        description,
        public: true,
        files,
      }),
    });
    if (!resp.ok) {
      console.error(`Failed to create Gist: HTTP ${resp.status}`);
      return null;
    }
    const result = (await resp.json()) as { id?: string };
    return result?.id || null;
  } catch (error: any) {
    console.error(`Failed to create Gist: ${error.message || error}`);
    return null;
  }
}

function picocolorsBold(str: string): string {
  // Simple helper to avoid importing colors inside api.ts if not needed,
  // but we can import it if we want.
  return `\x1b[1m${str}\x1b[22m`;
}

/**
 * Detects whether a Claude credential is an OAuth subscription token
 * (e.g. generated via `claude setup-token`, prefixed with `sk-ant-oat`)
 * requiring Bearer authentication and beta headers, versus a standard API key.
 */
export function isClaudeOAuthToken(apiKey: string): boolean {
  return apiKey.trim().startsWith("sk-ant-oat");
}

/**
 * Validates the primary LLM API key by executing a fast mock query against
 * the provider's official model-listing endpoints.
 */
export async function validateApiKey(
  provider: LLMProvider,
  apiKey: string,
): Promise<void> {
  let url: string;
  let headers: Record<string, string> | undefined;

  if (provider === "gemini") {
    url = `https://generativelanguage.googleapis.com/v1beta/models?key=${apiKey}`;
  } else if (provider === "openrouter") {
    url = "https://openrouter.ai/api/v1/auth/key";
    headers = { Authorization: `Bearer ${apiKey}` };
  } else {
    url = "https://api.anthropic.com/v1/models";
    headers = {
      "anthropic-version": "2023-06-01",
    };
    if (isClaudeOAuthToken(apiKey)) {
      headers["Authorization"] = `Bearer ${apiKey}`;
      headers["anthropic-beta"] = "claude-code-20250219,oauth-2025-04-20";
    } else {
      headers["x-api-key"] = apiKey;
    }
  }

  const providerLabel = getProviderLabel(provider);

  try {
    const response = await fetch(url, { headers });
    try {
      if (!response.ok) {
        if (
          response.status === 401 ||
          response.status === 403 ||
          response.status === 400
        ) {
          throw new Error(
            `${providerLabel} key rejected by the API (HTTP ${response.status})`,
          );
        }
        throw new Error(`API returned HTTP ${response.status}`);
      }
    } finally {
      if (response.body) {
        try {
          await response.body.cancel();
        } catch {
          // ignore
        }
      }
    }
  } catch (error: any) {
    if (error?.message && error.message.includes("rejected")) {
      throw error;
    }
    // Network or transport failure
    throw new Error(
      `Could not reach the ${providerLabel} API: ${error.message || error}`,
      { cause: error },
    );
  }
}
