import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  fetchGithubUser,
  verifyAndRefreshScopes,
  validateApiKey,
  createProjectV2,
  fetchRepositoryNodeId,
} from "../src/api.js";
import { execa } from "execa";

vi.mock("execa");

describe("fetchGithubUser", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("successfully parses login and scopes from raw include output", async () => {
    const rawIncludeOutput =
      "HTTP/2 200 OK\r\n" +
      "server: github.com\r\n" +
      "x-oauth-scopes: repo, workflow, write:discussion\r\n" +
      "\r\n" +
      '{"login": "testuser", "id": 12345}';

    vi.mocked(execa).mockResolvedValue({
      stdout: rawIncludeOutput,
    } as any);

    const result = await fetchGithubUser();
    expect(result.login).toBe("testuser");
    expect(result.scopes).toEqual(["repo", "workflow", "write:discussion"]);
  });

  it("handles case-insensitivity in headers", async () => {
    const rawIncludeOutput =
      "HTTP/2 200 OK\r\n" +
      "X-OAuth-Scopes: project, repo\r\n" +
      "\r\n" +
      '{"login": "anotheruser"}';

    vi.mocked(execa).mockResolvedValue({
      stdout: rawIncludeOutput,
    } as any);

    const result = await fetchGithubUser();
    expect(result.login).toBe("anotheruser");
    expect(result.scopes).toEqual(["project", "repo"]);
  });

  it("falls back to jq login check if body parsing fails", async () => {
    const rawIncludeOutput =
      "HTTP/2 200 OK\r\n" +
      "x-oauth-scopes: repo\r\n" +
      "\r\n" +
      "invalid-json-body";

    vi.mocked(execa)
      .mockResolvedValueOnce({ stdout: rawIncludeOutput } as any)
      .mockResolvedValueOnce({ stdout: "fallbackuser\n" } as any);

    const result = await fetchGithubUser();
    expect(result.login).toBe("fallbackuser");
    expect(result.scopes).toEqual(["repo"]);
  });
});

describe("verifyAndRefreshScopes", () => {
  const dummyUser = { login: "testuser", scopes: ["repo", "workflow"] };

  it("succeeds directly when all required scopes are present", async () => {
    const result = await verifyAndRefreshScopes(dummyUser, false, false, false);
    expect(result).toBe(dummyUser);
  });

  it("warns and continues if scopes are empty (e.g. fine-grained PAT)", async () => {
    const fineGrainedUser = { login: "testuser", scopes: [] };
    const result = await verifyAndRefreshScopes(
      fineGrainedUser,
      false,
      false,
      false,
    );
    expect(result).toBe(fineGrainedUser);
  });

  it("throws error if scopes are missing and env token is configured", async () => {
    const missingUser = { login: "testuser", scopes: ["repo"] };
    await expect(
      verifyAndRefreshScopes(missingUser, false, true, true),
    ).rejects.toThrow(
      "Your GitHub token is missing the required scope(s): workflow.",
    );
  });

  it("throws error if scopes are missing and non-interactive", async () => {
    const missingUser = { login: "testuser", scopes: ["repo"] };
    await expect(
      verifyAndRefreshScopes(missingUser, false, false, false),
    ).rejects.toThrow(
      "Your GitHub token is missing the required scope(s): workflow.",
    );
  });

  it("warns and returns if interactive but missing scopes (handled by caller)", async () => {
    const missingUser = { login: "testuser", scopes: ["repo"] };
    const result = await verifyAndRefreshScopes(
      missingUser,
      false,
      false,
      true,
    );
    expect(result).toBe(missingUser);
  });
});

