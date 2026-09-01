# Tailscale setup for Claude Share

This guide is the **concrete deployment path** for running the Claude Share
central server (`claude-share-server`) on a small, trusted group of known
people without exposing bearer tokens and quota data on the public internet
in cleartext.

## Why Tailscale (and not a public domain + reverse proxy)

Claude Share's central server sends **device bearer tokens** and **quota
data** on every authenticated request. Milestone 5 documented that this must
not cross an untrusted network path in plain HTTP — but this project
deliberately does **not** terminate TLS inside the FastAPI app itself.

For **this deployment** (a fixed group of friends/family, not a public
service), Tailscale is the chosen solution:

| | Tailscale (chosen) | Public domain + Caddy/nginx |
|---|---|---|
| Who can reach the server | Only devices on your tailnet | Anyone who knows the URL |
| Encryption | WireGuard mesh (automatic) | HTTPS certificates you manage |
| Public port exposure | None required | Must open 443 (and often 80) |
| Domain name | Optional (MagicDNS) | Required |
| Certificate renewal | None | Let's Encrypt / manual |
| Per-person setup | Install Tailscale on each device | Usually just a browser |

**Trade-off:** every person who uses the CLI, browser extension, or
dashboard must install Tailscale and be invited to the same tailnet. That
is acceptable for a private family plan; it would be the wrong choice for a
public SaaS.

Within the tailnet, clients talk to the server over ordinary `http://`
URLs (for example `http://100.x.y.z:8001`). Traffic is encrypted by
Tailscale's mesh — you are **not** sending tokens in cleartext across the
public internet. This is **not** the same as "plain HTTP on Oracle Cloud's
public IP," which is what Milestone 9 closes.

---

## 1. Install Tailscale on the server (Ubuntu on Oracle Cloud)

SSH into the Oracle Cloud VM where `claude-share-server` already runs.

### Install

```bash
curl -fsSL https://tailscale.com/install.sh | sh
```

### Authenticate and join a tailnet

```bash
sudo tailscale up
```

The command prints a URL. Open it in a browser, sign in (or create a
Tailscale account), and approve this machine. When it succeeds, the server
is a member of your tailnet.

### Find the server's Tailscale address

**IPv4 (100.x.y.z range):**

```bash
tailscale ip -4
```

Example output: `100.64.12.34`

