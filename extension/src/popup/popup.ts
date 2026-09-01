import {
  normalizeServerUrl,
  registerDevice,
  serverOriginPattern,
} from "../api.js";
import { STORAGE_KEYS, type ExtensionConfig, type QuotaSnapshot } from "../types.js";

function byId<T extends HTMLElement>(id: string): T {
  const element = document.getElementById(id);
  if (!element) {
    throw new Error(`Missing element #${id}`);
  }
  return element as T;
}

function setStatus(element: HTMLElement, message: string, kind: "ok" | "error" | "" = ""): void {
  element.textContent = message;
  element.className = kind ? `status ${kind}` : "status";
}

async function loadExistingConfig(): Promise<void> {
  const response = await chrome.runtime.sendMessage({ type: "get-config" });
  const config = response?.config as ExtensionConfig | null | undefined;
  if (!config) {
    return;
  }
  byId<HTMLInputElement>("server-url").value = config.serverUrl;
  byId<HTMLInputElement>("device-token").value = config.deviceToken;
  byId<HTMLInputElement>("member-id").value = config.memberId;
}

function renderSnapshot(snapshot: QuotaSnapshot): void {
  const output = byId<HTMLPreElement>("status-output");
  if (snapshot.error) {
    output.textContent = `Unavailable: ${snapshot.error}`;
    return;
  }
  output.textContent =
    `Member: ${snapshot.displayName} (${snapshot.memberId})\n` +
    `Pool: ${snapshot.poolId}\n` +
    `Guaranteed: ${snapshot.guaranteedUnits} units\n` +
    `Used: ${snapshot.usedUnits}\n` +
    `Remaining: ${snapshot.remainingUnits}\n` +
    `Indicator: ${snapshot.indicatorText}\n` +
    `Exhausted: ${snapshot.isExhausted ? "yes" : "no"}`;
}

async function ensureHostPermission(serverUrl: string): Promise<void> {
  const pattern = serverOriginPattern(serverUrl);
  const hasPermission = await chrome.permissions.contains({ origins: [pattern] });
  if (hasPermission) {
    return;
  }
  const granted = await chrome.permissions.request({ origins: [pattern] });
  if (!granted) {
    throw new Error(`Host permission required for ${pattern}`);
  }
}

async function saveConfiguration(): Promise<void> {
  const status = byId<HTMLParagraphElement>("save-status");
  try {
    const config: ExtensionConfig = {
      serverUrl: normalizeServerUrl(byId<HTMLInputElement>("server-url").value),
      deviceToken: byId<HTMLInputElement>("device-token").value.trim(),
      memberId: byId<HTMLInputElement>("member-id").value.trim(),
    };
    if (!config.deviceToken || !config.memberId) {
      throw new Error("Device token and member ID are required.");
    }

    await ensureHostPermission(config.serverUrl);
    await chrome.storage.local.set({ [STORAGE_KEYS.config]: config });
    setStatus(status, "Saved. Refreshing quota…", "ok");

    const refreshed = await chrome.runtime.sendMessage({ type: "refresh-quota" });
    if (refreshed?.snapshot) {
      renderSnapshot(refreshed.snapshot as QuotaSnapshot);
    }
  } catch (error) {
    setStatus(status, error instanceof Error ? error.message : String(error), "error");
  }
}

async function registerNewDevice(): Promise<void> {
  const status = byId<HTMLParagraphElement>("register-status");
  try {
    const serverUrl = normalizeServerUrl(byId<HTMLInputElement>("server-url").value);
    const userId = byId<HTMLInputElement>("user-id").value.trim();
    const deviceName = byId<HTMLInputElement>("device-name").value.trim() || "Browser extension";
    if (!userId) {
      throw new Error("User ID is required to register a device.");
    }

    await ensureHostPermission(serverUrl);
    const result = await registerDevice(serverUrl, userId, deviceName);
    byId<HTMLInputElement>("device-token").value = result.token;
    setStatus(status, `Registered device ${result.device.id}. Token filled in above — save configuration.`, "ok");
  } catch (error) {
    setStatus(status, error instanceof Error ? error.message : String(error), "error");
  }
}

async function refreshStatus(): Promise<void> {
  const response = await chrome.runtime.sendMessage({ type: "refresh-quota" });
  if (response?.snapshot) {
    renderSnapshot(response.snapshot as QuotaSnapshot);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  void loadExistingConfig().then(refreshStatus);
  byId<HTMLButtonElement>("save-config").addEventListener("click", () => void saveConfiguration());
  byId<HTMLButtonElement>("register-device").addEventListener("click", () => void registerNewDevice());
  byId<HTMLButtonElement>("refresh-status").addEventListener("click", () => void refreshStatus());
});
