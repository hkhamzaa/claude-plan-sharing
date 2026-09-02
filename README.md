# Claude Share

A quota-management and fair-sharing system for a trusted group of people who
share one underlying Claude subscription.

**Deploying for a group?** See **[SETUP.md](SETUP.md)** for the full server,
pool, CLI, Claude Code hook, extension, and dashboard walkthrough.

**Milestones 1-5 are implemented.** Milestone 1 is the local quota
engine: it divides 100% of a pool's logical quota equally among its
members and tracks consumption against two independent windows (a
five-hour window and a weekly window), backed by SQLite. Milestone 2 adds
capacity delegation between members of the same pool: **SOLID** transfers
(permanent-until-revoked) and **SHARED** grants (conditional, revocable
access to a member's currently-unused capacity, with the owner always
served first). Milestone 3 adds a local identity layer (`login`/`join`)
so a machine can be pointed at a pool/member once instead of retyping
`--member`/`--pool` on every command. Milestone 4 adds an actual Claude
Code integration: a `UserPromptSubmit` hook that checks quota before each
prompt and blocks/warns accordingly, installed via `hook install`.
Milestone 5 adds an **optional** central server (FastAPI + PostgreSQL) so
several devices can share one authoritative copy of this same state over
HTTP, with per-device bearer-token authentication — see "Central server
(Milestone 5)" below. **Local-only usage (Milestones 1-4) still works
exactly as before, with zero server/Postgres setup** — the server is a new
deployment *option*, not a replacement. See
[docs/architecture.md](docs/architecture.md) for what's in scope and
what's intentionally deferred, including the Postgres locking strategy and
why the device-token auth is intentionally minimal.

The quota engine only understands abstract "quota units" — it has no
knowledge of tokens, prompts, or dollars. Claude-specific logic will live in
adapters added in later milestones.

## Install

Requires Python 3.12+.

