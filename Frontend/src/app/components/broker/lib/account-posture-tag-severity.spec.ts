import { describe, expect, it } from "vitest";

import { accountPostureTagSeverity } from "./account-posture-tag-severity";

describe("accountPostureTagSeverity", () => {
  it.each([
    ["CLEAN", "success"],
    ["ready for action", "success"],
    ["ACTIVE", "success"],
    ["degraded feed", "warn"],
    ["stale evidence", "warn"],
    ["FROZEN", "danger"],
    ["UNSAFE", "danger"],
    ["blocked by policy", "danger"],
    ["NOT_PROVEN", "secondary"],
  ] as const)("maps %s to %s", (posture, expectedSeverity) => {
    expect(accountPostureTagSeverity(posture)).toBe(expectedSeverity);
  });
});
