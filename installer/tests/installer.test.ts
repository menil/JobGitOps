import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { runInstallation } from "../src/installer.js";
import { execa } from "execa";
import fs from "fs-extra";
import path from "path";
import os from "os";
import { validateGraphqlRequests } from "./graphql-guard.js";

vi.mock("execa");

afterEach(validateGraphqlRequests);
vi.mock("tar", () => ({
  default: {
    x: vi.fn().mockResolvedValue(undefined),
  },
  x: vi.fn().mockResolvedValue(undefined),
}));

vi.mock("fs-extra", async (importOriginal) => {
  const actual = await importOriginal<typeof import("fs-extra")>();
  return {
    ...actual,
    default: {
      ...actual.default,
      copy: vi.fn().mockResolvedValue(undefined),
      pathExists: vi.fn().mockResolvedValue(true),
      writeFile: vi.fn().mockResolvedValue(undefined),
      readFile: vi
        .fn()
        .mockResolvedValue("Readme template containing __OWNER__ and __REPO__"),
      remove: vi.fn().mockResolvedValue(undefined),
      ensureDir: vi.fn().mockResolvedValue(undefined),
      existsSync: vi.fn().mockImplementation((p: string) => {
        if (p.endsWith("README.md") || p.endsWith("settings.yaml")) {
          return true;
        }
        return false;
      }),
      readFileSync: vi.fn().mockImplementation((p: string) => {
        if (p.endsWith("README.md")) {
          return "Link to project: https://github.com/testowner/job-search-test/projects";
        }
        if (p.endsWith("settings.yaml")) {
          return (
            '# projects_v2:\n#   project_id: ""\n#   status_field_name: ""\n\n' +
            "# gmail:\n" +
            "#   enabled: true\n" +
            '#   label: "GitEmployed"        # required when enabled\n' +
            "#                              # continuation comment line\n" +
            '#   query: ""                 # optional\n' +
            "#   days_back: 7               # lookback window\n" +
            "#                              # also the retention window\n" +
            "#                              # cursor.\n"
          );
        }
        return "";
      }),
      writeFileSync: vi.fn().mockReturnValue(undefined),
      readdir: vi.fn().mockImplementation(async (dirPath: string) => {
        if (dirPath.endsWith("extracted")) {
          return ["gitemployed-v0.6.0"];
        }
        return [
          "sync-template.yml",
          "ci.yml",
          "build-runner.yml",
          "pr-review.yml",
          "release-on-merge.yml",
          "test-workflow.yml",
        ];
      }),
      stat: vi.fn().mockImplementation(async (dirPath: string) => {
        return { isDirectory: () => true };
      }),
      statSync: vi.fn().mockImplementation((dirPath: string) => {
        return { isDirectory: () => true };
      }),
    },
  };
});

type GraphqlRouteHandler = (body: string) => {
  ok: boolean;
  json?: () => Promise<unknown>;
};

const createProjectV2Response = () => ({
  ok: true,
  json: async () => ({
    data: {
      createProjectV2: {
        projectV2: {
          id: "PVT_123",
          url: "https://github.com/users/testowner/projects/1",
        },
      },
    },
  }),
});

// Compares the parsed hostname rather than a substring match, since
// `url.includes("codeload.github.com")` would also match hosts like
// "codeload.github.com.evil.example".
function isCodeloadHost(url: string): boolean {
  try {
    return new URL(url).hostname === "codeload.github.com";
  } catch {
    return false;
  }
}

// Shared URL-routing fetch stub covering every endpoint runInstallation hits,
// so tests only customize the GraphQL responses they actually care about.
function mockGithubFetch(
  graphql: GraphqlRouteHandler = () => createProjectV2Response(),
) {
  return vi.fn().mockImplementation(async (url: string, init?: RequestInit) => {
    if (isCodeloadHost(url) || url.includes("/tarball/")) {
      return {
        ok: true,
        body: {
          getReader: () => ({
            read: () => Promise.resolve({ done: true, value: undefined }),
          }),
        },
      };
    }
    if (url.includes("/repos/testowner/job-search-test")) {
      return { ok: true, json: async () => ({ node_id: "R_repo_123" }) };
    }
    if (url.includes("/users/testowner")) {
      return { ok: true, json: async () => ({ node_id: "U_owner_123" }) };
    }
    if (url.includes("/graphql")) {
      return graphql(String(init?.body ?? ""));
    }
    if (url.includes("/gists")) {
      return { ok: true, json: async () => ({ id: "mock-gist-id" }) };
    }
    return { ok: false };
  });
}

