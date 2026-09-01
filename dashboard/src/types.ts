export interface DashboardConfig {
  serverUrl: string;
  deviceToken: string;
  memberId: string;
}

export interface WindowStatus {
  window_type: string;
  allocation_units: number;
  used_units: number;
  remaining_units: number;
  window_start: string;
  reset_at: string;
}

export interface MemberStatus {
  member_id: string;
  pool_id: string;
  display_name: string;
  windows: Record<string, WindowStatus>;
}

export interface EffectiveCapacity {
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

export interface MemberSummary {
  id: string;
  pool_id: string;
  user_id: string;
  display_name: string;
}

export interface CapacityRequest {
  id: string;
  pool_id: string;
  requester_member_id: string;
  target_member_id: string;
  window_type: string;
  amount: number;
  type: string;
  status: string;
  created_at: string;
  approved_at: string | null;
  expires_at: string | null;
  message: string | null;
}

export interface CapacityGrant {
  id: string;
  pool_id: string;
  source_member_id: string;
  recipient_member_id: string;
  window_type: string;
  amount: number;
  type: string;
  status: string;
  created_at: string;
  activated_at: string;
  expires_at: string;
  revoked_at: string | null;
}

export interface MemberGrants {
  sent: CapacityGrant[];
  received: CapacityGrant[];
}

export const CONFIG_KEY = "claudeShareDashboardConfig";
export const WINDOW_TYPES = ["five_hour", "weekly"] as const;
