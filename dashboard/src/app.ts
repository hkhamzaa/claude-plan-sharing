import {
  approveRequest,
  getMemberCapacity,
  getMemberStatus,
  listActiveGrants,
  listPendingRequests,
  loadPoolOverview,
  registerDevice,
  rejectRequest,
  revokeGrant,
  type MemberPoolRow,
} from "./api.js";
import { formatDateTime, formatResetIn, formatUnits, formatWindowLabel } from "./format.js";
import {
  CONFIG_KEY,
  WINDOW_TYPES,
  type CapacityGrant,
  type CapacityRequest,
  type DashboardConfig,
  type EffectiveCapacity,
  type MemberGrants,
  type MemberStatus,
} from "./types.js";

const POLL_MS = 60_000;

let config: DashboardConfig | null = null;
let activeView = "my-status";
let pollTimer: number | null = null;

function $(id: string): HTMLElement {
  const el = document.getElementById(id);
  if (!el) {
    throw new Error(`Missing #${id}`);
  }
  return el;
}

function loadConfig(): DashboardConfig | null {
  const raw = localStorage.getItem(CONFIG_KEY);
  if (!raw) {
    return null;
  }
  try {
    const parsed = JSON.parse(raw) as DashboardConfig;
    if (parsed.deviceToken && parsed.memberId) {
      return parsed;
    }
  } catch {
    // ignore
  }
  return null;
}

function saveConfig(next: DashboardConfig): void {
  localStorage.setItem(CONFIG_KEY, JSON.stringify(next));
  config = next;
}

function showSetup(): void {
  $("setup-panel").classList.remove("hidden");
  $("app-panel").classList.add("hidden");
}

function showApp(): void {
  $("setup-panel").classList.add("hidden");
  $("app-panel").classList.remove("hidden");
}

function setStatus(message: string, isError = false): void {
  const el = $("global-status");
  el.textContent = message;
  el.className = isError ? "status error" : "status";
}

function renderWindowBlock(
  windowType: string,
  status: MemberStatus,
  capacity: EffectiveCapacity,
): string {
  const window = status.windows[windowType];
  if (!window) {
    return `<p>No ${formatWindowLabel(windowType)} window data.</p>`;
  }
  return `
    <section class="card">
      <h3>${formatWindowLabel(windowType)}</h3>
      <dl>
        <dt>Used</dt><dd>${formatUnits(window.used_units)}</dd>
        <dt>Remaining (base window)</dt><dd>${formatUnits(window.remaining_units)}</dd>
        <dt>Guaranteed</dt><dd>${formatUnits(capacity.guaranteed_units)}</dd>
        <dt>Potential</dt><dd>${formatUnits(capacity.potential_units)}</dd>
        <dt>SOLID sent / received</dt><dd>${formatUnits(capacity.solid_sent)} / ${formatUnits(capacity.solid_received)}</dd>
        <dt>SHARED offered / borrow potential</dt><dd>${formatUnits(capacity.shared_offered)} / ${formatUnits(capacity.shared_borrowed_potential)}</dd>
        <dt>Resets in</dt><dd>${formatResetIn(window.reset_at)} (${formatDateTime(window.reset_at)})</dd>
      </dl>
    </section>
  `;
}

async function renderMyStatus(): Promise<void> {
  if (!config) {
    return;
  }
  const status = await getMemberStatus(config, config.memberId);
  const [fiveHour, weekly] = await Promise.all(
    WINDOW_TYPES.map((window) => getMemberCapacity(config!, config!.memberId, window)),
  );
  $("view-my-status").innerHTML = `
    <h2>My status — ${status.display_name}</h2>
    <p class="hint">Pool ${status.pool_id}</p>
    ${renderWindowBlock("five_hour", status, fiveHour)}
    ${renderWindowBlock("weekly", status, weekly)}
  `;
}

