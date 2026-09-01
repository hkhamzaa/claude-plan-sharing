/**
 * Quota formatting and state logic mirrored from the Claude Code hook
 * (`integrations/claude_code/hook.py`) for consistent messaging.
 */

export const TOTAL_ALLOCATION_BPS = 10_000;
export const PLACEHOLDER_PROMPT_COST_UNITS = 1;
export const WARNING_THRESHOLD_FRACTION = 0.2;
export const BRAND_NAME = "Claude Share";
export const FIVE_HOUR_WINDOW = "five_hour";

export function formatDuration(totalSeconds: number): string {
  const safeSeconds = Math.max(Math.floor(totalSeconds), 0);
  const hours = Math.floor(safeSeconds / 3600);
  const minutes = Math.floor((safeSeconds % 3600) / 60);
  if (hours && minutes) {
    return `${hours}h ${minutes}m`;
  }
  if (hours) {
    return `${hours}h`;
  }
  return `${minutes}m`;
}

export function formatRemainingPercent(remainingUnits: number, guaranteedUnits: number): number {
  if (guaranteedUnits <= 0) {
    return 0;
  }
  return Math.max(0, Math.min(100, Math.round((remainingUnits / guaranteedUnits) * 100)));
}

/** Compact indicator shown persistently on claude.ai, e.g. "62% remaining, resets in 2h 14m". */
export function formatIndicatorText(
  remainingUnits: number,
  guaranteedUnits: number,
  resetAt: Date,
  now: Date,
): string {
  const pct = formatRemainingPercent(remainingUnits, guaranteedUnits);
  const resetIn = formatDuration((resetAt.getTime() - now.getTime()) / 1000);
  return `${pct}% remaining, resets in ${resetIn}`;
}

export function renderQuotaMessage(
  headline: string,
  guaranteedUnits: number,
  usedUnits: number,
  resetAt: Date,
  now: Date,
): string {
  const usedPct = (usedUnits / TOTAL_ALLOCATION_BPS) * 100;
  const sharePct = (guaranteedUnits / TOTAL_ALLOCATION_BPS) * 100;
  return (
    `${BRAND_NAME}\n` +
    `${headline}\n` +
    `Used: ${usedPct.toFixed(1)}% / ${sharePct.toFixed(0)}%\n` +
    `Reset: ${formatDuration((resetAt.getTime() - now.getTime()) / 1000)}`
  );
}

export interface QuotaInputs {
  guaranteedUnits: number;
  usedUnits: number;
  resetAt: Date;
  now: Date;
}

export interface QuotaDecision {
  remainingUnits: number;
  isExhausted: boolean;
  isLow: boolean;
  indicatorText: string;
  warningHeadline: string | null;
  warningBody: string | null;
}

export function computeQuotaDecision(input: QuotaInputs): QuotaDecision {
  const remainingUnits = Math.max(input.guaranteedUnits - input.usedUnits, 0);
  const indicatorText = formatIndicatorText(
    remainingUnits,
    input.guaranteedUnits,
    input.resetAt,
    input.now,
  );

  if (remainingUnits < PLACEHOLDER_PROMPT_COST_UNITS) {
    const body = renderQuotaMessage(
      "Allocation exhausted.",
      input.guaranteedUnits,
      input.usedUnits,
      input.resetAt,
      input.now,
    );
    return {
      remainingUnits,
      isExhausted: true,
      isLow: true,
      indicatorText,
      warningHeadline: "Allocation exhausted.",
      warningBody: body,
    };
  }

  const isLow =
    input.guaranteedUnits > 0 &&
    remainingUnits / input.guaranteedUnits < WARNING_THRESHOLD_FRACTION;

  if (isLow) {
    const body = renderQuotaMessage(
      "Quota running low.",
      input.guaranteedUnits,
      input.usedUnits,
      input.resetAt,
      input.now,
    );
    return {
      remainingUnits,
      isExhausted: false,
      isLow: true,
      indicatorText,
      warningHeadline: "Quota running low.",
      warningBody: body,
    };
  }

  return {
    remainingUnits,
    isExhausted: false,
    isLow: false,
    indicatorText,
    warningHeadline: null,
    warningBody: null,
  };
}
