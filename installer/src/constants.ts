/** Maintainer-only workflows excluded from user repo installation. */
export const EXCLUDED_WORKFLOWS = [
  "sync-template.yml",
  "ci.yml",
  "build-runner.yml",
  "pr-review.yml",
  "release-on-merge.yml",
];

/** Supported LLM providers across the installer. */
export type LLMProvider = "gemini" | "openrouter" | "claude";

export function getProviderLabel(provider: LLMProvider): string {
  switch (provider) {
    case "gemini":
      return "Gemini";
    case "openrouter":
      return "OpenRouter";
    case "claude":
      return "Claude";
  }
}
