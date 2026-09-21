import open from "open";
import ora from "ora";
import pc from "picocolors";

import { DEFAULT_GMAIL_LABEL } from "./constants.js";
import {
  promptGmailSetup,
  promptHasGmailCredentials,
  promptGmailClientId,
  promptGmailClientSecret,
  promptGmailLabel,
} from "./prompts.js";
import { runGmailOAuthFlow } from "./gmailOAuth.js";

const GOOGLE_CLOUD_CREDENTIALS_URL =
  "https://console.cloud.google.com/apis/credentials";

export interface GmailSetupResult {
  wantGmail: boolean;
  gmailClientId: string;
  gmailClientSecret: string;
  gmailRefreshToken: string;
  gmailLabel: string;
}

/**
 * Resolves the full optional Gmail Sync setup for one installer run: whether
 * to enable it, the OAuth client credentials, the label, and (unless a
 * pre-minted token was supplied) the refresh token from a live browser
 * consent flow. Mirrors the CLI-flag -> env-var -> interactive-prompt
 * fallback order already used for the LLM provider key and optional
 * services in `index.ts`.
 *
 * CLI-flag/prompt/terminal-spinner wiring, like `index.ts` and
 * `prompts.ts` -- deliberately kept out of `gmailOAuth.ts`, which holds only
 * the testable OAuth protocol mechanics, and out of `vitest.config.ts`'s
 * coverage gate for the same reason those two files are.
 *
 * Returns all-empty-string values when Gmail Sync isn't enabled.
 */
export async function resolveGmailSetup(
  options: any,
  interactive: boolean,
): Promise<GmailSetupResult> {
  let wantGmail = Boolean(options.gmail);
  if (!wantGmail && interactive) {
    wantGmail = await promptGmailSetup();
  }

  if (!wantGmail) {
    return {
      wantGmail: false,
      gmailClientId: "",
      gmailClientSecret: "",
      gmailRefreshToken: "",
      gmailLabel: "",
    };
  }

  const clientCredentialsPreSupplied = Boolean(
    (options.gmailClientId || process.env.GMAIL_CLIENT_ID) &&
    (options.gmailClientSecret || process.env.GMAIL_CLIENT_SECRET),
  );
  if (!clientCredentialsPreSupplied && interactive) {
    const hasCredentials = await promptHasGmailCredentials();
    if (!hasCredentials) {
      console.log(pc.cyan("\nOpening the Google Cloud Console for you..."));
      console.log(
        pc.bold("In the tab that just opened, do these three things:"),
      );
      console.log(
        `  1. ${pc.bold("Enable the Gmail API")} (skip if already enabled): ` +
          `search "Gmail API" in the top search bar > click it > Enable.`,
      );
      console.log(
        `  2. ${pc.bold("Add yourself as a test user -- do not skip this")}: ` +
          `left sidebar > "OAuth consent screen" > "Audience" tab > "Test users" > ` +
          `add your own Google account's email > Save. Skipping this step is the ` +
          `most common snag: you'll get all the way through consent and see ` +
          `${pc.bold('"Access blocked: ... has not completed the Google verification process"')} ` +
          `instead of the permission screen -- if that happens, this is the step ` +
          `you missed.`,
      );
      console.log(
        `  3. ${pc.bold("Create the OAuth client")}: "OAuth consent screen" > ` +
          `"Overview" tab > "Create OAuth Client" > Application type: ` +
          `${pc.bold("Desktop app")} (not Web application -- Desktop app is ` +
          `required for the redirect to work) > give it any name > Create. A ` +
          `popup shows your ${pc.bold("Client ID")} and ${pc.bold("Client Secret")} ` +
          `-- copy both, you'll paste them in next.`,
      );
      console.log(
        pc.dim(
          "\n(Full walkthrough with screenshots-equivalent detail: DEVELOPMENT.md's " +
            '"Gmail Sync Setup" section.)\n',
        ),
      );
      await open(GOOGLE_CLOUD_CREDENTIALS_URL).catch(() => {});
    }
  }

  const gmailClientId = await resolveRequiredGmailValue(
    options.gmailClientId || process.env.GMAIL_CLIENT_ID || "",
    interactive,
    promptGmailClientId,
    "--gmail-client-id/GMAIL_CLIENT_ID",
  );
  const gmailClientSecret = await resolveRequiredGmailValue(
    options.gmailClientSecret || process.env.GMAIL_CLIENT_SECRET || "",
    interactive,
    promptGmailClientSecret,
    "--gmail-client-secret/GMAIL_CLIENT_SECRET",
  );

  let gmailLabel = options.gmailLabel || process.env.GMAIL_LABEL || "";
  if (!gmailLabel && interactive) {
    gmailLabel = await promptGmailLabel();
  } else if (!gmailLabel) {
    gmailLabel = DEFAULT_GMAIL_LABEL;
  }

  let gmailRefreshToken =
    options.gmailRefreshToken || process.env.GMAIL_REFRESH_TOKEN || "";
  if (!gmailRefreshToken) {
    if (options.dryRun) {
      console.log(
        pc.cyan(
          "\n[Dry Run] Would open your browser for Gmail OAuth consent now.",
        ),
      );
      gmailRefreshToken = "dry-run-placeholder-refresh-token";
    } else if (interactive) {
      console.log(
        pc.cyan("\n🔑 Opening your browser to authorize Gmail Sync..."),
      );
      const gmailSpinner = ora(
        "Waiting for Google authorization in your browser...",
      ).start();
      try {
        gmailRefreshToken = await runGmailOAuthFlow(
          gmailClientId,
          gmailClientSecret,
          (url) => {
            gmailSpinner.text = `Waiting for Google authorization... (if your browser didn't open, visit: ${url})`;
          },
        );
        gmailSpinner.succeed("Gmail authorized successfully.");
      } catch (err: any) {
        gmailSpinner.fail();
        throw err;
      }
    } else {
      throw new Error(
        "Gmail Sync enabled for a non-interactive install but no " +
          "--gmail-refresh-token/GMAIL_REFRESH_TOKEN provided. Mint " +
          "one first (see DEVELOPMENT.md's headless walkthrough) or " +
          "run the installer interactively.",
      );
    }
  }

  return {
    wantGmail: true,
    gmailClientId,
    gmailClientSecret,
    gmailRefreshToken,
    gmailLabel,
  };
}

async function resolveRequiredGmailValue(
  cliOrEnvValue: string,
  interactive: boolean,
  promptFn: () => Promise<string>,
  flagDescription: string,
): Promise<string> {
  if (cliOrEnvValue) return cliOrEnvValue;
  if (interactive) return promptFn();
  throw new Error(`Gmail Sync enabled but no ${flagDescription} provided.`);
}
