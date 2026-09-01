import type { QuotaSnapshot } from "./types.js";
import {
  findChatElements,
  installSendInterceptors,
  removeIndicator,
  renderIndicator,
  renderWarningBanner,
} from "./intercept.js";

let latestSnapshot: QuotaSnapshot | null = null;
let allowNextSend = false;
let interceptCleanup: (() => void) | null = null;
let installedSendButton: HTMLElement | null = null;
let installedInputArea: HTMLElement | null = null;
let domObserver: MutationObserver | null = null;

function getInterceptState() {
  return {
    isExhausted: latestSnapshot?.isExhausted ?? false,
    allowNextSend,
  };
}

function armOneShotOverride(): void {
  allowNextSend = true;
  reinstallInterceptors();
}

function consumeOneShotOverride(): void {
  allowNextSend = false;
  reinstallInterceptors();
}

function showBlockedWarning(): void {
  if (!latestSnapshot?.warningHeadline || !latestSnapshot.warningBody) {
    return;
  }
  renderWarningBanner(document, {
    headline: latestSnapshot.warningHeadline,
    body: latestSnapshot.warningBody,
    onDismissOverride: armOneShotOverride,
  });
}

function reinstallInterceptors(): void {
  const { sendButton, inputArea } = findChatElements(document);

  if (
    sendButton === installedSendButton &&
    inputArea === installedInputArea &&
    interceptCleanup !== null
  ) {
    return;
  }

  interceptCleanup?.();
  interceptCleanup = null;
  installedSendButton = null;
  installedInputArea = null;

  if (!sendButton && !inputArea) {
    return;
  }

  const result = installSendInterceptors(document, getInterceptState, {
    onBlocked: showBlockedWarning,
    onOverrideConsumed: consumeOneShotOverride,
  });

  if (!result.foundElements) {
    return;
  }
  interceptCleanup = result.cleanup;
  installedSendButton = sendButton;
  installedInputArea = inputArea;
}

function applySnapshot(snapshot: QuotaSnapshot): void {
  latestSnapshot = snapshot;
  allowNextSend = false;

  if (snapshot.error) {
    removeIndicator(document);
    interceptCleanup?.();
    interceptCleanup = null;
    installedSendButton = null;
    installedInputArea = null;
    return;
  }

  renderIndicator(
    document,
    snapshot.indicatorText,
    snapshot.isLow,
    snapshot.isExhausted,
  );

  if (snapshot.isExhausted && snapshot.warningHeadline && snapshot.warningBody) {
    renderWarningBanner(document, {
      headline: latestSnapshot.warningHeadline!,
      body: latestSnapshot.warningBody!,
      onDismissOverride: armOneShotOverride,
    });
  } else {
    document.getElementById("claude-share-quota-warning")?.remove();
  }

  reinstallInterceptors();
}

function requestInitialSnapshot(): void {
  chrome.runtime.sendMessage({ type: "get-quota" }, (response) => {
    if (chrome.runtime.lastError) {
      return;
    }
    if (response?.snapshot) {
      applySnapshot(response.snapshot as QuotaSnapshot);
    }
  });
}

chrome.runtime.onMessage.addListener((message) => {
  if (message?.type === "quota-update" && message.snapshot) {
    applySnapshot(message.snapshot as QuotaSnapshot);
  }
});

function startDomWatch(): void {
  if (domObserver) {
    return;
  }
  domObserver = new MutationObserver(() => {
    reinstallInterceptors();
  });
  domObserver.observe(document.documentElement, { childList: true, subtree: true });
}

try {
  requestInitialSnapshot();
  startDomWatch();
} catch {
  // fail open: never break claude.ai
}
