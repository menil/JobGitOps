import { describe, it, expect, vi, afterEach } from "vitest";

vi.mock("open", () => ({ default: vi.fn() }));

// resolveGmailSetup pulls in `prompts.ts` (real @inquirer/prompts, which
// would hang waiting on stdin) and `ora` (real terminal spinners); neither
// is exercised via runGmailOAuthFlow directly, so mock the module boundary
// rather than pull all of that into this file's tests.
vi.mock("../src/prompts.js", () => ({
  promptGmailSetup: vi.fn(),
  promptGmailClientId: vi.fn(),
  promptGmailClientSecret: vi.fn(),
  promptGmailLabel: vi.fn(),
}));

import open from "open";
import { runGmailOAuthFlow, GmailOAuthError } from "../src/gmailOAuth.js";

const mockedOpen = vi.mocked(open);
const realFetch = global.fetch;

// Compares the parsed hostname rather than a substring match, since
// `url.includes("oauth2.googleapis.com")` would also match hosts like
// "oauth2.googleapis.com.evil.example" (mirrors installer.test.ts's
// `isCodeloadHost` helper for the same class of check).
function isGoogleTokenEndpoint(url: string): boolean {
  try {
    return new URL(url).hostname === "oauth2.googleapis.com";
  } catch {
    return false;
  }
}

/**
 * Mocks only the outbound call to Google's token endpoint; every other
 * fetch (in particular, the redirect-simulation call to the real local
 * loopback server below) goes through the real, unmocked `fetch`, so the
 * server-listening code under test actually runs end to end.
 */
function mockTokenExchange(response: unknown, ok = true) {
  global.fetch = vi.fn(async (url: any, init?: any) => {
    if (isGoogleTokenEndpoint(String(url))) {
      return {
        ok,
        statusText: ok ? "OK" : "Bad Request",
        json: async () => response,
      } as any;
    }
    return realFetch(url as any, init);
  }) as any;
}

/** Simulates the browser completing consent and Google redirecting back to
 * the loopback server, by extracting the real ephemeral redirect_uri/port
 * `runGmailOAuthFlow` picked and hitting it directly. The real `state`
 * value is included automatically unless the caller overrides it via
 * `params.state`, matching what a genuine Google redirect would carry. */
function simulateBrowserRedirect(params: Record<string, string> = {}) {
  mockedOpen.mockImplementation(async (url: any) => {
    const authUrl = new URL(String(url));
    const redirectUri = authUrl.searchParams.get("redirect_uri")!;
    const target = new URL(redirectUri);
    target.searchParams.set("state", authUrl.searchParams.get("state")!);
    for (const [key, value] of Object.entries(params)) {
      target.searchParams.set(key, value);
    }
    await realFetch(target.toString());
    return {} as any;
  });
}

afterEach(() => {
  vi.restoreAllMocks();
  global.fetch = realFetch;
});