describe("runInstallation", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("skips actions under dry-run mode", async () => {
    vi.mocked(execa).mockResolvedValue({ stdout: "mock-result" } as any);

    await runInstallation(
      {
        repoName: "job-search-test",
        visibility: "private",
        provider: "gemini",
        primaryKey: "mock-gemini-key",
        optionalKeys: {},
        wantProjects: false,
        dryRun: true,
      },
      "testowner",
    );

    // In dry-run, we only call execa to query the latest tag (which is run during tagging phase)
    // No repository creation, git push, or secrets setting are run.
    const execaCalls = vi.mocked(execa).mock.calls;
    const repoCreateCall = execaCalls.find((call) =>
      call[1]?.includes("create"),
    );
    expect(repoCreateCall).toBeUndefined();
  });

  it("performs full download, template copies, repo creation and secrets upload under live run", async () => {
    vi.mocked(execa).mockResolvedValue({ stdout: "mock-result" } as any);
    global.fetch = mockGithubFetch();

    await runInstallation(
      {
        repoName: "job-search-test",
        visibility: "private",
        provider: "gemini",
        primaryKey: "mock-gemini-key",
        optionalKeys: { tavily: "tavily-key" },
        wantProjects: true,
        dryRun: false,
      },
      "testowner",
    );

    // Verify repository create is called
    const execaCalls = vi.mocked(execa).mock.calls;
    const repoCreateCall = execaCalls.find(
      (call) => call[0] === "gh" && call[1]?.includes("create"),
    );
    expect(repoCreateCall).toBeDefined();
    expect(repoCreateCall?.[1]).toContain("job-search-test");

    // Verify secrets upload is called for gemini, tavily, and GH_PAT
    const secretCalls = execaCalls.filter(
      (call) =>
        call[0] === "gh" &&
        call[1]?.includes("secret") &&
        call[1]?.includes("set"),
    );
    expect(secretCalls.length).toBe(3);
    expect(secretCalls.some((call) => call[1]?.[2] === "GH_PAT")).toBe(true);
    expect(secretCalls.some((call) => call[1]?.[2] === "GEMINI_API_KEY")).toBe(
      true,
    );
    expect(secretCalls.some((call) => call[1]?.[2] === "TAVILY_API_KEY")).toBe(
      true,
    );

    // Verify GIST_ID variable upload is called
    const varCalls = execaCalls.filter(
      (call) =>
        call[0] === "gh" && call[1]?.some((arg) => arg.includes("variables")),
    );
    expect(varCalls.length).toBeGreaterThanOrEqual(1);
    expect(
      varCalls.some((call) => call[1]?.some((arg) => arg.includes("GIST_ID"))),
    ).toBe(true);

    // Verify that only core runtime workflows are copied (maintainer workflows excluded)
    const copyCalls = vi.mocked(fs.copy).mock.calls;
    const workflowCopyCalls = copyCalls.filter(
      (call) =>
        typeof call[0] === "string" && call[0].includes(".github/workflows"),
    );
    expect(workflowCopyCalls.length).toBe(1);
    expect(workflowCopyCalls[0][0]).toContain("test-workflow.yml");

    const scriptCopyCalls = copyCalls.filter(
      (call) =>
        typeof call[0] === "string" && call[0].includes(".github/scripts"),
    );
    expect(scriptCopyCalls.length).toBe(1);
  });

  it("performs secrets upload for GH_PAT even when wantProjects is false", async () => {
    vi.mocked(execa).mockResolvedValue({ stdout: "mock-result" } as any);
    global.fetch = mockGithubFetch();

    await runInstallation(
      {
        repoName: "job-search-test",
        visibility: "private",
        provider: "gemini",
        primaryKey: "mock-gemini-key",
        optionalKeys: {},
        wantProjects: false,
        dryRun: false,
      },
      "testowner",
    );

    const execaCalls = vi.mocked(execa).mock.calls;
    const secretCalls = execaCalls.filter(
      (call) =>
        call[0] === "gh" &&
        call[1]?.includes("secret") &&
        call[1]?.includes("set"),
    );
    // Should upload GEMINI_API_KEY and GH_PAT
    expect(secretCalls.length).toBe(2);
    expect(secretCalls.some((call) => call[1]?.[2] === "GH_PAT")).toBe(true);
    expect(secretCalls.some((call) => call[1]?.[2] === "GEMINI_API_KEY")).toBe(
      true,
    );
  });

  it("performs secrets upload for CLAUDE_CODE_OAUTH_TOKEN when provider is claude", async () => {
    vi.mocked(execa).mockResolvedValue({ stdout: "mock-result" } as any);
    global.fetch = mockGithubFetch();

    await runInstallation(
      {
        repoName: "job-search-test",
        visibility: "private",
        provider: "claude",
        primaryKey: "sk-ant-oat01-mock-key",
        optionalKeys: {},
        wantProjects: false,
        dryRun: false,
      },
      "testowner",
    );

    const execaCalls = vi.mocked(execa).mock.calls;
    const secretCalls = execaCalls.filter(
      (call) =>
        call[0] === "gh" &&
        call[1]?.includes("secret") &&
        call[1]?.includes("set"),
    );
    expect(secretCalls.length).toBe(2);
    expect(secretCalls.some((call) => call[1]?.[2] === "GH_PAT")).toBe(true);
    expect(
      secretCalls.some((call) => call[1]?.[2] === "CLAUDE_CODE_OAUTH_TOKEN"),
    ).toBe(true);
  });

  it("performs secrets upload for CLAUDE_CODE_OAUTH_TOKEN when provider is claude and key is API key", async () => {
    vi.mocked(execa).mockResolvedValue({ stdout: "mock-result" } as any);
    global.fetch = mockGithubFetch();

    await runInstallation(
      {
        repoName: "job-search-test",
        visibility: "private",
        provider: "claude",
        primaryKey: "sk-ant-api03-mock-api-key",
        optionalKeys: {},
        wantProjects: false,
        dryRun: false,
      },
      "testowner",
    );

    const execaCalls = vi.mocked(execa).mock.calls;
    const secretCalls = execaCalls.filter(
      (call) =>
        call[0] === "gh" &&
        call[1]?.includes("secret") &&
        call[1]?.includes("set"),
    );
    expect(secretCalls.length).toBe(2);
    expect(secretCalls.some((call) => call[1]?.[2] === "GH_PAT")).toBe(true);
    expect(
      secretCalls.some((call) => call[1]?.[2] === "CLAUDE_CODE_OAUTH_TOKEN"),
    ).toBe(true);
  });

  it("publishes the Projects V2 board when repository visibility is public", async () => {
    vi.mocked(execa).mockResolvedValue({ stdout: "mock-result" } as any);
    const fetchMock = mockGithubFetch((body) =>
      body.includes("updateProjectV2")
        ? {
            ok: true,
            json: async () => ({
              data: { updateProjectV2: { projectV2: { id: "PVT_123" } } },
            }),
          }
        : createProjectV2Response(),
    );
    global.fetch = fetchMock;

    await runInstallation(
      {
        repoName: "job-search-test",
        visibility: "public",
        provider: "gemini",
        primaryKey: "mock-gemini-key",
        optionalKeys: {},
        wantProjects: true,
        dryRun: false,
      },
      "testowner",
    );

    const updateBodies = fetchMock.mock.calls
      .filter(([url]) => String(url).includes("/graphql"))
      .map(([, init]) => String((init as RequestInit)?.body ?? ""))
      .filter((b) => b.includes("updateProjectV2"));
    expect(updateBodies).toHaveLength(1);
    expect(JSON.parse(updateBodies[0]).variables).toEqual({
      projectId: "PVT_123",
      public: true,
    });
  });

  it("keeps the Projects V2 board private when repository visibility is private", async () => {
    vi.mocked(execa).mockResolvedValue({ stdout: "mock-result" } as any);
    const fetchMock = mockGithubFetch();
    global.fetch = fetchMock;

    await runInstallation(
      {
        repoName: "job-search-test",
        visibility: "private",
        provider: "gemini",
        primaryKey: "mock-gemini-key",
        optionalKeys: {},
        wantProjects: true,
        dryRun: false,
      },
      "testowner",
    );

    const graphqlBodies = fetchMock.mock.calls
      .filter(([url]) => String(url).includes("/graphql"))
      .map(([, init]) => String((init as RequestInit)?.body ?? ""));
    expect(graphqlBodies.length).toBeGreaterThanOrEqual(1);
    expect(graphqlBodies.every((b) => !b.includes("updateProjectV2"))).toBe(
      true,
    );
  });

  it("continues installation when publishing the board fails on a public repository", async () => {
    vi.mocked(execa).mockResolvedValue({ stdout: "mock-result" } as any);
    global.fetch = mockGithubFetch((body) =>
      body.includes("updateProjectV2")
        ? { ok: false }
        : createProjectV2Response(),
    );

    await expect(
      runInstallation(
        {
          repoName: "job-search-test",
          visibility: "public",
          provider: "gemini",
          primaryKey: "mock-gemini-key",
          optionalKeys: {},
          wantProjects: true,
          dryRun: false,
        },
        "testowner",
      ),
    ).resolves.toBeUndefined();

    // The board was still created, so settings.yaml must be patched with its ID
    const settingsWrite = vi
      .mocked(fs.writeFileSync)
      .mock.calls.find(([p]) => String(p).endsWith("settings.yaml"));
    expect(settingsWrite).toBeDefined();
    expect(String(settingsWrite?.[1])).toContain("PVT_123");
  });

  it("uploads the three Gmail secrets and patches settings.yaml when wantGmail is true", async () => {
    vi.mocked(execa).mockResolvedValue({ stdout: "mock-result" } as any);
    global.fetch = mockGithubFetch();

    await runInstallation(
      {
        repoName: "job-search-test",
        visibility: "private",
        provider: "gemini",
        primaryKey: "mock-gemini-key",
        optionalKeys: {},
        wantProjects: false,
        wantGmail: true,
        gmailClientId: "gmail-client-id",
        gmailClientSecret: "gmail-client-secret",
        gmailRefreshToken: "gmail-refresh-token",
        gmailLabel: "MyLabel",
        dryRun: false,
      },
      "testowner",
    );

    const execaCalls = vi.mocked(execa).mock.calls;
    const secretCalls = execaCalls.filter(
      (call) =>
        call[0] === "gh" &&
        call[1]?.includes("secret") &&
        call[1]?.includes("set"),
    );
    for (const [name, value] of [
      ["GMAIL_CLIENT_ID", "gmail-client-id"],
      ["GMAIL_CLIENT_SECRET", "gmail-client-secret"],
      ["GMAIL_REFRESH_TOKEN", "gmail-refresh-token"],
    ]) {
      const call = secretCalls.find((c) => c[1]?.[2] === name);
      expect(call).toBeDefined();
      expect((call?.[2] as any)?.input).toBe(value);
    }

    const settingsWrite = vi
      .mocked(fs.writeFileSync)
      .mock.calls.find(([p]) => String(p).endsWith("settings.yaml"));
    expect(settingsWrite).toBeDefined();
    const written = String(settingsWrite?.[1]);
    expect(written).toContain("gmail:");
    expect(written).toContain("enabled: true");
    expect(written).toContain('label: "MyLabel"');
    // The commented `# gmail:` block (and its continuation-comment lines)
    // from the fixture must be replaced in place, not left behind with the
    // enabled block merely appended after it -- proves the regex-replace
    // path fired, not the append fallback.
    expect(written).not.toContain("#   enabled: true");
    expect(written).not.toContain("continuation comment line");
    // The unrelated, still-commented `# projects_v2:` block above it must
    // survive untouched -- only the `# gmail:` block was replaced.
    expect(written).toContain('#   project_id: ""');
  });

  it("escapes a label containing YAML/regex-replacement-special characters safely", async () => {
    vi.mocked(execa).mockResolvedValue({ stdout: "mock-result" } as any);
    global.fetch = mockGithubFetch();

    await runInstallation(
      {
        repoName: "job-search-test",
        visibility: "private",
        provider: "gemini",
        primaryKey: "mock-gemini-key",
        optionalKeys: {},
        wantProjects: false,
        wantGmail: true,
        gmailClientId: "gmail-client-id",
        gmailClientSecret: "gmail-client-secret",
        gmailRefreshToken: "gmail-refresh-token",
        // `$&` would expand to the whole matched block under a naive
        // `String.replace(pattern, stringWithDollarSign)` call; a trailing
        // backslash would break the YAML double-quoted string if the
        // backslash itself weren't escaped before the closing quote.
        gmailLabel: "Job$&Search\\",
        dryRun: false,
      },
      "testowner",
    );

    const settingsWrite = vi
      .mocked(fs.writeFileSync)
      .mock.calls.find(([p]) => String(p).endsWith("settings.yaml"));
    expect(settingsWrite).toBeDefined();
    const written = String(settingsWrite?.[1]);
    expect(written).toContain('label: "Job$&Search\\\\"');
    expect(written).not.toContain("#   enabled: true");
  });

  it("does not crash and skips the settings.yaml patch in --dry-run mode even with wantGmail true", async () => {
    vi.mocked(execa).mockResolvedValue({ stdout: "mock-result" } as any);
    global.fetch = mockGithubFetch();

    await expect(
      runInstallation(
        {
          repoName: "job-search-test",
          visibility: "private",
          provider: "gemini",
          primaryKey: "mock-gemini-key",
          optionalKeys: {},
          wantProjects: false,
          wantGmail: true,
          gmailClientId: "gmail-client-id",
          gmailClientSecret: "gmail-client-secret",
          gmailRefreshToken: "gmail-refresh-token",
          gmailLabel: "GitEmployed",
          dryRun: true,
        },
        "testowner",
      ),
    ).resolves.toBeUndefined();

    const settingsWrite = vi
      .mocked(fs.writeFileSync)
      .mock.calls.find(([p]) => String(p).endsWith("settings.yaml"));
    expect(settingsWrite).toBeUndefined();
  });

  it("does not upload Gmail secrets or patch settings.yaml when wantGmail is false", async () => {
    vi.mocked(execa).mockResolvedValue({ stdout: "mock-result" } as any);
    global.fetch = mockGithubFetch();

    await runInstallation(
      {
        repoName: "job-search-test",
        visibility: "private",
        provider: "gemini",
        primaryKey: "mock-gemini-key",
        optionalKeys: {},
        wantProjects: false,
        wantGmail: false,
        dryRun: false,
      },
      "testowner",
    );

    const execaCalls = vi.mocked(execa).mock.calls;
    const secretCalls = execaCalls.filter(
      (call) =>
        call[0] === "gh" &&
        call[1]?.includes("secret") &&
        call[1]?.includes("set"),
    );
    expect(secretCalls.some((c) => c[1]?.[2] === "GMAIL_CLIENT_ID")).toBe(
      false,
    );
    expect(secretCalls.some((c) => c[1]?.[2] === "GMAIL_REFRESH_TOKEN")).toBe(
      false,
    );

    const settingsWrite = vi
      .mocked(fs.writeFileSync)
      .mock.calls.find(([p]) => String(p).endsWith("settings.yaml"));
    expect(settingsWrite).toBeUndefined();
  });

  it("does not upload partial Gmail secrets if wantGmail is true but a credential is missing", async () => {
    vi.mocked(execa).mockResolvedValue({ stdout: "mock-result" } as any);
    global.fetch = mockGithubFetch();

    await runInstallation(
      {
        repoName: "job-search-test",
        visibility: "private",
        provider: "gemini",
        primaryKey: "mock-gemini-key",
        optionalKeys: {},
        wantProjects: false,
        wantGmail: true,
        gmailClientId: "gmail-client-id",
        gmailClientSecret: "",
        gmailRefreshToken: "gmail-refresh-token",
        gmailLabel: "GitEmployed",
        dryRun: false,
      },
      "testowner",
    );

    const execaCalls = vi.mocked(execa).mock.calls;
    const secretCalls = execaCalls.filter(
      (call) =>
        call[0] === "gh" &&
        call[1]?.includes("secret") &&
        call[1]?.includes("set"),
    );
    expect(secretCalls.some((c) => c[1]?.[2] === "GMAIL_CLIENT_ID")).toBe(
      false,
    );
    expect(secretCalls.some((c) => c[1]?.[2] === "GMAIL_REFRESH_TOKEN")).toBe(
      false,
    );

    // Regression test: settings.yaml must not end up with `gmail.enabled:
    // true` when the secrets it depends on weren't actually uploaded --
    // that would leave a repo silently broken (cron fails every hour with
    // no signal at install time). The settings patch must be gated on the
    // same completeness check as the secret upload.
    const settingsWrite = vi
      .mocked(fs.writeFileSync)
      .mock.calls.find(([p]) => String(p).endsWith("settings.yaml"));
    expect(settingsWrite).toBeUndefined();
  });

  it("fails installation and propagates error when download fails", async () => {
    const mockError = new Error("GH CLI error");
    // Mock public download to throw/reject
    global.fetch = vi.fn().mockRejectedValue(new Error("Network failure"));

    // Mock execa to reject when fetching the tarball
    vi.mocked(execa).mockImplementation(async (bin, args) => {
      if (
        bin === "gh" &&
        args &&
        args[0] === "api" &&
        args[1].includes("tarball")
      ) {
        throw mockError;
      }
      return { stdout: "mock-result" } as any;
    });

    await expect(
      runInstallation(
        {
          repoName: "job-search-test",
          visibility: "private",
          provider: "gemini",
          primaryKey: "mock-gemini-key",
          optionalKeys: {},
          wantProjects: false,
          tag: "latest",
          dryRun: false,
        },
        "testowner",
      ),
    ).rejects.toThrow(
      "Failed to download GitEmployed tarball for 'latest': GH CLI error",
    );

    // Also assert cause is correct
    try {
      await runInstallation(
        {
          repoName: "job-search-test",
          visibility: "private",
          provider: "gemini",
          primaryKey: "mock-gemini-key",
          optionalKeys: {},
          wantProjects: false,
          tag: "latest",
          dryRun: false,
        },
        "testowner",
      );
    } catch (err: any) {
      expect(err.cause).toBe(mockError);
    }
  });
});