function renderPoolTable(rows: MemberPoolRow[]): string {
  const header = `
    <tr>
      <th>Member</th>
      <th>5h used</th>
      <th>5h guaranteed</th>
      <th>5h potential</th>
      <th>5h reset</th>
      <th>Weekly used</th>
      <th>Weekly guaranteed</th>
      <th>Weekly potential</th>
    </tr>
  `;
  const body = rows
    .map(({ member, status, capacityFiveHour, capacityWeekly }) => {
      const five = status.windows.five_hour;
      const week = status.windows.weekly;
      return `
        <tr>
          <td>${member.display_name}${member.id === config?.memberId ? " (you)" : ""}</td>
          <td>${five ? formatUnits(five.used_units) : "—"}</td>
          <td>${formatUnits(capacityFiveHour.guaranteed_units)}</td>
          <td>${formatUnits(capacityFiveHour.potential_units)}</td>
          <td>${five ? formatResetIn(five.reset_at) : "—"}</td>
          <td>${week ? formatUnits(week.used_units) : "—"}</td>
          <td>${formatUnits(capacityWeekly.guaranteed_units)}</td>
          <td>${formatUnits(capacityWeekly.potential_units)}</td>
        </tr>
      `;
    })
    .join("");
  return `<table class="data-table"><thead>${header}</thead><tbody>${body}</tbody></table>`;
}

async function renderPoolOverview(): Promise<void> {
  if (!config) {
    return;
  }
  const myStatus = await getMemberStatus(config, config.memberId);
  const rows = await loadPoolOverview(config, myStatus.pool_id);
  $("view-pool").innerHTML = `
    <h2>Pool overview</h2>
    <p class="hint">One members-list call plus status + capacity per member (fan-out).</p>
    ${renderPoolTable(rows)}
  `;
}

function renderRequestRow(request: CapacityRequest, membersById: Map<string, string>): string {
  const requester = membersById.get(request.requester_member_id) ?? request.requester_member_id;
  return `
    <article class="card" data-request-id="${request.id}">
      <h3>${request.type.toUpperCase()} — ${formatUnits(request.amount)} units (${formatWindowLabel(request.window_type)})</h3>
      <p>From <strong>${requester}</strong>${request.message ? `: ${request.message}` : ""}</p>
      <p class="hint">Requested ${formatDateTime(request.created_at)}</p>
      <div class="actions">
        <button type="button" data-action="approve" data-id="${request.id}">Approve</button>
        <button type="button" data-action="reject" data-id="${request.id}" class="secondary">Reject</button>
      </div>
    </article>
  `;
}

async function renderPendingRequests(): Promise<void> {
  if (!config) {
    return;
  }
  const requests = await listPendingRequests(config, config.memberId);
  const myStatus = await getMemberStatus(config, config.memberId);
  const members = await loadPoolOverview(config, myStatus.pool_id);
  const names = new Map(members.map((row) => [row.member.id, row.member.display_name]));

  $("view-pending").innerHTML = `
    <h2>Pending requests (need my approval)</h2>
    ${
      requests.length === 0
        ? "<p>No pending requests.</p>"
        : requests.map((r) => renderRequestRow(r, names)).join("")
    }
  `;
}

function renderGrantSection(title: string, grants: CapacityGrant[], canRevoke: boolean, membersById: Map<string, string>): string {
  if (grants.length === 0) {
    return `<p>No ${title.toLowerCase()}.</p>`;
  }
  return grants
    .map((grant) => {
      const otherId = canRevoke ? grant.recipient_member_id : grant.source_member_id;
      const other = membersById.get(otherId) ?? otherId;
      return `
        <article class="card">
          <h3>${grant.type.toUpperCase()} — ${formatUnits(grant.amount)} (${formatWindowLabel(grant.window_type)})</h3>
          <p>${canRevoke ? "To" : "From"} <strong>${other}</strong></p>
          <p class="hint">Active since ${formatDateTime(grant.activated_at)}, expires ${formatDateTime(grant.expires_at)}</p>
          ${
            canRevoke
              ? `<button type="button" data-action="revoke" data-id="${grant.id}">Revoke</button>`
              : ""
          }
        </article>
      `;
    })
    .join("");
}

