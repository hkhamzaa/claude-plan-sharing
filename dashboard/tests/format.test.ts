import { describe, expect, it } from "vitest";
import { formatDateTime, formatDuration, formatResetIn, formatUnits, formatWindowLabel } from "../src/format.js";

describe("formatDuration", () => {
  it("formats hours and minutes", () => {
    expect(formatDuration(8140)).toBe("2h 15m");
  });

  it("never goes negative", () => {
    expect(formatDuration(-30)).toBe("0m");
  });
});

describe("formatResetIn", () => {
  it("computes display duration from ISO reset time", () => {
    const now = new Date("2026-01-01T10:00:00Z");
    expect(formatResetIn("2026-01-01T12:30:00Z", now)).toBe("2h 30m");
  });
});

describe("formatDateTime", () => {
  it("returns a non-empty localized string", () => {
    expect(formatDateTime("2026-01-01T12:00:00Z").length).toBeGreaterThan(0);
  });
});

describe("formatWindowLabel", () => {
  it("labels known windows", () => {
    expect(formatWindowLabel("five_hour")).toBe("Five hour");
    expect(formatWindowLabel("weekly")).toBe("Weekly");
  });
});

describe("formatUnits", () => {
  it("formats integers without arithmetic", () => {
    expect(formatUnits(5000)).toBe("5,000");
  });
});
