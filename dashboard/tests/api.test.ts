import { describe, expect, it } from "vitest";
import { mapPoolOverviewRow } from "../src/api.js";
import type { MemberPoolOverviewResponse } from "../src/api.js";

describe("mapPoolOverviewRow", () => {
  const sampleRow: MemberPoolOverviewResponse = {
    member: {
      id: "member-1",
      pool_id: "pool-1",
      user_id: "user-1",
      display_name: "Alice",
    },
    status: {
      member_id: "member-1",
      pool_id: "pool-1",
      display_name: "Alice",
      windows: {
        five_hour: {
          window_type: "five_hour",
          allocation_units: 5000,
          used_units: 100,
          remaining_units: 4900,
          window_start: "2026-01-01T00:00:00Z",
          reset_at: "2026-01-01T05:00:00Z",
        },
        weekly: {
          window_type: "weekly",
          allocation_units: 5000,
          used_units: 200,
          remaining_units: 4800,
          window_start: "2026-01-01T00:00:00Z",
          reset_at: "2026-01-08T00:00:00Z",
        },
      },
    },
    capacity: {
      five_hour: {
        member_id: "member-1",
        window_type: "five_hour",
        base_allocation_units: 5000,
        solid_sent: 0,
        solid_received: 0,
        guaranteed_units: 5000,
        shared_offered: 0,
        shared_borrowed_potential: 0,
        potential_units: 5000,
      },
      weekly: {
        member_id: "member-1",
        window_type: "weekly",
        base_allocation_units: 5000,
        solid_sent: 0,
        solid_received: 0,
        guaranteed_units: 5000,
        shared_offered: 0,
        shared_borrowed_potential: 0,
        potential_units: 5000,
      },
    },
  };

  it("maps batch overview rows into the pool table shape", () => {
    const mapped = mapPoolOverviewRow(sampleRow);
    expect(mapped.member.display_name).toBe("Alice");
    expect(mapped.status.windows.five_hour?.used_units).toBe(100);
    expect(mapped.capacityFiveHour.guaranteed_units).toBe(5000);
    expect(mapped.capacityWeekly.potential_units).toBe(5000);
  });

  it("rejects overview rows missing a window capacity", () => {
    expect(() =>
      mapPoolOverviewRow({
        ...sampleRow,
        capacity: { five_hour: sampleRow.capacity.five_hour },
      }),
    ).toThrow(/missing capacity/i);
  });
});
