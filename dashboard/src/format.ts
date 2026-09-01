/** Display-only formatting — no quota/capacity business logic. */

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

export function formatResetIn(resetAtIso: string, now: Date = new Date()): string {
  const resetAt = new Date(resetAtIso);
  return formatDuration((resetAt.getTime() - now.getTime()) / 1000);
}

export function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString();
}

export function formatWindowLabel(windowType: string): string {
  return windowType === "five_hour" ? "Five hour" : "Weekly";
}

/** Display a server-provided integer with no client-side arithmetic. */
export function formatUnits(value: number): string {
  return value.toLocaleString();
}
