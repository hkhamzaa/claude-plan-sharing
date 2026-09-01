import {
  POLL_ALARM_NAME,
  POLL_INTERVAL_MINUTES,
  STORAGE_KEYS,
  type ExtensionConfig,
  type QuotaSnapshot,
} from "./types.js";
import { errorSnapshot, fetchQuotaSnapshot } from "./api.js";

export async function loadConfig(): Promise<ExtensionConfig | null> {
  const result = await chrome.storage.local.get(STORAGE_KEYS.config);
  const config = result[STORAGE_KEYS.config] as ExtensionConfig | undefined;
  if (!config?.serverUrl || !config.deviceToken || !config.memberId) {
    return null;
  }
  return config;
}

export async function saveSnapshot(snapshot: QuotaSnapshot): Promise<void> {
  await chrome.storage.local.set({ [STORAGE_KEYS.lastSnapshot]: snapshot });
}

export async function loadSnapshot(): Promise<QuotaSnapshot | null> {
  const result = await chrome.storage.local.get(STORAGE_KEYS.lastSnapshot);
  return (result[STORAGE_KEYS.lastSnapshot] as QuotaSnapshot | undefined) ?? null;
}

export async function pollQuota(): Promise<QuotaSnapshot> {
  const config = await loadConfig();
  if (!config) {
    const snapshot = errorSnapshot("Extension not configured.");
    await saveSnapshot(snapshot);
    return snapshot;
  }

  try {
    const snapshot = await fetchQuotaSnapshot(config);
    await saveSnapshot(snapshot);
    await broadcastSnapshot(snapshot);
    return snapshot;
  } catch (error) {
    const snapshot = errorSnapshot(error instanceof Error ? error.message : String(error));
    await saveSnapshot(snapshot);
    await broadcastSnapshot(snapshot);
    return snapshot;
  }
}

async function broadcastSnapshot(snapshot: QuotaSnapshot): Promise<void> {
  const tabs = await chrome.tabs.query({ url: ["https://claude.ai/*", "https://*.claude.ai/*"] });
  for (const tab of tabs) {
    if (tab.id !== undefined) {
      chrome.tabs.sendMessage(tab.id, { type: "quota-update", snapshot }).catch(() => {
        // content script may not be ready; fail open
      });
    }
  }
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(POLL_ALARM_NAME, { periodInMinutes: POLL_INTERVAL_MINUTES });
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === POLL_ALARM_NAME) {
    void pollQuota();
  }
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.type === "get-quota") {
    void (async () => {
      const cached = await loadSnapshot();
      if (cached) {
        sendResponse({ snapshot: cached });
        return;
      }
      const snapshot = await pollQuota();
      sendResponse({ snapshot });
    })();
    return true;
  }

  if (message?.type === "refresh-quota") {
    void pollQuota().then((snapshot) => sendResponse({ snapshot }));
    return true;
  }

  if (message?.type === "get-config") {
    void loadConfig().then((config) => sendResponse({ config }));
    return true;
  }

  return false;
});

void pollQuota();