describe("runGmailOAuthFlow", () => {
  it("returns the refresh token on a successful consent + exchange", async () => {
    mockTokenExchange({ refresh_token: "rt-123", access_token: "at-123" });
    simulateBrowserRedirect({ code: "auth-code-abc" });

    const token = await runGmailOAuthFlow("client-id", "client-secret");

    expect(token).toBe("rt-123");
  });

  it("opens the browser to an auth URL with the correct scope, offline consent params, and a state value", async () => {
    mockTokenExchange({ refresh_token: "rt-123" });
    simulateBrowserRedirect({ code: "auth-code-abc" });

    await runGmailOAuthFlow("client-id-xyz", "client-secret");

    expect(mockedOpen).toHaveBeenCalledTimes(1);
    const openedUrl = new URL(String(mockedOpen.mock.calls[0][0]));
    expect(openedUrl.origin).toBe("https://accounts.google.com");
    expect(openedUrl.searchParams.get("client_id")).toBe("client-id-xyz");
    expect(openedUrl.searchParams.get("scope")).toBe(
      "https://www.googleapis.com/auth/gmail.readonly",
    );
    expect(openedUrl.searchParams.get("access_type")).toBe("offline");
    expect(openedUrl.searchParams.get("prompt")).toBe("consent");
    expect(openedUrl.searchParams.get("redirect_uri")).toMatch(
      /^http:\/\/127\.0\.0\.1:\d+$/,
    );
    expect(openedUrl.searchParams.get("state")).toMatch(/^[0-9a-f]{32}$/);
  });

  it("invokes onBrowserOpened with the same auth URL before opening it", async () => {
    mockTokenExchange({ refresh_token: "rt-123" });
    simulateBrowserRedirect({ code: "auth-code-abc" });

    const onBrowserOpened = vi.fn();
    await runGmailOAuthFlow("client-id", "client-secret", onBrowserOpened);

    expect(onBrowserOpened).toHaveBeenCalledTimes(1);
    expect(onBrowserOpened.mock.calls[0][0]).toContain("accounts.google.com");
  });

  it("sends the authorization code and matching redirect_uri to the token endpoint", async () => {
    mockTokenExchange({ refresh_token: "rt-123" });
    simulateBrowserRedirect({ code: "specific-code-999" });

    await runGmailOAuthFlow("client-id", "client-secret");

    const exchangeCall = (global.fetch as any).mock.calls.find((c: any[]) =>
      isGoogleTokenEndpoint(String(c[0])),
    );
    expect(exchangeCall).toBeTruthy();
    const [, init] = exchangeCall;
    const body = init.body as URLSearchParams;
    expect(body.get("code")).toBe("specific-code-999");
    expect(body.get("client_id")).toBe("client-id");
    expect(body.get("client_secret")).toBe("client-secret");
    expect(body.get("grant_type")).toBe("authorization_code");
    expect(body.get("redirect_uri")).toMatch(/^http:\/\/127\.0\.0\.1:\d+$/);
  });

  it("rejects with a GmailOAuthError naming the reason when the user denies consent", async () => {
    simulateBrowserRedirect({ error: "access_denied" });

    let caught: unknown;
    try {
      await runGmailOAuthFlow("client-id", "client-secret");
    } catch (err) {
      caught = err;
    }

    expect(caught).toBeInstanceOf(GmailOAuthError);
    expect((caught as Error).message).toMatch(/denied/i);
  });

  it("rejects when the token endpoint reports an error", async () => {
    mockTokenExchange(
      { error: "invalid_client", error_description: "bad credentials" },
      false,
    );
    simulateBrowserRedirect({ code: "auth-code-abc" });

    await expect(
      runGmailOAuthFlow("bad-client-id", "bad-secret"),
    ).rejects.toThrow(/bad credentials/);
  });

  it("rejects when Google returns no refresh token", async () => {
    // No refresh_token: Google only omits it when a prior authorization for
    // this exact client is still active, even though access_type=offline
    // and prompt=consent are both set -- this must not be treated as success.
    mockTokenExchange({ access_token: "at-123" });
    simulateBrowserRedirect({ code: "auth-code-abc" });

    await expect(
      runGmailOAuthFlow("client-id", "client-secret"),
    ).rejects.toThrow(/did not return a refresh token/);
  });

  it("ignores a request with a code but a missing/mismatched state (CSRF protection) and keeps waiting for the real redirect", async () => {
    // Regression test: without checking `state`, another local process (or
    // a page already open in the browser) that reaches this loopback port
    // first could inject its own code and have it accepted as authoritative.
    mockTokenExchange({ refresh_token: "rt-123" });

    mockedOpen.mockImplementation(async (url: any) => {
      const authUrl = new URL(String(url));
      const redirectUri = authUrl.searchParams.get("redirect_uri")!;
      const realState = authUrl.searchParams.get("state")!;

      // A forged/stray request with a bogus state first -- must be
      // rejected, not accepted as the real redirect.
      await realFetch(
        `${redirectUri}/?code=attacker-injected-code&state=wrong-state`,
      );
      // The real redirect, with the matching state, arrives after.
      await realFetch(`${redirectUri}/?code=auth-code-abc&state=${realState}`);
      return {} as any;
    });

    const token = await runGmailOAuthFlow("client-id", "client-secret");

    expect(token).toBe("rt-123");
    // The forged request's code must never reach the token exchange.
    const exchangeCall = (global.fetch as any).mock.calls.find((c: any[]) =>
      isGoogleTokenEndpoint(String(c[0])),
    );
    const body = exchangeCall[1].body as URLSearchParams;
    expect(body.get("code")).toBe("auth-code-abc");
  });

  it("ignores stray requests to the loopback server (e.g. a browser favicon fetch) and keeps waiting for the real redirect", async () => {
    mockTokenExchange({ refresh_token: "rt-123" });

    mockedOpen.mockImplementation(async (url: any) => {
      const authUrl = new URL(String(url));
      const redirectUri = authUrl.searchParams.get("redirect_uri")!;
      const state = authUrl.searchParams.get("state")!;
      // A stray request first (should be ignored, not resolve/close the
      // server), then the real redirect.
      await realFetch(`${redirectUri}/favicon.ico`);
      await realFetch(`${redirectUri}/?code=auth-code-abc&state=${state}`);
      return {} as any;
    });

    const token = await runGmailOAuthFlow("client-id", "client-secret");
    expect(token).toBe("rt-123");
  });

  it("still completes if launching the browser itself fails, as long as the user opens the printed URL", async () => {
    // Simulates e.g. no display/browser available on the machine running
    // the installer: `open()` rejects, but the flow must not abort -- the
    // user can still manually open the URL passed to `onBrowserOpened`.
    mockTokenExchange({ refresh_token: "rt-123" });

    let redirectUri = "";
    let state = "";
    mockedOpen.mockImplementation(async (url: any) => {
      const authUrl = new URL(String(url));
      redirectUri = authUrl.searchParams.get("redirect_uri")!;
      state = authUrl.searchParams.get("state")!;
      throw new Error("no display available");
    });

    const flowPromise = runGmailOAuthFlow("client-id", "client-secret");
    // Wait for `open()` to have been invoked (and captured the redirect
    // URI) before simulating the user manually completing consent.
    await vi.waitFor(() => expect(redirectUri).not.toBe(""));
    await realFetch(`${redirectUri}/?code=auth-code-abc&state=${state}`);

    await expect(flowPromise).resolves.toBe("rt-123");
  });
});
