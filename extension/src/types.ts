/** Shared types for extension storage and quota polling. */

export interface ExtensionConfig {
  serverUrl: string;
  deviceToken: string;
  memberId: string;
}

export interface WindowStatusResponse {
  window_type: string;
  allocation_units: number;
  used_units: number;
  remaining_units: number;
  window_start: string;
  reset_at: string;
}

export interface MemberStatusResponse {
  member_id: string;
  pool_id: string;
  display_name: string;
  windows: Record<string, WindowStatusResponse>;
}

export interface EffectiveCapacityResponse {
  member_id: string;
  window_type: string;
  base_allocation_units: number;
  solid_sent: number;
  solid_received: number;
  guaranteed_units: number;
  shared_offered: number;
  shared_borrowed_potential: number;
  potential_units: number;
}

export interface QuotaSnapshot {
  memberId: string;
  displayName: string;
  poolId: string;
  guaranteedUnits: number;
  usedUnits: number;
  remainingUnits: number;
  resetAt: string;
  isExhausted: boolean;
  isLow: boolean;
  indicatorText: string;
  warningHeadline: string | null;
  warningBody: string | null;
  fetchedAt: string;
  error: string | null;
}

export interface DeviceRegistrationResponse {
  device: {
    id: string;
    user_id: string;
    device_name: string;
    created_at: string;
  };
  token: string;
}

export const STORAGE_KEYS = {
  config: "claudeShareConfig",
  lastSnapshot: "claudeShareLastSnapshot",
} as const;

export const POLL_ALARM_NAME = "claudeSharePoll";
export const POLL_INTERVAL_MINUTES = 1;
