import type {
  CapacityGrant,
  CapacityRequest,
  DashboardConfig,
  EffectiveCapacity,
  MemberGrants,
  MemberStatus,
  MemberSummary,
} from "./types.js";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

function baseUrl(config: DashboardConfig): string {
  const trimmed = config.serverUrl.trim().replace(/\/+$/, "");
  return trimmed || window.location.origin;
}

async function apiFetch<T>(config: DashboardConfig, path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  headers.set("Authorization", `Bearer ${config.deviceToken}`);
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const response = await fetch(`${baseUrl(config)}${path}`, { ...init, headers });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: string };
      if (body.detail) {
        detail = body.detail;
      }
    } catch {
      // ignore
    }
    throw new ApiError(`${response.status}: ${detail}`, response.status);
  }
  return (await response.json()) as T;
}

export async function registerDevice(
  serverUrl: string,
  userId: string,
  deviceName: string,
): Promise<{ token: string }> {
  const trimmed = serverUrl.trim().replace(/\/+$/, "") || window.location.origin;
  const response = await fetch(`${trimmed}/devices`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ user_id: userId, device_name: deviceName }),
  });
  if (!response.ok) {
    throw new ApiError("Device registration failed", response.status);
  }
  const body = (await response.json()) as { token: string };
  return body;
}

export function getMemberStatus(config: DashboardConfig, memberId: string): Promise<MemberStatus> {
  return apiFetch(config, `/members/${encodeURIComponent(memberId)}/status`);
}

export function getMemberCapacity(
  config: DashboardConfig,
  memberId: string,
  window: string,
): Promise<EffectiveCapacity> {
  return apiFetch(config, `/members/${encodeURIComponent(memberId)}/capacity?window=${window}`);
}

export function listPoolMembers(config: DashboardConfig, poolId: string): Promise<MemberSummary[]> {
  return apiFetch(config, `/pools/${encodeURIComponent(poolId)}/members`);
}

export function listPendingRequests(config: DashboardConfig, memberId: string): Promise<CapacityRequest[]> {
  return apiFetch(config, `/members/${encodeURIComponent(memberId)}/capacity/requests/pending`);
}

export function listActiveGrants(config: DashboardConfig, memberId: string): Promise<MemberGrants> {
  return apiFetch(config, `/members/${encodeURIComponent(memberId)}/capacity/grants`);
}

export function approveRequest(config: DashboardConfig, requestId: string, memberId: string): Promise<CapacityGrant> {
  return apiFetch(config, `/capacity/requests/${encodeURIComponent(requestId)}/approve`, {
    method: "POST",
    body: JSON.stringify({ approving_member_id: memberId }),
  });
}

export function rejectRequest(config: DashboardConfig, requestId: string, memberId: string): Promise<CapacityRequest> {
  return apiFetch(config, `/capacity/requests/${encodeURIComponent(requestId)}/reject`, {
    method: "POST",
    body: JSON.stringify({ rejecting_member_id: memberId }),
  });
}

export function revokeGrant(config: DashboardConfig, grantId: string, memberId: string): Promise<CapacityGrant> {
  return apiFetch(config, `/capacity/grants/${encodeURIComponent(grantId)}/revoke`, {
    method: "POST",
    body: JSON.stringify({ revoking_member_id: memberId }),
  });
}

export interface MemberPoolRow {
  member: MemberSummary;
  status: MemberStatus;
  capacityFiveHour: EffectiveCapacity;
  capacityWeekly: EffectiveCapacity;
}

export interface MemberPoolOverviewResponse {
  member: MemberSummary;
  status: MemberStatus;
  capacity: Record<string, EffectiveCapacity>;
}

export interface PoolOverviewResponse {
  pool_id: string;
  members: MemberPoolOverviewResponse[];
}

export function mapPoolOverviewRow(row: MemberPoolOverviewResponse): MemberPoolRow {
  const capacityFiveHour = row.capacity.five_hour;
  const capacityWeekly = row.capacity.weekly;
  if (!capacityFiveHour || !capacityWeekly) {
    throw new Error("Pool overview response missing capacity for a window.");
  }
  return {
    member: row.member,
    status: row.status,
    capacityFiveHour,
    capacityWeekly,
  };
}

export async function loadPoolOverview(config: DashboardConfig, poolId: string): Promise<MemberPoolRow[]> {
  const overview = await apiFetch<PoolOverviewResponse>(
    config,
    `/pools/${encodeURIComponent(poolId)}/overview`,
  );
  return overview.members.map(mapPoolOverviewRow);
}
