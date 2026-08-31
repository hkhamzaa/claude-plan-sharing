# Claude Share

A quota-management and fair-sharing system for a trusted group of people who
share one underlying Claude subscription.

**Milestones 1-4 are implemented.** Milestone 1 is the local quota
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
prompt and blocks/warns accordingly, installed via `hook install`. There
is still no real authentication and no networking/central server — see
[docs/architecture.md](docs/architecture.md) for what's in scope and
what's intentionally deferred.

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

Installs (or removes) a `UserPromptSubmit` hook in Claude Code's
settings.json - `--project` targets `./.claude/settings.json` (the
default; good for a shared, git-committed setup), `--user` targets
`~/.claude/settings.json` (this machine only). Both commands merge into
whatever is already there: other hooks and settings are left untouched,
and running `install` twice never creates a duplicate entry.

Before each prompt, the hook checks the joined identity's guaranteed
FIVE_HOUR quota: sufficient and not running low → silent; running low →
allowed through with a short warning; exhausted → the prompt is blocked
with a message like:

```
Claude Share
Allocation exhausted.
Used: 25.0% / 25%
Reset: 1h 32m
```

A machine with no `login`/`join` configured is completely unaffected -
the hook is strictly opt-in. See
[docs/architecture.md](docs/architecture.md) for the exact mechanism
(verified against Claude Code's hooks documentation), the fail-open error
policy, and why this checks *availability* rather than metering real
usage (there's no token-cost estimator yet - it's a fixed placeholder
cost per prompt, not real Claude usage).

**End-to-end setup, from zero:**

```bash
cd claude-share && pip install -e .

claude-share pool create --name "Family Plan" --members "Alice,Bob"
# -> Alice: member_id=alice-id user_id=alice-user-id

claude-share login --user-id alice-user-id --device-name "Alice's Laptop"
claude-share join  --pool pool-id --member alice-id

cd /path/to/some/project   # wherever you use Claude Code
claude-share hook install --project
# -> Installed the Claude Share UserPromptSubmit hook into .../.claude/settings.json.
```

From here, every prompt submitted in that project through Claude Code (or
VS Code's Claude Code extension, which uses the same hook mechanism) is
checked against Alice's guaranteed quota automatically - no per-prompt
action needed.

## Running tests

```bash
pip install -e ".[dev]"
pytest
```

## Project layout

```
src/claude_share/
  domain/          entities, value objects, allocation/capacity math, repository ports
  application/     use-case orchestration (QuotaService, CapacityService, AgentService) + DTOs
  infrastructure/  SQLite implementation of the repository ports
  agent/           local identity/session layer: config file + login/join/status logic
  integrations/
    claude_code/   UserPromptSubmit hook (hook.py) + settings.json installer (settings.py)
  cli/             argparse CLI, translates args <-> service/agent/integrations calls
tests/
docs/architecture.md   layering rationale and documented assumptions
```

See [docs/architecture.md](docs/architecture.md) for the full design
rationale, including why the domain layer has zero SQLite or Claude
dependencies, the SOLID/SHARED accounting and owner-priority mechanics,
the local identity config file's format/location, why `login` isn't real
authentication yet, the verified `UserPromptSubmit` hook mechanism and
its fail-open error policy, and every explicit assumption made along the
way.