Using [uv](https://github.com/astral-sh/uv):

```bash
cd claude-share
uv venv
uv pip install -e ".[dev]"
```

Using plain `pip`:

```bash
cd claude-share
python -m venv .venv
# Windows: .venv\Scripts\activate | macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
```

`pip install -e .` alone (no extras) is all pure-local usage (Milestones
1-4: `pool create`, `status`, `consume`, `request`/`grant`, the Claude
Code hook) needs — it has zero third-party dependencies. The Milestone 5
pieces are opt-in extras:

- `.[postgres]` — just the Postgres-backed `UnitOfWork` (`psycopg`)
- `.[server]` — the FastAPI central server (`.[postgres]` + `fastapi` + `uvicorn` + `pydantic`)
- `.[client]` — the local agent's remote/HTTP mode (`httpx`)
- `.[dev]` — everything above, plus `pytest`, for running the full test suite

## CLI usage

The CLI stores its SQLite database at `~/.claude-share/claude_share.db` and
its local identity config at `~/.claude-share/config.json` by default.
Override with `--db <path>` / `--config <path>` on any command, or the
`CLAUDE_SHARE_DB` / `CLAUDE_SHARE_CONFIG` environment variables.

Create a pool (this also creates one member per name, splitting 100% of
quota equally across both windows):

```bash
claude-share pool create --name "Family Plan" --members "Alice,Bob,Carol"
```

This prints each member's `member_id` and `user_id` — save these, you'll
need them for the other commands:

```
Created pool '...' ('Family Plan') with 3 member(s):
  Alice: member_id=... user_id=...
  Bob: member_id=... user_id=...
  Carol: member_id=... user_id=...
```

### Local identity (`login` / `join` / `whoami`)

Instead of passing `--member`/`--pool` on every command, point this
machine at an identity once:

```bash
claude-share login --user-id <alice_user_id> --device-name "Alice's Laptop"
claude-share join --pool <pool_id> --member <alice_member_id>
claude-share whoami
```

`login` requires `--user-id` to already belong to an existing member
(there's no account-creation flow here — see
[docs/architecture.md](docs/architecture.md) for why this isn't real
authentication). `join` requires `login` to have run first, and verifies
the given member actually belongs to the given pool. `whoami` prints the
local identity plus, once joined, a live status/capacity snapshot — or
"Not logged in" if neither `login` nor `join` has been run yet.

Once joined, `status`, `consume`, `capacity`, and `request`/`grant revoke`
all default their identity flags (`--member`, `--pool`, `--to`, `--by`) to
this machine's local identity — but an explicit flag always overrides it,
so nothing here breaks passing ids by hand (needed for tests, or for
anyone acting on behalf of more than one identity from the same machine).

Check a member's current allocation/usage/remaining for both windows:

```bash
claude-share status                    # uses the joined identity
claude-share status --member <member_id>   # or specify explicitly
```

Consume quota units (idempotency key required — retrying the same key never
double-spends):

```bash
claude-share consume --window five_hour --amount 10 --idempotency-key req-001
claude-share consume --window weekly --amount 10 --idempotency-key req-002
```

A consume that would exceed the member's remaining balance in that window is
rejected (exit code 2) and leaves stored state unchanged.

### Capacity delegation (SOLID / SHARED)

```bash
claude-share request --pool <pool_id> --from <owner_member_id> --to <requester_member_id> \
    --window <five_hour|weekly> --amount <units> --type <solid|shared> [--message <text>]
claude-share request approve --request-id <id> --by <member_id>
claude-share request reject --request-id <id> --by <member_id>
claude-share grant revoke --grant-id <id> --by <member_id>
claude-share capacity --member <member_id> --window <five_hour|weekly>
```

`--from` is the member who owns the capacity and must approve (only they
can); `--to` is the member asking for it, who receives it once approved.
Only the grant's source can revoke it early. `--pool`/`--to`/`--by` all
default from local identity when configured (see above) — only `--from`
(always "the other party") and `--request-id`/`--grant-id` must be given
explicitly every time.

**Walkthrough — realistic two-device flow.** Alice and Bob each log in and
join on their own machine (simulated here with two `--config` files
against the same shared database), then every command below omits
`--member`/`--to`/`--by` in favor of each device's own joined identity:

```bash
claude-share pool create --name "Family Plan" --members "Alice,Bob"
# -> Alice: member_id=alice-id user_id=alice-user-id
# -> Bob:   member_id=bob-id   user_id=bob-user-id
# -> pool 'pool-id'

# Alice's machine:
claude-share --config alice.json login --user-id alice-user-id --device-name "Alice's Laptop"
claude-share --config alice.json join  --pool pool-id --member alice-id

# Bob's machine:
claude-share --config bob.json login --user-id bob-user-id --device-name "Bob's Laptop"
claude-share --config bob.json join  --pool pool-id --member bob-id
```

**SOLID — a permanent transfer:**

```bash
# Bob asks Alice for 1000 units of her five_hour quota, permanently.
# --to defaults to Bob (the joined identity on this device); --from is
# always explicit, since it's necessarily "someone else."
claude-share --config bob.json request --from alice-id \
    --window five_hour --amount 1000 --type solid
# -> Created solid request 'req-id': bob-id is asking alice-id for 1000 unit(s) ... status=pending

# Only Alice can approve; --by defaults to her joined identity.
claude-share --config alice.json request approve --request-id req-id
# -> Approved. Created solid grant 'grant-id': alice-id -> bob-id, 1000 unit(s) ...

claude-share --config alice.json capacity --window five_hour
# -> guaranteed_units=4000  (5000 base - 1000 sent)
claude-share --config bob.json capacity --window five_hour
# -> guaranteed_units=6000  (5000 base + 1000 received)

# Alice can no longer spend the 1000 she gave away - this is rejected:
claude-share --config alice.json consume --window five_hour --amount 4500 --idempotency-key try-1
```

**SHARED — conditional, revocable, owner keeps priority:**

```bash
claude-share --config bob.json request --from alice-id \
    --window five_hour --amount 2000 --type shared
claude-share --config alice.json request approve --request-id req-id-2

# Bob spends his own 5000 first, then draws the rest from Alice's shared grant.
claude-share --config bob.json consume --window five_hour --amount 5000 --idempotency-key bob-own
claude-share --config bob.json consume --window five_hour --amount 300 --idempotency-key bob-shared
# -> Consumed 300 unit(s) ..., 300 of which drawn from SHARED grant(s). ...

# Alice always gets served first: if she then spends enough of her own
# capacity, Bob's next shared draw may find less (or nothing) available -
# regardless of the grant's original amount.

# Alice can revoke early at any time; Bob's access disappears immediately.
claude-share --config alice.json grant revoke --grant-id grant-id-2
```

See [docs/architecture.md](docs/architecture.md) for the full SOLID/SHARED
accounting rules, owner-priority mechanics, and the grant expiration policy.

### Claude Code integration (`hook install` / `hook uninstall`)

```bash
claude-share hook install [--project | --user]
claude-share hook uninstall [--project | --user]
```

Installs (or removes) **two** Claude Code hooks in settings.json — a
`UserPromptSubmit` pre-check and a `Stop` post-turn metering hook.
`--project` targets `./.claude/settings.json` (the default; good for a
shared, git-committed setup), `--user` targets `~/.claude/settings.json`
(this machine only). Both commands merge into whatever is already there:
other hooks and settings are left untouched, and running `install` twice
never creates duplicate entries.

**Before each prompt** (`UserPromptSubmit` → `claude-share-hook`): checks
whether the joined identity has any guaranteed FIVE_HOUR capacity
remaining. No capacity → prompt blocked; running low → allowed with a
warning; otherwise silent:

```
Claude Share
Allocation exhausted.
Used: 25.0% / 25%
Reset: 1h 32m
```

**After each turn** (`Stop` → `claude-share-stop-hook`): reads Claude
Code's reported input/output token counts from the session transcript,
converts them to abstract quota units, and calls `consume()`. Failures
to read token data consume zero units (fail-open) — never a guessed
fallback.

A machine with no `login`/`join` configured is completely unaffected —
both hooks are strictly opt-in. See
[docs/architecture.md](docs/architecture.md) for the two-hook design,
the verified token data source, the weighting formula, idempotency, and
the honest caveat that this is measured token-count metering from Claude
Code, not Anthropic's internal billing ledger.

**End-to-end setup, from zero:**

```bash
cd claude-share && pip install -e .

claude-share pool create --name "Family Plan" --members "Alice,Bob"
# -> Alice: member_id=alice-id user_id=alice-user-id

claude-share login --user-id alice-user-id --device-name "Alice's Laptop"
claude-share join  --pool pool-id --member alice-id

cd /path/to/some/project   # wherever you use Claude Code
claude-share hook install --project
# -> Installed the Claude Share UserPromptSubmit and Stop hooks into .../.claude/settings.json.
```

From here, every prompt submitted in that project through Claude Code (or
VS Code's Claude Code extension, which uses the same hook mechanism) is
checked against Alice's guaranteed quota automatically - no per-prompt
action needed.

## Central server (Milestone 5)

**Purely local usage needs none of this.** The server exists so multiple
devices can share one authoritative copy of pool/quota/capacity state over
HTTP instead of each keeping an independent local SQLite database.

> **Do not expose this server on the public internet in cleartext.** Bearer
> tokens and quota data travel in HTTP bodies; a public `http://<ip>:8001`
> deployment is unsafe. `claude-share-server` does **not** terminate TLS
> itself. For this project's deployment, use **Tailscale** so only trusted
> devices on your tailnet can reach the server (encrypted mesh, no public
> port required). Follow **[docs/TAILSCALE_SETUP.md](docs/TAILSCALE_SETUP.md)**
> end to end — install on the Oracle Cloud server and each Windows client,
> point `--server`/extension/dashboard URLs at the Tailscale address, then
> close the public Security List / iptables rule for port 8001.

### 1. Set up Postgres

Any reachable PostgreSQL 13+ server works. Create an empty database for
claude-share to use:

```bash
createdb claude_share
# or: psql -c "CREATE DATABASE claude_share;"
```

### 2. Run the server

```bash
pip install -e ".[server]"

export CLAUDE_SHARE_DATABASE_URL="postgresql://user:password@localhost:5432/claude_share"
export CLAUDE_SHARE_SERVER_HOST="0.0.0.0"   # default: 127.0.0.1
export CLAUDE_SHARE_SERVER_PORT="8000"      # default: 8000

claude-share-server
```

This creates the Postgres schema if it doesn't already exist (safe to run
repeatedly) and starts a FastAPI app under `uvicorn`. Interactive API docs
are available at `/docs` while it's running.

### 3. Point a device at it

`pool create` can bootstrap a pool directly on a server, before anyone has
an identity yet — `--server` is a global flag (like `--db`/`--config`) and
must come before the subcommand:

```bash
claude-share --server http://100.x.y.z:8001 \
    pool create --name "Family Plan" --members "Alice,Bob"
# -> Alice: member_id=... user_id=...
# -> Bob:   member_id=... user_id=...
```

`login --server <url>` registers this machine as a new device against
that server (minting a bearer token, stored in the local identity config
file — see `--config` above) instead of the local SQLite database. From
`join` onward, every command works exactly like the local flow — same
subcommands, same flags, same output shapes — just talking HTTP instead of
opening a local database file:

```bash
claude-share --config alice.json login --server http://100.x.y.z:8001 \
    --user-id <alice_user_id> --device-name "Alice's Laptop"
claude-share --config alice.json join --pool <pool_id> --member <alice_member_id>

claude-share --config alice.json status
claude-share --config alice.json consume --window five_hour --amount 10 --idempotency-key req-001
claude-share --config alice.json request --from <bob_member_id> --window five_hour --amount 500 --type shared
claude-share --config alice.json capacity --window five_hour
```

A machine that has never run `login --server` is completely unaffected —
`--db`/`--config` with no `--server` behaves exactly as in Milestones 1-4,
using local SQLite with zero server involvement. A machine can be pointed
at local SQLite *or* a server, never both at once, per `--config` file
(one `LocalIdentity` = one mode) — use separate `--config` paths to run
both modes side by side on the same machine.

**Deploying on a real server?** See
[docs/TAILSCALE_SETUP.md](docs/TAILSCALE_SETUP.md) for Tailscale setup
(server + Windows clients), migrating off a public IP, and closing public
port exposure.

### Auth model, briefly

Every request except pool creation, device registration, and reading a
pool's member list must carry a valid `Authorization: Bearer <token>` from
a prior `login --server`/device registration; the server always resolves
that token back to the device's own `user_id` and rejects any attempt to
act as a `member_id` that `user_id` doesn't own (403), regardless of what
the request body claims. This is intentionally minimal (an opaque, hashed,
per-device token — no OAuth/JWT/expiry/scopes) for a small, cooperative,
already-trusted group; see docs/architecture.md for the full design and
why it's sufficient here.

## Running tests

```bash
pip install -e ".[dev]"
pytest
```

The Milestone 5 Postgres/server tests
(`tests/test_postgres_unit_of_work.py`, `tests/test_server_e2e_postgres.py`)
need a reachable Postgres server able to `CREATE`/`DROP DATABASE` — they
default to `postgresql://postgres:postgres@localhost:5432/postgres` and
create/drop their own short-lived throwaway databases per test run. Point
them elsewhere with:

```bash
export CLAUDE_SHARE_TEST_POSTGRES_ADMIN_DSN="postgresql://user:password@host:5432/postgres"
pytest
```

If no Postgres is reachable at that DSN, those two files are skipped (with
a message naming the DSN they tried) and the rest of the suite — including
`tests/test_server_routes.py`'s much larger set of HTTP-layer tests, which
run against SQLite and need no external service — runs normally.

## Project layout

```
src/claude_share/
  domain/          entities, value objects, allocation/capacity math, repository ports
  application/     use-case orchestration (QuotaService, CapacityService, AgentService) + DTOs
                   tokens.py: device API token generation/hashing (Milestone 5)
  infrastructure/
    sqlite/        SQLite implementation of the repository ports (local-only mode)
    postgres/      Postgres implementation of the same ports (Milestone 5, central server mode)
  agent/           local identity/session layer: config file + login/join/status logic
                   remote_client.py: HTTP counterpart to QuotaService/CapacityService/AgentService (Milestone 5)
  integrations/
    claude_code/   UserPromptSubmit hook (hook.py) + settings.json installer (settings.py)
  server/          Milestone 5: FastAPI app exposing the application services over HTTP
  cli/             argparse CLI, translates args <-> service/agent/integrations calls
                   (picks local-SQLite or remote-HTTP services per the loaded identity)
tests/
docs/architecture.md   layering rationale and documented assumptions
```

See [docs/architecture.md](docs/architecture.md) for the full design
rationale, including why the domain layer has zero SQLite/Postgres/Claude
dependencies, the SOLID/SHARED accounting and owner-priority mechanics,
the local identity config file's format/location, why `login` isn't real
authentication in local-only mode, the verified `UserPromptSubmit` hook
mechanism and its fail-open error policy, the Postgres locking/isolation
strategy and why it preserves Milestone 1's concurrency guarantees, the
device-token auth design, and every explicit assumption made along the
way.
