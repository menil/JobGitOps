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
      writeFile: vi.fn().mockResolvedValue(undefined),
      readFile: vi
        .fn()
        .mockResolvedValue("Readme template containing __OWNER__ and __REPO__"),
      remove: vi.fn().mockResolvedValue(undefined),
      ensureDir: vi.fn().mockResolvedValue(undefined),
      readdir: vi.fn().mockImplementation(async (dirPath: string) => {
        if (dirPath.includes("extracted")) {
          return ["jobgitops-v0.6.0"];
        }
        return ["sync-template.yml", "test-workflow.yml"];
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
    // Stub global fetch to prevent actual network calls during downloadTarball
    const mockResponse = {
      ok: true,
      body: {
        getReader: () => ({
          read: () => Promise.resolve({ done: true, value: undefined }),
        }),
      },
    };
    global.fetch = vi.fn().mockResolvedValue(mockResponse);

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

    // Verify secrets upload is called for gemini, tavily, and PROJECT_V2_TOKEN
    const secretCalls = execaCalls.filter(
      (call) =>
        call[0] === "gh" &&
        call[1]?.includes("secret") &&
        call[1]?.includes("set"),
    );
    expect(secretCalls.length).toBe(3);
  });
});
