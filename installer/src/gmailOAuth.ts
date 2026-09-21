import crypto from "crypto";
import http from "http";
import type { AddressInfo } from "net";
import open from "open";

/** Read-only scope, mirroring the engine's `gmail_client.py` requirement. */
const GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly";
const GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth";
const GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token";

/** How long to wait for the user to complete the browser consent flow. */
const CONSENT_TIMEOUT_MS = 5 * 60 * 1000;

export class GmailOAuthError extends Error {}

/**
 * Starts a short-lived HTTP server on a loopback address to catch Google's
 * OAuth redirect, the same pattern CLIs like `gh auth login --web` use:
 * Desktop-app OAuth clients get automatic support for any `localhost` port
 * (RFC 8252), so nothing needs to be pre-registered beyond the client
 * itself. Resolves with the authorization `code` once Google redirects back
 * with a matching `state`, or rejects on `?error=...`, a mismatched/missing
 * `state`, a timeout, or a server error.
 *
 * `state` guards against another local process (or a malicious page already
 * open in the user's browser) reaching this loopback port first and
 * injecting its own `code`/`error` before the real Google redirect lands
 * (RFC 8252 §8.3) -- without it, that request would otherwise be accepted
 * as authoritative.
 */
function waitForAuthorizationCode(
  server: http.Server,
  port: number,
  expectedState: string,
): Promise<string> {
  return new Promise((resolve, reject) => {
    let settled = false;

    const cleanup = () => {
      clearTimeout(timeout);
      server.close();
    };
    const settleResolve = (code: string) => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve(code);
    };
    const settleReject = (err: Error) => {
      if (settled) return;
      settled = true;
      cleanup();
      reject(err);
    };

    const timeout = setTimeout(() => {
      settleReject(
        new GmailOAuthError(
          "Timed out waiting for Google authorization in the browser. Run the setup again when you're ready.",
        ),
      );
    }, CONSENT_TIMEOUT_MS);

    server.on("request", (req, res) => {
      const url = new URL(req.url ?? "/", `http://127.0.0.1:${port}`);
      const code = url.searchParams.get("code");
      const error = url.searchParams.get("error");
      const state = url.searchParams.get("state");

      if (!code && !error) {
        // Stray request (e.g. favicon.ico) -- ignore, keep waiting.
        res.writeHead(404).end();
        return;
      }

      if (state !== expectedState) {
        // Doesn't match this run's own request -- never trust it, but keep
        // listening rather than aborting: an attacker's stray probe must
        // not derail the user's own still-pending, legitimate redirect.
        res.writeHead(403).end();
        return;
      }

      res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      res.end(
        error
          ? "<html><body><h3>Gmail authorization was not completed.</h3>You can close this tab and return to your terminal.</body></html>"
          : "<html><body><h3>Gmail authorization complete.</h3>You can close this tab and return to your terminal.</body></html>",
      );

      if (error) {
        settleReject(
          new GmailOAuthError(`Google authorization was denied: ${error}`),
        );
      } else {
        settleResolve(code as string);
      }
    });

    server.on("error", settleReject);
  });
}

interface TokenResponse {
  refresh_token?: string;
  access_token?: string;
  error?: string;
  error_description?: string;
}

async function exchangeCodeForRefreshToken(
  clientId: string,
  clientSecret: string,
  code: string,
  redirectUri: string,
): Promise<string> {
  const response = await fetch(GOOGLE_TOKEN_URL, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      client_id: clientId,
      client_secret: clientSecret,
      code,
      grant_type: "authorization_code",
      redirect_uri: redirectUri,
    }),
  });

  const data = (await response.json()) as TokenResponse;

  if (!response.ok || data.error) {
    throw new GmailOAuthError(
      `Google rejected the token exchange: ${data.error_description || data.error || response.statusText}. Double-check the Client ID/Secret.`,
    );
  }
  if (!data.refresh_token) {
    throw new GmailOAuthError(
      "Google did not return a refresh token. This usually means a prior " +
        "authorization for this app is still active; revoke it at " +
        "https://myaccount.google.com/permissions and run setup again.",
    );
  }
  return data.refresh_token;
}

/**
 * Runs the full interactive Gmail OAuth consent flow for a user-owned
 * Desktop-app OAuth client (never a JobGitOps-shared client -- see
 * DEVELOPMENT.md's "Gmail Sync Setup" section for why) and returns the
 * resulting refresh token.
 *
 * Opens the user's default browser to Google's consent screen and blocks
 * until either the loopback server receives the redirect or
 * `CONSENT_TIMEOUT_MS` elapses.
 */
export async function runGmailOAuthFlow(
  clientId: string,
  clientSecret: string,
  onBrowserOpened?: (url: string) => void,
): Promise<string> {
  const server = http.createServer();
  // A bind failure (e.g. no loopback interface, a sandboxed/locked-down
  // environment) emits 'error' on the server instead of ever invoking the
  // listen callback -- without this rejection path, the outer promise would
  // hang forever rather than surfacing a clear error.
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.removeListener("error", reject);
      resolve();
    });
  });
  const port = (server.address() as AddressInfo).port;
  const redirectUri = `http://127.0.0.1:${port}`;
  const state = crypto.randomBytes(16).toString("hex");

  const authUrl = new URL(GOOGLE_AUTH_URL);
  authUrl.searchParams.set("client_id", clientId);
  authUrl.searchParams.set("redirect_uri", redirectUri);
  authUrl.searchParams.set("response_type", "code");
  authUrl.searchParams.set("scope", GMAIL_SCOPE);
  authUrl.searchParams.set("state", state);
  // access_type=offline + prompt=consent: without both, Google only issues
  // a refresh_token on a mailbox's very first-ever authorization for this
  // client, which would silently break a second run (e.g. rotating the
  // token later).
  authUrl.searchParams.set("access_type", "offline");
  authUrl.searchParams.set("prompt", "consent");

  // Awaited immediately (nothing else awaited in between): the server can
  // reject `codePromise` as soon as the browser redirect lands, which can
  // race ahead of a separately-awaited `open()` call and get flagged as an
  // unhandled rejection in that gap. Launching the browser is a best-effort
  // side effect -- if it fails, `onBrowserOpened`'s URL is still printed for
  // the user to open manually, so a launch failure doesn't abort the flow.
  const codePromise = waitForAuthorizationCode(server, port, state);
  onBrowserOpened?.(authUrl.toString());
  void open(authUrl.toString()).catch(() => {});

  const code = await codePromise;
  return exchangeCodeForRefreshToken(clientId, clientSecret, code, redirectUri);
}
