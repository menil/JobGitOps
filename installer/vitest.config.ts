import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    globals: true,
    environment: "node",
    pool: "forks",
    coverage: {
      provider: "v8",
      reporter: ["text", "json", "html"],
      exclude: [
        "src/index.ts",
        "src/prompts.ts",
      ],
      thresholds: {
        lines: 90,
        functions: 90,
        branches: 50,
        statements: 90,
      },
    },
  },
});
