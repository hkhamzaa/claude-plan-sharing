# How to Set Up Claude Share

This guide walks through everything needed to run Claude Share for a small trusted group: setting up the central server, creating a pool, and getting each person connected across the CLI, Claude Code, the browser extension, and the dashboard.

It's written from real experience deploying this exact system — every command here was actually run and confirmed working, including the fixes for the gotchas we hit along the way (Python version mismatches, double firewalls, GitHub token permissions, etc.).

**Who this is for:** one person acts as the "owner" — they set up the server and create the pool. Everyone else just needs to install the client and join.

---

## Part 0 — Before you start

- All members must be people you trust. Claude Share coordinates *fair sharing* of one Claude subscription among people who've agreed to this arrangement — it does not create separate real Anthropic accounts, and using one account's login across multiple people is a violation of Anthropic's Consumer Terms of Service. This is a deliberate, accepted risk for a small trusted group — go in with eyes open.
- Claude Share tracks a **cooperative, self-reported fair-share ledger** — it does not have access to Anthropic's real internal usage counters. It measures real token usage per person (as of Milestone 8) and enforces fairness *among your group*, but it cannot verify or guarantee your combined usage never exceeds what the underlying Anthropic account actually allows.
- You'll need:
  - A server to run the central coordinator on (this guide assumes a Linux server you control — we used an Oracle Cloud Always Free ARM instance, but any Ubuntu-like server works)
  - Python 3.12+ on the server and on every client machine
  - Git and a GitHub account with access to the private repo
  - [Tailscale](https://tailscale.com) installed on the server and every client device (recommended — see Part 7)

---

## Part 1 — Owner: Set up the server

### 1.1 — Connect to your server

```bash
ssh -i /path/to/your-key.pem ubuntu@YOUR_SERVER_IP
```

### 1.2 — If this server runs anything else already, check first

Before installing anything, confirm you won't collide with existing services:

```bash
sudo ss -tulpn | grep LISTEN
df -h
systemctl list-units --type=service --state=running
```

Note what ports and disk space are already in use. Claude Share needs one free TCP port (we use **8001**, since 8000 is the code default but often already taken by other services) and Postgres's default port **5432**.

### 1.3 — Install Postgres

```bash
sudo apt update
sudo apt install -y postgresql postgresql-contrib libpq-dev python3-dev build-essential
```

### 1.4 — Create an isolated database and user

```bash
sudo -u postgres psql -c "CREATE DATABASE claude_share;"
sudo -u postgres psql -c "CREATE USER claude_share_user WITH PASSWORD 'REPLACE_WITH_A_REAL_PASSWORD';"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE claude_share TO claude_share_user;"
```

Generate a strong password instead of typing one yourself:

```bash
openssl rand -base64 24
```

Use that output as the password above (replacing `REPLACE_WITH_A_REAL_PASSWORD` in the `CREATE USER` command), and **save it somewhere safe** — you'll need it again in Step 1.8. If you already ran the command with a placeholder, fix it with:

```bash
sudo -u postgres psql -c "ALTER USER claude_share_user WITH PASSWORD 'your_real_password';"
```

> Note: you'll see `could not change directory to "/home/ubuntu": Permission denied` on every `sudo -u postgres psql` command — this is harmless and expected (it's `psql` failing to `cd` into a directory it doesn't need), not an error with the actual SQL command.

### 1.5 — Check your Python version

Claude Share requires **Python 3.12+**. Check what you have:

```bash
python3 --version
```

If it's older than 3.12 (common on Ubuntu 22.04, which ships 3.10), install 3.12 *alongside* your existing Python — do not replace the system default, since other software on the server may depend on it:

```bash
sudo add-apt-repository ppa:deadsnakes/ppa -y
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3.12-dev
```

Verify both versions now exist side by side:

```bash
python3.12 --version   # should show 3.12.x
python3 --version      # should be unchanged from before
```

> If `apt install` opens a text dialog asking "Which services should be restarted?" — if this server runs anything else important, uncheck those specific services in the list (use arrow keys + spacebar to toggle, Tab to reach `<Ok>`, Enter to confirm) before continuing, so unrelated live services aren't disrupted.

### 1.6 — Clone the repository

If the repo is private, you'll need a GitHub Personal Access Token:

1. Go to `https://github.com/settings/tokens?type=beta`
2. Generate a new fine-grained token
3. Set **Repository access** → "Only select repositories" → choose your repo
4. Under **Permissions → Repository permissions**, set **Contents** to **Read-only** (this step is easy to miss — without it, cloning will fail with a 403 even though the token looks valid)
5. Generate and copy the token

Then clone:

```bash
cd /home/ubuntu
git clone https://YOUR_GITHUB_USERNAME:YOUR_TOKEN@github.com/YOUR_USERNAME/claude-plan-sharing.git claude-share
cd claude-share
```

(If the repo is public, drop the username/token from the URL.)

### 1.7 — Create an isolated virtual environment using Python 3.12

```bash
python3.12 -m venv venv
source venv/bin/activate
pip install --upgrade pip
```

Confirm it's using the right version:

```bash
python --version   # should show 3.12.x
```

### 1.8 — Install dependencies and configure the database connection

```bash
pip install -e ".[server,client]"
```

Set the database connection string (using the password you saved in Step 1.4):

```bash
echo 'export CLAUDE_SHARE_DATABASE_URL="postgresql://claude_share_user:YOUR_PASSWORD@localhost:5432/claude_share"' >> ~/.bashrc
source ~/.bashrc
```

### 1.9 — Test-run the server manually

```bash
export CLAUDE_SHARE_SERVER_HOST="0.0.0.0"
export CLAUDE_SHARE_SERVER_PORT="8001"
claude-share-server
```

You should see:
```
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8001
```

The database schema is created automatically on first startup — no separate migration step needed.

In a **second** terminal (open a new SSH connection), confirm it's responding locally:

```bash
curl http://localhost:8001/docs
```

You should get back HTML (FastAPI's Swagger docs page). Once confirmed, go back to the first terminal and press **Ctrl+C** to stop the manual test run — we'll run it properly as a background service next.

### 1.10 — Run it as a permanent background service (systemd)

This makes the server survive SSH disconnects, crashes, and reboots.

```bash
sudo nano /etc/systemd/system/claude-share-server.service
```

Paste this in (replace `YOUR_PASSWORD` with your real database password):

```ini
[Unit]
Description=Claude Share central server
After=network.target postgresql.service
Requires=postgresql.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/claude-share
Environment=CLAUDE_SHARE_DATABASE_URL=postgresql://claude_share_user:YOUR_PASSWORD@localhost:5432/claude_share
Environment=CLAUDE_SHARE_SERVER_HOST=0.0.0.0
Environment=CLAUDE_SHARE_SERVER_PORT=8001
ExecStart=/home/ubuntu/claude-share/venv/bin/claude-share-server
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Save and exit: **Ctrl+O**, **Enter**, **Ctrl+X**.

Enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable claude-share-server.service
sudo systemctl start claude-share-server.service
sudo systemctl status claude-share-server.service
```

You should see `Active: active (running)`.

> **If you get "address already in use"**: something (likely your Step 1.9 manual test) is still holding port 8001. Find and stop it:
> ```bash
> sudo ss -tulpn | grep 8001
> sudo kill <PID_SHOWN>
> sudo systemctl restart claude-share-server.service
> ```

**To stop the server later** (e.g. when not actively using it):
```bash
sudo systemctl stop claude-share-server.service
```
**To start it again:**
```bash
sudo systemctl start claude-share-server.service
```
It stays *enabled* either way, meaning it'll auto-start on a reboot only if it was left `active` — running `stop` does not disable it, so if you want it to definitely not come back after a reboot, also run `sudo systemctl disable claude-share-server.service`.

---

## Part 2 — Owner: Open network access

**Strongly recommended: use Tailscale instead of exposing this to the public internet.** See **Part 7** for the full walkthrough — a server with an open port on the public internet gets scanned by bots within hours (harmless against this system's auth, but unnecessary noise and exposure). The steps below are for initial testing only; switch to Tailscale before onboarding your group for real.

If you're testing without Tailscale first, you need to open port 8001 in **two separate places** — missing either one means the port stays blocked even though the other is open.

### 2.1 — Cloud provider firewall (e.g. Oracle Cloud Security List)

In your cloud provider's console, add an inbound/ingress rule:
- Source: `0.0.0.0/0` (or your own IP for tighter testing)
- Protocol: TCP
- Port: `8001`

### 2.2 — Server's own firewall (iptables)

Check if `ufw` is active first:
```bash
sudo ufw status
```
If `inactive`, check the underlying `iptables` rules directly — some cloud images pre-configure these separately from `ufw`:
```bash
sudo iptables -L INPUT -n --line-numbers
```
If you see a rule rejecting everything except SSH (port 22), insert an allow rule for 8001 before that reject rule (replace `4` with whatever line number sits just before your REJECT rule):
```bash
sudo iptables -I INPUT 4 -p tcp --dport 8001 -m state --state NEW -j ACCEPT
```

Make it survive a reboot:
```bash
sudo apt install -y iptables-persistent
sudo netfilter-persistent save
```

### 2.3 — Verify from outside

From your own computer (not SSH'd into the server):
```bash
curl http://YOUR_SERVER_IP:8001/docs
```
Should return HTML. If it hangs/fails, double check both 2.1 and 2.2.

---

## Part 3 — Owner: Create the pool

Once the server's running and reachable, create your pool — this only needs to happen once, ever, per group.

`--server` is a **global** flag and must come **before** the subcommand:

```bash
claude-share --server http://YOUR_SERVER_ADDRESS:8001 pool create --name "MyTeam" --members "Hamza,Alice,Bob,Carol"
```

This prints something like:

```
Created pool 'abc123' ('MyTeam') with 4 member(s):
  Hamza: member_id=m1 user_id=u1
  Alice: member_id=m2 user_id=u2
  Bob: member_id=m3 user_id=u3
  Carol: member_id=m4 user_id=u4
```

**Save this entire output.** You need to privately send each person their own `user_id` and `member_id` (not everyone else's), plus the shared `pool_id` and the server address. Treat these like credentials — send them privately, not in a group chat.

The quota is split automatically and evenly (e.g. 4 people = 25% each, using precise math that always sums to exactly 100% even when the split isn't even).

---

## Part 4 — Everyone (including the owner): Set up your own device

Each person does this once per device they plan to use.

### 4.1 — Install prerequisites

- Python 3.12+
- Git access to the repo (same token setup as Part 1.6 if the repo is private)

### 4.2 — Clone and install

```bash
git clone https://YOUR_USERNAME:YOUR_TOKEN@github.com/YOUR_USERNAME/claude-plan-sharing.git claude-share
cd claude-share
python3.12 -m venv venv
source venv/bin/activate   # or venv\Scripts\Activate.ps1 on Windows
pip install --upgrade pip
pip install -e ".[client]"
```

(Client-only install — you don't need the `server`/`postgres` extras unless you're the owner running the server itself.)

### 4.3 — Log in, pointing at the owner's server

Using the `user_id` the owner sent you privately (`--server` is global — place it before `login`):

```bash
claude-share --server http://SERVER_ADDRESS:8001 login --user-id YOUR_USER_ID --device-name "YourName-Laptop"
```

The device bearer token is **not** printed to the terminal — it is saved in `~/.claude-share/config.json` as `device_token`. You'll need that value for the browser extension and dashboard (Part 6 and Part 8).

### 4.4 — Join the pool

Using the `pool_id` and your own `member_id`:

```bash
claude-share join --pool YOUR_POOL_ID --member YOUR_MEMBER_ID
```

### 4.5 — Confirm it worked

```bash
claude-share status
```

You should see your personal quota — both the 5-hour and weekly windows — pulled live from the server.

---

## Part 5 — Set up Claude Code integration (recommended)

This installs **two** hooks: a **UserPromptSubmit** hook (checks quota before each prompt) and a **Stop** hook (meters real token usage after each turn completes and calls `consume()` on the server).

```bash
claude-share hook install --project
```

(Use `--user` instead of `--project` to apply it to every project on your machine, not just the current one.)

From now on:
- If you have capacity, prompts go through silently.
- If you're running low, you'll see a warning but the prompt still goes through.
- If you're exhausted, the prompt is blocked with a clear message showing your usage and when it resets.
- After each response completes, your *real* token usage for that turn is measured and deducted — a long, complex prompt costs more than a short one, proportional to actual usage, not a flat count per prompt.

This works identically whether you use Claude Code in a plain terminal or inside VS Code's Claude Code panel — it's the same underlying mechanism either way.

To remove it later:
```bash
claude-share hook uninstall --project
```

---

## Part 6 — Set up the browser extension (optional)

For quota visibility while using claude.ai directly in Chrome.

```bash
cd extension
npm install
npm run build
```

Then in Chrome:
1. Go to `chrome://extensions`
2. Enable **Developer mode** (top right)
3. Click **Load unpacked**
4. Select the **`extension/`** directory (the folder containing `manifest.json`, not `dist/`)
5. Click the extension icon, enter your server address, your **device token** (from `~/.claude-share/config.json` → `device_token`), and your `member_id`

You'll see a small quota indicator on claude.ai pages. If you're exhausted, you'll see a warning banner with a best-effort send-block — this is explicitly a soft, bypassable warning (browsers don't allow a hard block the way Claude Code's hook does), including a one-time **"Send anyway (one message)"** override, clearly labeled as such.

---

## Part 7 — Recommended: switch to Tailscale instead of a public port

Once you've confirmed everything works over a plain public IP, switch to Tailscale — it gives every device an encrypted private network with no public exposure, no domain name, and no certificates to manage. This matters because plain HTTP sends your device tokens across the network in cleartext.

Full details are in [`docs/TAILSCALE_SETUP.md`](docs/TAILSCALE_SETUP.md). Summary:

**On the server:**
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale ip -4
```
Note the resulting Tailscale IP (looks like `100.x.y.z`).

**On each client device (Windows example):**
```powershell
winget install Tailscale.Tailscale
```
Sign in via the tray app, joining the same tailnet the server joined. Then confirm connectivity:
```powershell
ping 100.x.y.z
curl http://100.x.y.z:8001/docs
```

**Re-point everyone's config** to use `http://100.x.y.z:8001` instead of the public IP (re-run `login` with the new `--server` value, and update the extension/dashboard's saved server URL).

**Then close the public exposure entirely** — remove the cloud firewall rule from Part 2.1, and remove the iptables rule from Part 2.2:
```bash
sudo iptables -L INPUT -n --line-numbers    # find the rule's line number
sudo iptables -D INPUT <LINE_NUMBER>
sudo netfilter-persistent save
```

---

## Part 8 — Optional: the dashboard

A web page showing your status, the whole pool's status, pending approval requests, and active grants — with buttons instead of typing commands.

On the server (or anywhere with the repo checked out):

```bash
cd dashboard
npm install
npm run build
```

It's served by the same central server at `/dashboard/` — visit `http://YOUR_SERVER_ADDRESS:8001/dashboard/` (or the Tailscale address, once switched over), and enter your device token + member ID on first load (token from `~/.claude-share/config.json`).

There is **no CLI command** to list pending requests — use the dashboard (or the API docs at `/docs`) for that.

---

## Part 9 — Requesting and sharing quota

If someone's running low and a teammate has spare capacity, either type works:

- **SOLID** — a real, ongoing transfer of quota from one person to another.
- **SHARED** — conditional, revocable borrowing: the lender keeps first priority over their own capacity at all times, and the borrower can only use what the lender genuinely isn't using at that moment.

To request:
```bash
claude-share request --pool POOL_ID --from LENDER_MEMBER_ID --to BORROWER_MEMBER_ID --window five_hour --amount 500 --type shared --message "optional note"
```

- `--from` is the capacity owner who must approve; `--to` is the requester who would receive it.
- `--window` is which of the two tracking clocks you mean: `five_hour` or `weekly` — these are two separate, independent limits, not one nested inside the other.
- `--amount` is a number of abstract quota units out of the pool's total 10,000 (so 500 = 5% of the whole pool).

**There's no automatic notification** — the lender has to actively check for pending requests via the dashboard's "Pending Requests" view (or `GET /members/{id}/capacity/requests/pending` in the API). Tell them directly (message, call, whatever) that you've sent a request.

To approve:
```bash
claude-share request approve --request-id REQUEST_ID --by LENDER_MEMBER_ID
```

To revoke a grant later:
```bash
claude-share grant revoke --grant-id GRANT_ID --by LENDER_MEMBER_ID
```

---

## Quick reference — command summary

| Action | Command |
|---|---|
| Create pool (owner, once) | `claude-share --server URL pool create --name X --members "A,B,C"` |
| Log in (each device) | `claude-share --server URL login --user-id ID --device-name NAME` |
| Join pool (each device) | `claude-share join --pool POOL_ID --member MEMBER_ID` |
| Check status | `claude-share status` |
| Install Claude Code hooks | `claude-share hook install --project` |
| Uninstall hooks | `claude-share hook uninstall --project` |
| Request quota | `claude-share request --pool ID --from X --to Y --window five_hour --amount N --type shared` |
| Approve request | `claude-share request approve --request-id ID --by MEMBER_ID` |
| Revoke grant | `claude-share grant revoke --grant-id ID --by MEMBER_ID` |
| Start server (owner) | `sudo systemctl start claude-share-server.service` |
| Stop server (owner) | `sudo systemctl stop claude-share-server.service` |
| Check server status (owner) | `sudo systemctl status claude-share-server.service` |

---

## Known limitations (honest, not hidden)

- **No push notifications.** Pending requests require someone to actively check — nothing pings anyone automatically.
- **Fair-share tracking, not real Anthropic account metering.** Claude Share measures real token counts per person (as of the token-metering update) and enforces fairness among your group, but it has no access to Anthropic's actual internal usage counters — it cannot see or guarantee your combined real usage against the underlying subscription's true limits.
- **Browser extension enforcement is soft.** It's a best-effort warning with a deliberate one-message override, not a hard block — browsers don't offer a reliable mechanism for the kind of pre-send interception Claude Code's hook can do.
- **This arrangement (multiple people using one Claude account) conflicts with Anthropic's Consumer Terms of Service.** This was flagged at the start of the project and is a deliberately accepted risk for a small trusted group, not something Claude Share resolves or hides.