**MagicDNS hostname** (if enabled in your tailnet's admin console — recommended):

```bash
tailscale status
```

Look for this machine's name, e.g. `claude-share-server`. With MagicDNS,
other devices reach it as:

```text
http://claude-share-server.<your-tailnet>.ts.net:8001
```

(Replace `<your-tailnet>` with your actual tailnet name from the admin
console.)

### Confirm the Claude Share server is listening

On the server, verify the process is up (adjust port if yours differs from
8001):

```bash
curl -s http://127.0.0.1:8001/health
```

Expected: `{"ok":true}`

From **another device already on the tailnet** (after section 2), verify
over Tailscale:

```bash
curl -s http://100.x.y.z:8001/health
```

(use your real `tailscale ip -4` value)

---

## 2. Install Tailscale on a client (Windows)

Each person who runs the CLI, browser extension, or dashboard needs Tailscale
on their machine.

### Install

1. Download the Windows installer from [tailscale.com/download/windows](https://tailscale.com/download/windows), **or**
2. From PowerShell (if you use winget):

   ```powershell
   winget install Tailscale.Tailscale
   ```

3. Launch **Tailscale** from the Start menu and sign in with the **same
   tailnet** the server joined (same Google/Microsoft/GitHub account or
   invite link from the tailnet admin).

4. Confirm connected: the Tailscale tray icon should show **Connected**.

### Quick connectivity check

```powershell
ping 100.x.y.z
```

(use the server's Tailscale IP from `tailscale ip -4`)

Then:

```powershell
curl http://100.x.y.z:8001/health
```

Expected: `{"ok":true}`

---

## 3. Point Claude Share clients at the Tailscale address

Replace any **public IP** URL (e.g. `http://203.0.113.50:8001`) with the
Tailscale URL everywhere below. Both of these forms work:

- `http://100.x.y.z:8001` (Tailscale IPv4)
- `http://<device-name>.<tailnet>.ts.net:8001` (MagicDNS)

Use the **same base URL** on every client for a given deployment (pick one
style and stick to it).

### CLI (`claude-share`)

**New device / re-login** (registers against the server over Tailscale):

```bash
claude-share login --server http://100.x.y.z:8001 \
    --user-id <user_id> --device-name "Alice's Laptop"

claude-share join --pool <pool_id> --member <member_id>
```

**Bootstrap a pool on the server** (no prior identity):

```bash
claude-share --server http://100.x.y.z:8001 \
    pool create --name "Family Plan" --members "Alice,Bob"
```

**Already logged in with the old public IP?** The `server_url` is stored in
your local identity config (`~/.claude-share/config.json` by default). Either:

- Run `login --server` again with the new Tailscale URL (re-registers a
  device token), then `join` again, **or**
- Edit `config.json` manually: set `"server_url"` to the Tailscale URL
  (keep `"device_token"` unchanged if the server database is the same).

All other remote commands (`status`, `consume`, `capacity`, …) use whatever
`server_url` is already in that config file — no code changes needed.

### Browser extension

1. Open the extension popup → **Settings**.
2. Set **Server URL** to `http://100.x.y.z:8001` (or your MagicDNS URL).
3. Save. The extension requests **optional host permission** for that origin
   the first time (`http://*/*` is already declared as optional in
   `manifest.json`).
4. Re-register or paste your existing device token and member id if prompted.

### Dashboard

The dashboard is a static SPA served from the central server at
`/dashboard/` when the `dashboard/dist` build is present.

1. Open `http://100.x.y.z:8001/dashboard/` in a browser (while on Tailscale).
2. In the setup form, set **Server URL** to the same Tailscale base URL.
3. Register a device or enter an existing token, member id, and pool id.

Because the dashboard is **same-origin** with the API when loaded from
`/dashboard/`, no CORS configuration is required.

---

## 4. Close public port exposure (recommended end state)

Once every client reaches the server over Tailscale and `curl
http://100.x.y.z:8001/health` works from a Windows machine, **remove the
public internet path** to port 8001. There is no reason to keep it open.

### Oracle Cloud Security List

In the Oracle Cloud Console:

1. **Networking** → **Virtual cloud networks** → your VCN → **Security Lists**
   → the list attached to the server's subnet.
2. Find the **Ingress** rule that allows TCP port **8001** from `0.0.0.0/0`
   (or similar).
3. **Delete** that rule (or restrict it to a management IP you still need —
   but Tailscale-only is the goal).

### Server iptables (if you added a rule earlier)

If deployment work added a host firewall rule such as:

```bash
sudo iptables -I INPUT -p tcp --dport 8001 -j ACCEPT
```

Remove it after confirming Tailscale access works:

```bash
sudo iptables -D INPUT -p tcp --dport 8001 -j ACCEPT
```

Persist the change if your distro saves iptables rules (e.g. `iptables-save`
/ `netfilter-persistent`).

**Verify:** from a machine **not** on Tailscale, `curl
http://<public-ip>:8001/health` should **fail**. From a Tailscale client,
it should still succeed.

### Optional hardening: allow port 8001 only on the Tailscale interface

Not required, but available if you want the OS firewall to reject 8001 on
the public NIC even if a cloud rule is misconfigured:

```bash
# Allow Claude Share only on the Tailscale interface
sudo iptables -I INPUT -i tailscale0 -p tcp --dport 8001 -j ACCEPT

# Ensure a general ACCEPT for 8001 on all interfaces is NOT present
sudo iptables -D INPUT -p tcp --dport 8001 -j ACCEPT   # if it exists
```

Interface name is usually `tailscale0`; confirm with `ip link show tailscale0`.

---

## 5. Troubleshooting

### Tailnet admin / ACLs

Open [login.tailscale.com/admin](https://login.tailscale.com/admin):

- **Machines:** confirm both the server and each client show **Connected**.
- **DNS:** enable **MagicDNS** if you want `.ts.net` hostnames.
- **Access controls (ACLs):** default tailnets allow all members to reach
  all members. If you customized ACLs, ensure clients can reach the server's
  Tailscale IP on TCP **8001** (or your `CLAUDE_SHARE_SERVER_PORT`).

Inviting someone: **Users** → **Invite** → they install Tailscale and join
the same tailnet.

### Ping works but `curl http://100.x.y.z:8001/health` fails

Work through this list on the **server**:

1. **Is `claude-share-server` running?**

   ```bash
   curl -s http://127.0.0.1:8001/health
   ```

2. **Is it bound to the right interface?** Check `CLAUDE_SHARE_SERVER_HOST`.
   `0.0.0.0` listens on all interfaces (including Tailscale) — good.
   `127.0.0.1` only listens locally — remote Tailscale clients cannot connect;
   set `CLAUDE_SHARE_SERVER_HOST=0.0.0.0` and restart.

3. **Host firewall / iptables:** a rule may allow the public interface but
   not `tailscale0`, or `ufw` may block incoming traffic. Try temporarily:

   ```bash
   sudo iptables -I INPUT -i tailscale0 -p tcp --dport 8001 -j ACCEPT
   ```

4. **Wrong port:** confirm `CLAUDE_SHARE_SERVER_PORT` (default `8000` in
   code; your deployment may use `8001`).

5. **Tailscale not connected on server:** `tailscale status` should not show
   "Logged out."

### Extension cannot reach the server

- Confirm optional host permission was granted (Chrome prompts on save).
- Server URL must include scheme and port: `http://100.x.y.z:8001`, not
  `100.x.y.z:8001` alone.
- Tailscale must be **Connected** on Windows before opening claude.ai.

### CLI `RemoteRequestError` / connection refused

- Same checks as curl above.
- Verify `server_url` in config JSON matches the Tailscale URL exactly
  (no trailing slash required; the client normalizes it).

### "Works on public IP but not Tailscale"

Usually `CLAUDE_SHARE_SERVER_HOST=127.0.0.1` or a firewall that only opened
the public NIC. Bind to `0.0.0.0` and allow `tailscale0` in iptables (see
optional hardening above).

---

## Quick reference

| What | Tailscale URL example |
|------|------------------------|
| Health check | `http://100.64.12.34:8001/health` |
| API docs | `http://100.64.12.34:8001/docs` |
| Dashboard | `http://100.64.12.34:8001/dashboard/` |
| CLI `--server` | `http://100.64.12.34:8001` |

See also [architecture.md](architecture.md) (Milestone 9 — transport security)
and [README.md](../README.md) (Central server section).