describe("validateApiKey", () => {
  it("succeeds if response is OK for Gemini", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
    } as any);

    await expect(validateApiKey("gemini", "valid-key")).resolves.not.toThrow();
    expect(global.fetch).toHaveBeenCalledWith(
      "https://generativelanguage.googleapis.com/v1beta/models?key=valid-key",
      { headers: undefined },
    );
  });

  it("succeeds if response is OK for OpenRouter", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
    } as any);

    await expect(
      validateApiKey("openrouter", "valid-key"),
    ).resolves.not.toThrow();
    expect(global.fetch).toHaveBeenCalledWith(
      "https://openrouter.ai/api/v1/auth/key",
      { headers: { Authorization: "Bearer valid-key" } },
    );
  });

  it("throws key rejected error if status is 400, 401, or 403", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 403,
    } as any);

    await expect(validateApiKey("gemini", "bad-key")).rejects.toThrow(
      "Gemini key rejected by the API (HTTP 403)",
    );
  });

  it("throws general API error if status is different", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 500,
    } as any);

    await expect(validateApiKey("openrouter", "key")).rejects.toThrow(
      "API returned HTTP 500",
    );
  });

  it("throws network/transport error if fetch fails", async () => {
    global.fetch = vi
      .fn()
      .mockRejectedValue(new Error("DNS Resolution Failure"));

    await expect(validateApiKey("gemini", "key")).rejects.toThrow(
      "Could not reach the Gemini API: DNS Resolution Failure",
    );
  });
});

describe("createProjectV2", () => {
  const originalFetch = global.fetch;

  beforeEach(() => {
    vi.clearAllMocks();
    // Mock getGhCliToken (uses execa) to return a fake token
    vi.mocked(execa).mockResolvedValue({ stdout: "ghp_fake_token\n" } as any);
  });

  afterEach(() => {
    global.fetch = originalFetch;
  });

  it("returns project node ID on success", async () => {
    global.fetch = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ node_id: "U_xxxx" }),
      } as any)
      .mockResolvedValueOnce({
        ok: true,
        json: () =>
          Promise.resolve({
            data: {
              createProjectV2: {
                projectV2: {
                  id: "PVT_1234",
                  url: "https://github.com/users/testuser/projects/1",
                },
              },
            },
          }),
      } as any);

    const result = await createProjectV2(
      "testuser",
      "my-job-search",
      "R_repo_123",
    );
    expect(result).toEqual({
      id: "PVT_1234",
      url: "https://github.com/users/testuser/projects/1",
    });
  });

  it("returns null if owner fetch fails", async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: false,
      status: 404,
    } as any);

    const result = await createProjectV2("testuser", "my-job-search");
    expect(result).toBeNull();
  });

  it("returns null if GraphQL response has errors", async () => {
    global.fetch = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ node_id: "U_xxxx" }),
      } as any)
      .mockResolvedValueOnce({
        ok: true,
        json: () =>
          Promise.resolve({
            errors: [{ message: "Forbidden" }],
          }),
      } as any);

    const result = await createProjectV2("testuser", "my-job-search");
    expect(result).toBeNull();
  });

  it("returns null if response is missing projectV2.id", async () => {
    global.fetch = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ node_id: "U_xxxx" }),
      } as any)
      .mockResolvedValueOnce({
        ok: true,
        json: () => Promise.resolve({ data: { createProjectV2: null } }),
      } as any);

    const result = await createProjectV2("testuser", "my-job-search");
    expect(result).toBeNull();
  });

  it("throws if ownerLogin or title is empty", async () => {
    await expect(createProjectV2("", "title")).rejects.toThrow(
      "ownerLogin and title are required.",
    );
    await expect(createProjectV2("user", "")).rejects.toThrow(
      "ownerLogin and title are required.",
    );
  });
});

describe("fetchRepositoryNodeId", () => {
  const originalFetch = global.fetch;

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(execa).mockResolvedValue({ stdout: "ghp_fake_token\n" } as any);
  });

  afterEach(() => {
    global.fetch = originalFetch;
  });

  it("returns repo node ID on success", async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ node_id: "R_repo_123" }),
    } as any);

    const result = await fetchRepositoryNodeId("owner", "repo");
    expect(result).toBe("R_repo_123");
  });

  it("returns null if API request fails", async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: false,
      status: 404,
    } as any);

    const result = await fetchRepositoryNodeId("owner", "repo");
    expect(result).toBeNull();
  });

  it("returns null if owner or repo is missing", async () => {
    const result1 = await fetchRepositoryNodeId("", "repo");
    expect(result1).toBeNull();

    const result2 = await fetchRepositoryNodeId("owner", "");
    expect(result2).toBeNull();
  });
});
