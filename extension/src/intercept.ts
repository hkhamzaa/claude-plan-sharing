/**
 * Best-effort send interception helpers, isolated from claude.ai DOM structure
 * so behavior can be unit-tested without a live page.
 */

export interface SendInterceptState {
  isExhausted: boolean;
  /** When true, the very next send attempt is allowed through, then cleared. */
  allowNextSend: boolean;
}

export function shouldInterceptSend(state: SendInterceptState): boolean {
  return state.isExhausted && !state.allowNextSend;
}

export interface DomSelectors {
  sendButton: string;
  inputArea: string;
}

/** Selectors the content script tries, in order of preference. Fragile by design. */
export const DEFAULT_SELECTORS: DomSelectors = {
  sendButton:
    'button[aria-label*="Send" i], button[data-testid*="send" i], button[type="submit"]',
  inputArea:
    'div[contenteditable="true"][role="textbox"], textarea, [data-testid*="composer" i] [contenteditable="true"]',
};

export function findChatElements(
  root: ParentNode,
  selectors: DomSelectors = DEFAULT_SELECTORS,
): { sendButton: HTMLElement | null; inputArea: HTMLElement | null } {
  const sendButton = root.querySelector<HTMLElement>(selectors.sendButton);
  const inputArea = root.querySelector<HTMLElement>(selectors.inputArea);
  return { sendButton, inputArea };
}

export interface InterceptHandlers {
  onBlocked: () => void;
  /** Called when a one-shot override is consumed to allow a single send. */
  onOverrideConsumed?: () => void;
}

export interface InterceptInstallResult {
  foundElements: boolean;
  cleanup: () => void;
}

function isSendShortcut(event: KeyboardEvent): boolean {
  return event.key === "Enter" && !event.shiftKey && !event.ctrlKey && !event.metaKey && !event.altKey;
}

export function installSendInterceptors(
  root: ParentNode,
  getState: () => SendInterceptState,
  handlers: InterceptHandlers,
  selectors: DomSelectors = DEFAULT_SELECTORS,
): InterceptInstallResult {
  const { sendButton, inputArea } = findChatElements(root, selectors);
  if (!sendButton && !inputArea) {
    return { foundElements: false, cleanup: () => undefined };
  }

  const cleanups: Array<() => void> = [];

  const blockIfNeeded = (event: Event): boolean => {
    const state = getState();
    if (!state.isExhausted) {
      return false;
    }
    if (state.allowNextSend) {
      handlers.onOverrideConsumed?.();
      return false;
    }
    event.preventDefault();
    event.stopPropagation();
    handlers.onBlocked();
    return true;
  };

  if (sendButton) {
    const onClick = (event: Event) => {
      blockIfNeeded(event);
    };
    sendButton.addEventListener("click", onClick, true);
    cleanups.push(() => sendButton.removeEventListener("click", onClick, true));

    if (sendButton instanceof HTMLButtonElement) {
      const previousDisabled = sendButton.disabled;
      const syncDisabled = () => {
        try {
          sendButton.disabled = shouldInterceptSend(getState());
        } catch {
          // fail open
        }
      };
      syncDisabled();
      const interval = window.setInterval(syncDisabled, 500);
      cleanups.push(() => {
        window.clearInterval(interval);
        try {
          sendButton.disabled = previousDisabled;
        } catch {
          // fail open
        }
      });
    }
  }

  if (inputArea) {
    const onKeyDown = (event: KeyboardEvent) => {
      if (isSendShortcut(event)) {
        blockIfNeeded(event);
      }
    };
    inputArea.addEventListener("keydown", onKeyDown, true);
    cleanups.push(() => inputArea.removeEventListener("keydown", onKeyDown, true));
  }

  return {
    foundElements: true,
    cleanup: () => {
      for (const cleanup of cleanups) {
        try {
          cleanup();
        } catch {
          // fail open
        }
      }
    },
  };
}

export const INDICATOR_ID = "claude-share-quota-indicator";
export const WARNING_ID = "claude-share-quota-warning";

export function renderIndicator(root: Document, text: string, isLow: boolean, isExhausted: boolean): HTMLElement | null {
  try {
    let indicator = root.getElementById(INDICATOR_ID);
    if (!indicator) {
      indicator = root.createElement("div");
      indicator.id = INDICATOR_ID;
      indicator.setAttribute("data-claude-share", "indicator");
      root.body?.appendChild(indicator);
    }
    indicator.textContent = `Claude Share: ${text}`;
    indicator.style.cssText = [
      "position:fixed",
      "top:12px",
      "right:12px",
      "z-index:2147483646",
      "padding:8px 12px",
      "border-radius:8px",
      "font:13px/1.4 system-ui,sans-serif",
      "box-shadow:0 2px 8px rgba(0,0,0,0.15)",
      isExhausted ? "background:#7f1d1d;color:#fff" : isLow ? "background:#92400e;color:#fff" : "background:#1f2937;color:#fff",
    ].join(";");
    return indicator;
  } catch {
    return null;
  }
}

export function removeIndicator(root: Document): void {
  try {
    root.getElementById(INDICATOR_ID)?.remove();
    root.getElementById(WARNING_ID)?.remove();
  } catch {
    // fail open
  }
}

export interface WarningBannerOptions {
  headline: string;
  body: string;
  onDismissOverride: () => void;
}

export function renderWarningBanner(root: Document, options: WarningBannerOptions): HTMLElement | null {
  try {
    let banner = root.getElementById(WARNING_ID);
    if (!banner) {
      banner = root.createElement("div");
      banner.id = WARNING_ID;
      banner.setAttribute("data-claude-share", "warning");
      root.body?.appendChild(banner);
    }

    banner.replaceChildren();
    banner.style.cssText = [
      "position:fixed",
      "bottom:16px",
      "left:50%",
      "transform:translateX(-50%)",
      "z-index:2147483647",
      "max-width:520px",
      "width:calc(100% - 32px)",
      "padding:12px 14px",
      "border-radius:10px",
      "background:#450a0a",
      "color:#fff",
      "font:13px/1.45 system-ui,sans-serif",
      "box-shadow:0 4px 16px rgba(0,0,0,0.25)",
    ].join(";");

    const title = root.createElement("div");
    title.textContent = options.headline;
    title.style.fontWeight = "600";
    title.style.marginBottom = "6px";

    const body = root.createElement("pre");
    body.textContent = options.body;
    body.style.margin = "0 0 10px 0";
    body.style.whiteSpace = "pre-wrap";
    body.style.font = "inherit";

    const actions = root.createElement("div");
    actions.style.display = "flex";
    actions.style.gap = "8px";

    const dismiss = root.createElement("button");
    dismiss.type = "button";
    dismiss.textContent = "Send anyway (one message)";
    dismiss.style.cssText = "cursor:pointer;border:0;border-radius:6px;padding:6px 10px;background:#fff;color:#450a0a;font:inherit";
    dismiss.addEventListener("click", () => {
      options.onDismissOverride();
      banner?.remove();
    });

    const note = root.createElement("span");
    note.textContent = "Best-effort only — can be bypassed.";
    note.style.opacity = "0.85";
    note.style.alignSelf = "center";

    actions.appendChild(dismiss);
    actions.appendChild(note);
    banner.appendChild(title);
    banner.appendChild(body);
    banner.appendChild(actions);
    return banner;
  } catch {
    return null;
  }
}