async function renderGrants(): Promise<void> {
  if (!config) {
    return;
  }
  const grants: MemberGrants = await listActiveGrants(config, config.memberId);
  const myStatus = await getMemberStatus(config, config.memberId);
  const members = await loadPoolOverview(config, myStatus.pool_id);
  const names = new Map(members.map((row) => [row.member.id, row.member.display_name]));

  $("view-grants").innerHTML = `
    <h2>My active grants</h2>
    <h3>Sent (I can revoke)</h3>
    ${renderGrantSection("sent grants", grants.sent, true, names)}
    <h3>Received</h3>
    ${renderGrantSection("received grants", grants.received, false, names)}
  `;
}

async function refreshActiveView(): Promise<void> {
  if (!config) {
    return;
  }
  try {
    if (activeView === "my-status") {
      await renderMyStatus();
    } else if (activeView === "pool") {
      await renderPoolOverview();
    } else if (activeView === "pending") {
      await renderPendingRequests();
    } else if (activeView === "grants") {
      await renderGrants();
    }
    setStatus(`Updated ${new Date().toLocaleTimeString()}`);
  } catch (error) {
    setStatus(error instanceof Error ? error.message : String(error), true);
  }
}

function setActiveView(view: string): void {
  activeView = view;
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-view]")) {
    button.classList.toggle("active", button.dataset.view === view);
  }
  for (const panel of document.querySelectorAll<HTMLElement>(".view")) {
    panel.classList.toggle("hidden", panel.id !== `view-${view}`);
  }
  void refreshActiveView();
}

function bindActions(): void {
  document.body.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLButtonElement) || !config) {
      return;
    }

    const action = target.dataset.action;
    const id = target.dataset.id;
    if (!action || !id) {
      return;
    }

    void (async () => {
      try {
        if (action === "approve") {
          await approveRequest(config!, id, config!.memberId);
        } else if (action === "reject") {
          await rejectRequest(config!, id, config!.memberId);
        } else if (action === "revoke") {
          await revokeGrant(config!, id, config!.memberId);
        } else {
          return;
        }
        await refreshActiveView();
      } catch (error) {
        setStatus(error instanceof Error ? error.message : String(error), true);
      }
    })();
  });
}

function startPolling(): void {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer);
  }
  pollTimer = window.setInterval(() => void refreshActiveView(), POLL_MS);
}

function bindSetup(): void {
  $("save-config").addEventListener("click", () => {
    const next: DashboardConfig = {
      serverUrl: ($("server-url") as HTMLInputElement).value.trim(),
      deviceToken: ($("device-token") as HTMLInputElement).value.trim(),
      memberId: ($("member-id") as HTMLInputElement).value.trim(),
    };
    if (!next.deviceToken || !next.memberId) {
      setStatus("Device token and member ID are required.", true);
      return;
    }
    saveConfig(next);
    showApp();
    setActiveView("my-status");
    startPolling();
  });

  $("register-device").addEventListener("click", () => {
    void (async () => {
      try {
        const serverUrl = ($("server-url") as HTMLInputElement).value.trim();
        const userId = ($("user-id") as HTMLInputElement).value.trim();
        const deviceName = ($("device-name") as HTMLInputElement).value.trim() || "Dashboard";
        if (!userId) {
          setStatus("User ID is required to register a device.", true);
          return;
        }
        const result = await registerDevice(serverUrl, userId, deviceName);
        ($("device-token") as HTMLInputElement).value = result.token;
        setStatus("Device registered — token filled in. Save configuration to continue.");
      } catch (error) {
        setStatus(error instanceof Error ? error.message : String(error), true);
      }
    })();
  });

  $("logout").addEventListener("click", () => {
    localStorage.removeItem(CONFIG_KEY);
    config = null;
    if (pollTimer !== null) {
      window.clearInterval(pollTimer);
    }
    showSetup();
  });
}

function bindNav(): void {
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-view]")) {
    button.addEventListener("click", () => {
      if (button.dataset.view) {
        setActiveView(button.dataset.view);
      }
    });
  }
  $("refresh-now").addEventListener("click", () => void refreshActiveView());
}

function init(): void {
  bindSetup();
  bindNav();
  bindActions();
  config = loadConfig();
  if (config) {
    showApp();
    setActiveView("my-status");
    startPolling();
  } else {
    showSetup();
  }
}

init();
