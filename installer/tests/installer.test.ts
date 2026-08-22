import { describe, it, expect, vi, beforeEach } from "vitest";
import { runInstallation } from "../src/installer.js";
import { execa } from "execa";
import fs from "fs-extra";
import path from "path";
import os from "os";

vi.mock("execa");
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
          return '# projects_v2:\n#   project_id: ""\n#   status_field_name: ""';
        }
        return "";
      }),
      writeFileSync: vi.fn().mockReturnValue(undefined),
      readdir: vi.fn().mockImplementation(async (dirPath: string) => {
        if (dirPath.endsWith("extracted")) {
          return ["jobgitops-v0.6.0"];
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
    // Stub global fetch to prevent actual network calls during downloadTarball and other API calls
    global.fetch = vi.fn().mockImplementation(async (url: string) => {
      if (url.includes("codeload.github.com") || url.includes("/tarball/")) {
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
        return {
          ok: true,
          json: async () => ({ node_id: "R_repo_123" }),
        };
      }
      if (url.includes("/users/testowner")) {
        return {
          ok: true,
          json: async () => ({ node_id: "U_owner_123" }),
        };
      }
      if (url.includes("/graphql")) {
        return {
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
        };
      }
      if (url.includes("/gists")) {
        return {
          ok: true,
          json: async () => ({ id: "mock-gist-id" }),
        };
      }
      return { ok: false };
    });

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
    global.fetch = vi.fn().mockImplementation(async (url: string) => {
      if (url.includes("codeload.github.com") || url.includes("/tarball/")) {
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
        return {
          ok: true,
          json: async () => ({ node_id: "R_repo_123" }),
        };
      }
      if (url.includes("/gists")) {
        return {
          ok: true,
          json: async () => ({ id: "mock-gist-id" }),
        };
      }
      return { ok: false };
    });

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
});
