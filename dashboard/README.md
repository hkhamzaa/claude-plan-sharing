# Claude Share Dashboard (Milestone 7)

A read-only web UI (plus approve/reject/revoke actions) over the **existing
Milestone 5 HTTP API**. It does not implement quota or capacity business
logic — every number comes from server-computed fields, and every action
calls an existing endpoint.

## What it shows

1. **My status** — usage, guaranteed/potential capacity, reset times for both windows
2. **Pool overview** — all members side by side (one batch `GET /pools/{id}/overview` call)
3. **Pending requests** — incoming requests needing your approval
4. **Active grants** — sent and received SOLID/SHARED grants, with revoke on sent grants

## Requirements

- A running Claude Share central server (Milestone 5)
- A device bearer token (`POST /devices`, same as CLI/extension)
- Your `member_id`

## Build

```bash
cd dashboard
npm install
npm run build
```

This writes static files to `dashboard/dist/`.

## Serve

The FastAPI app mounts the built dashboard at **`/dashboard/`** when
`dashboard/dist/` exists (see `server/app.py`). Start the server as usual:

```bash
claude-share-server
# or uvicorn with your DATABASE_URL / uow_factory
```

Then open `http://<host>:<port>/dashboard/` (use HTTPS in production).

Because the dashboard and API share the same origin when served this way,
**leave Server URL blank** in the setup form — API calls use relative paths.

To develop the UI against a remote server instead, enter that server's base URL
in the setup form (you may need CORS configured separately if not same-origin).

## Configure

1. Open `/dashboard/`
2. Optionally register a device (`POST /devices`) or paste an existing token
3. Enter your `member_id` and save

Credentials are stored in **`localStorage`** on this browser only (same pattern
as the extension's device token storage).

## Tests

```bash
cd dashboard
npm test
```

Vitest covers client-side **display formatting only** (durations, dates, labels).

## Thin-client principle

If a number is not present in an API response field, the dashboard does not
compute it. Guaranteed, potential, remaining, and grant figures all come from
`GET /members/{id}/status`, `GET /members/{id}/capacity`, `GET /pools/{id}/overview`
(for the pool table), and the Milestone 7 read endpoints documented in
`docs/architecture.md`.

## Not included

- Request creation UI (CLI only for now)
- Real-time / websockets (60s polling)
- Multi-pool admin or user management
