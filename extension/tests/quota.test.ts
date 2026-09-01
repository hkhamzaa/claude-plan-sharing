import { describe, expect, it } from "vitest";
import {
  FIVE_HOUR_WINDOW,
  computeQuotaDecision,
  formatDuration,
  formatIndicatorText,
  formatRemainingPercent,
  renderQuotaMessage,
} from "../src/quota.js";

describe("formatDuration", () => {
  it("formats hours and minutes", () => {
    expect(formatDuration(2 * 3600 + 14 * 60)).toBe("2h 14m");
  });

  it("formats hours only", () => {
    expect(formatDuration(3 * 3600)).toBe("3h");
  });

  it("formats minutes only", () => {
    expect(formatDuration(45 * 60)).toBe("45m");
  });

  it("never returns negative durations", () => {
    expect(formatDuration(-120)).toBe("0m");
  });
});

describe("formatIndicatorText", () => {
  it("shows remaining percent and reset time", () => {
    const now = new Date("2026-01-01T10:00:00Z");
    const resetAt = new Date("2026-01-01T12:14:00Z");
    expect(formatIndicatorText(3100, 5000, resetAt, now)).toBe("62% remaining, resets in 2h 14m");
  });
});

describe("formatRemainingPercent", () => {
  it("clamps to 0-100", () => {
    expect(formatRemainingPercent(6000, 5000)).toBe(100);
    expect(formatRemainingPercent(0, 5000)).toBe(0);
    expect(formatRemainingPercent(100, 0)).toBe(0);
  });
});

describe("computeQuotaDecision", () => {
  const resetAt = new Date("2026-01-01T12:00:00Z");
  const now = new Date("2026-01-01T10:00:00Z");

  it("marks exhausted when remaining is below placeholder cost", () => {
    const decision = computeQuotaDecision({
      guaranteedUnits: 5000,
      usedUnits: 5000,
      resetAt,
      now,
    });
    expect(decision.isExhausted).toBe(true);
    expect(decision.warningHeadline).toBe("Allocation exhausted.");
  });

  it("marks low when below warning threshold but not exhausted", () => {
    const decision = computeQuotaDecision({
      guaranteedUnits: 5000,
      usedUnits: 4100,
      resetAt,
      now,
    });
    expect(decision.isExhausted).toBe(false);
    expect(decision.isLow).toBe(true);
    expect(decision.warningHeadline).toBe("Quota running low.");
  });

  it("matches hook message formatting", () => {
    const message = renderQuotaMessage("Allocation exhausted.", 2500, 2500, resetAt, now);
    expect(message).toContain("Used: 25.0% / 25%");
    expect(message).toContain("Reset: 2h");
  });
});

describe("window constant", () => {
  it("uses five_hour like the hook", () => {
    expect(FIVE_HOUR_WINDOW).toBe("five_hour");
  });
});
