# Claude Share Browser Extension (Milestone 6)

## What this is

A **Manifest V3 Chrome/Chromium extension** that shows your Claude Share quota
while using [claude.ai](https://claude.ai) in a browser. It talks to the
**same Milestone 5 central HTTP server** as the CLI and Claude Code hook —
it does **not** implement quota logic locally and does **not** work in pure
local-SQLite-only mode (a browser extension cannot read your local SQLite file).

It provides:

- A persistent on-page indicator (e.g. `Claude Share: 62% remaining, resets in 2h 14m`)
- A visible warning when capacity is low or exhausted
- **Best-effort** send interception (disabled send button / Enter-to-send) when exhausted, with an explicit **“Send anyway (one message)”** escape hatch that allows exactly **one** send attempt through, then re-arms the block immediately

## Reliability limitations (read this first)

**This extension is intentionally soft enforcement — not a security boundary.**

Unlike the Claude Code `UserPromptSubmit` hook (a documented, first-party
“block before send” integration), claude.ai has **no equivalent stable API**.
This extension can only:

- Observe the page DOM
- Inject UI (indicator, warnings)
- Try to intercept send actions via DOM event handling

That means:

1. **It can break silently** whenever claude.ai changes its page structure.
   The content script depends on heuristic CSS selectors (see below) — not a
   documented API.
2. **It can be bypassed** (DevTools, different UI paths, timing races, or
   using “Send anyway (one message)” — which deliberately allows only a
   single send through before the block re-arms).
3. **It fails open by design** — if selectors fail, the server is unreachable,
   or anything throws internally, the extension **does not** trap you on
   claude.ai. No indicator, no interception, no visible errors on the page.
   This matches the Claude Code hook’s fail-open philosophy.

Do not treat this as hard quota enforcement. Treat it as **visibility + a
nudge**, backed by the same server data the CLI/hook already use.

## Requirements

- A running **Claude Share Milestone 5 server** reachable from your browser
- Chrome or another Chromium browser with Manifest V3 support
- A **device API token** from the existing `POST /devices` registration flow
  (same as CLI remote mode — no separate auth mechanism)
- Your **member_id** (from `claude-share pool create` output or `join`)

## Install (unpacked, development / personal use)

This milestone does **not** include Chrome Web Store packaging.

```bash
cd extension
npm install
npm run build
```

Then in Chrome:

1. Open `chrome://extensions`
2. Enable **Developer mode**
3. Click **Load unpacked**
4. Select the `extension/` directory (the folder containing `manifest.json`)

After changing TypeScript source, run `npm run build` again and click **Reload**
on the extension card.

## Configure

1. Click the extension icon to open the popup.
2. Enter your **server URL** (e.g. `https://your-server.example.com` or
   `http://127.0.0.1:8000` for local dev).
3. Either:
   - Paste an existing **device token** + **member_id**, or
   - Use **Register device** with your `user_id` (from pool create) to call
     `POST /devices` and fill in the token automatically.
4. Click **Save configuration**. Chrome will prompt for **host permission** to
   reach your server URL (via `optional_host_permissions`).

The background worker polls:

- `GET /members/{member_id}/status`
- `GET /members/{member_id}/capacity?window=five_hour`

every minute (and on demand from the popup).

## DOM selectors the content script depends on

These are **heuristic and fragile**:

| Purpose | Selector(s) tried |
|---------|-------------------|
| Send button | `button[aria-label*="Send" i]`, `button[data-testid*="send" i]`, `button[type="submit"]` |
| Chat input | `div[contenteditable="true"][role="textbox"]`, `textarea`, `[data-testid*="composer" i] [contenteditable="true"]` |

If neither a send button nor an input area matches, the content script **fail-opens**:
no UI injection, no interception, no thrown errors on the page. A `MutationObserver`
re-tries when the DOM changes (e.g. SPA navigation).

## Server / CORS note

API calls are made from the **extension background service worker and popup**,
not from the claude.ai page itself. With Chrome **host permissions** granted
for your server URL, these requests are **not subject to normal web CORS**.
No server-side CORS middleware was added for this milestone.

## Tests

```bash
cd extension
npm test
```

Uses **Vitest** + **jsdom**. Covers:

- Quota percentage / time-remaining formatting
- Fail-open behavior when expected DOM elements are absent
- Exhausted-capacity intercept logic in isolation (mock DOM)

## Out of scope (this milestone)

- Chrome Web Store publishing
- Firefox / Safari / other browsers
- New server business logic or endpoints
- Real Anthropic account integration
- Hard blocking beyond best-effort send interception
- Dashboard beyond this popup + on-page indicator
