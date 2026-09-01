import type {
  DeviceRegistrationResponse,
  EffectiveCapacityResponse,
  ExtensionConfig,
  MemberStatusResponse,
  QuotaSnapshot,
} from "./types.js";
import { FIVE_HOUR_WINDOW, computeQuotaDecision } from "./quota.js";

export function normalizeServerUrl(raw: string): string {
  const trimmed = raw.trim().replace(/\/+$/, "");
  if (!trimmed) {
    throw new Error("Server URL is required.");
  }
  const parsed = new URL(trimmed);
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new Error("Server URL must use http or https.");
  }
  return `${parsed.protocol}//${parsed.host}`;
}

export function serverOriginPattern(serverUrl: string): string {
  const normalized = normalizeServerUrl(serverUrl);
  return `${normalized}/*`;
}

async function apiFetch<T>(
  serverUrl: string,
  path: string,
  init: RequestInit & { token?: string | null } = {},
): Promise<T> {
  const { token, ...requestInit } = init;
  const headers = new Headers(requestInit.headers);
  headers.set("Accept", "application/json");
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  if (requestInit.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${normalizeServerUrl(serverUrl)}${path}`, {
    ...requestInit,
    headers,
  });

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) {
        detail = body.detail;
      }
    } catch {
      // ignore parse errors
    }
    throw new Error(`${response.status}: ${detail}`);
  }

  return (await response.json()) as T;
}

export async function registerDevice(
  serverUrl: string,
  userId: string,
  deviceName: string,
): Promise<DeviceRegistrationResponse> {
  return apiFetch<DeviceRegistrationResponse>(serverUrl, "/devices", {
    method: "POST",
    body: JSON.stringify({ user_id: userId, device_name: deviceName }),
  });
}

export async function fetchQuotaSnapshot(
  config: ExtensionConfig,
  now: Date = new Date(),
): Promise<QuotaSnapshot> {
  const status = await apiFetch<MemberStatusResponse>(
    config.serverUrl,
    `/members/${encodeURIComponent(config.memberId)}/status`,
    { token: config.deviceToken },
  );
  const capacity = await apiFetch<EffectiveCapacityResponse>(
    config.serverUrl,
    `/members/${encodeURIComponent(config.memberId)}/capacity?window=${FIVE_HOUR_WINDOW}`,
    { token: config.deviceToken },
  );

  const window = status.windows[FIVE_HOUR_WINDOW];
  if (!window) {
    throw new Error("Server response missing five_hour window status.");
  }

  const resetAt = new Date(window.reset_at);
  const decision = computeQuotaDecision({
    guaranteedUnits: capacity.guaranteed_units,
    usedUnits: window.used_units,
    resetAt,
    now,
  });

  return {
    memberId: status.member_id,
    displayName: status.display_name,
    poolId: status.pool_id,
    guaranteedUnits: capacity.guaranteed_units,
    usedUnits: window.used_units,
    remainingUnits: decision.remainingUnits,
    resetAt: window.reset_at,
    isExhausted: decision.isExhausted,
    isLow: decision.isLow,
    indicatorText: decision.indicatorText,
    warningHeadline: decision.warningHeadline,
    warningBody: decision.warningBody,
    fetchedAt: now.toISOString(),
    error: null,
  };
}

export function errorSnapshot(message: string, now: Date = new Date()): QuotaSnapshot {
  return {
    memberId: "",
    displayName: "",
    poolId: "",
    guaranteedUnits: 0,
    usedUnits: 0,
    remainingUnits: 0,
    resetAt: now.toISOString(),
    isExhausted: false,
    isLow: false,
    indicatorText: "Claude Share: unavailable",
    warningHeadline: null,
    warningBody: null,
    fetchedAt: now.toISOString(),
    error: message,
  };
}
