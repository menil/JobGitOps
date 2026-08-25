import { expect } from "vitest";
import { parse } from "graphql";

/**
 * Validates every GraphQL request body recorded on the current global.fetch
 * mock. Regression guard against interpolating invalid documents into queries
 * (e.g. // comments): mocked fetch never validates syntax server-side, so a
 * malformed query would otherwise pass CI and fail on real deployments.
 */
export function validateGraphqlRequests(): void {
  const calls: Array<[unknown, { body?: unknown } | undefined]> | undefined = (
    global.fetch as any
  )?.mock?.calls;
  for (const call of calls ?? []) {
    const rawBody = String(call?.[1]?.body ?? "");
    if (!rawBody) {
      continue;
    }
    let body: { query?: unknown };
    try {
      body = JSON.parse(rawBody);
    } catch {
      continue;
    }
    if (typeof body.query === "string") {
      expect(() => parse(body.query)).not.toThrow();
    }
  }
}
