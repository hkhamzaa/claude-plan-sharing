import { describe, expect, it } from "vitest";
import {
  DEFAULT_SELECTORS,
  findChatElements,
  installSendInterceptors,
  removeIndicator,
  renderIndicator,
  shouldInterceptSend,
} from "../src/intercept.js";

describe("shouldInterceptSend", () => {
  it("blocks only when exhausted and no one-shot override pending", () => {
    expect(shouldInterceptSend({ isExhausted: true, allowNextSend: false })).toBe(true);
    expect(shouldInterceptSend({ isExhausted: true, allowNextSend: true })).toBe(false);
    expect(shouldInterceptSend({ isExhausted: false, allowNextSend: false })).toBe(false);
  });
});

describe("fail-open when DOM elements are absent", () => {
  it("findChatElements returns nulls on an empty document", () => {
    document.body.innerHTML = "";
    const found = findChatElements(document, DEFAULT_SELECTORS);
    expect(found.sendButton).toBeNull();
    expect(found.inputArea).toBeNull();
  });

  it("installSendInterceptors does not throw and reports foundElements=false", () => {
    document.body.innerHTML = "";
    let blocked = false;
    expect(() => {
      const result = installSendInterceptors(
        document,
        () => ({ isExhausted: true, allowNextSend: false }),
        { onBlocked: () => {
          blocked = true;
        } },
      );
      expect(result.foundElements).toBe(false);
      result.cleanup();
    }).not.toThrow();
    expect(blocked).toBe(false);
  });

  it("removeIndicator and renderIndicator fail open without throwing", () => {
    document.body.innerHTML = "";
    expect(() => removeIndicator(document)).not.toThrow();
    expect(() => renderIndicator(document, "test", false, false)).not.toThrow();
  });

  it("does not inject indicator UI when body is unavailable", () => {
    const brokenDoc = {
      getElementById: () => null,
      createElement: () => {
        throw new Error("DOM unavailable");
      },
      body: null,
    } as unknown as Document;
    expect(renderIndicator(brokenDoc, "test", false, false)).toBeNull();
    expect(() => removeIndicator(brokenDoc)).not.toThrow();
  });
});

describe("exhausted-capacity intercept logic", () => {
  it("blocks send button clicks when exhausted", () => {
    document.body.innerHTML = `
      <button id="send" aria-label="Send message">Send</button>
      <div contenteditable="true" role="textbox"></div>
    `;

    let blocked = false;
    installSendInterceptors(
      document,
      () => ({ isExhausted: true, allowNextSend: false }),
      { onBlocked: () => {
        blocked = true;
      } },
      {
        sendButton: "#send",
        inputArea: '[role="textbox"]',
      },
    );

    const button = document.getElementById("send")!;
    button.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    expect(blocked).toBe(true);
  });

  it("allows exactly one send after override, then blocks again", () => {
    document.body.innerHTML = `<button id="send" aria-label="Send message">Send</button>`;
    let allowNextSend = false;
    let blocked = false;
    let overrideConsumptions = 0;

    installSendInterceptors(
      document,
      () => ({ isExhausted: true, allowNextSend }),
      {
        onBlocked: () => {
          blocked = true;
        },
        onOverrideConsumed: () => {
          allowNextSend = false;
          overrideConsumptions += 1;
        },
      },
      { sendButton: "#send", inputArea: "missing" },
    );

    const button = document.getElementById("send")!;
    button.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    expect(blocked).toBe(true);

    allowNextSend = true;
    blocked = false;
    button.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    expect(blocked).toBe(false);
    expect(overrideConsumptions).toBe(1);

    blocked = false;
    button.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    expect(blocked).toBe(true);
  });

  it("blocks Enter-to-send on the input area", () => {
    document.body.innerHTML = `<div id="input" contenteditable="true" role="textbox"></div>`;
    let blocked = false;

    installSendInterceptors(
      document,
      () => ({ isExhausted: true, allowNextSend: false }),
      { onBlocked: () => {
        blocked = true;
      } },
      { sendButton: "missing", inputArea: "#input" },
    );

    const input = document.getElementById("input")!;
    const event = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
    input.dispatchEvent(event);
    expect(blocked).toBe(true);
    expect(event.defaultPrevented).toBe(true);
  });
});
