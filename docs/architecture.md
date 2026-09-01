# Architecture — Local Quota Engine + Capacity Delegation + Local Agent + Claude Code Integration

Covers Milestone 1 (local quota engine: pools, members, allocation,
windows, idempotent consume), Milestone 2 (SOLID/SHARED capacity
delegation between members of a pool), Milestone 3 (local identity/device
layer), and Milestone 4 (a Claude Code `UserPromptSubmit` hook).
Milestone 8 adds real token-count-based metering via a companion `Stop`
hook. Milestone-specific sections are labeled below.

## Layering

```
domain/          <- pure business rules. No I/O, no SQLite, no Claude.
application/     <- use cases (QuotaService, CapacityService, AgentService), orchestrates domain + repository ports
infrastructure/  <- concrete adapters: SQLite implementation of the domain's repository ports
agent/           <- local identity/session layer: a JSON config file + login/join/status logic (Milestone 3)
integrations/    <- adapters to external tools (Claude Code hook + settings.json installer, Milestone 4)
cli/             <- argparse front-end, calls application/, agent/, and integrations/ only
```

Dependencies only point inward: `cli` depends on `application`, `application`
depends on `domain`, and `infrastructure` depends on `domain` (to implement
its ports) and is depended *on* only at the composition-root level (`cli/main.py`,
which is the one place that wires a concrete `SqliteUnitOfWork` into a
`QuotaService`). `domain` depends on nothing else in this codebase.

### Why the domain layer has no Claude-specific dependencies

The end goal for this project spans multiple future subscription providers'
worth of ambiguity risk: token-based limits, message-based limits, whatever
Anthropic's usage model looks like next quarter. If `QuotaWindow` or
`Allocation` knew about tokens or prompts, every change to Claude's usage
model would ripple into the core fairness/allocation logic that has nothing
to do with Claude specifically. Instead, the domain only ever manipulates
abstract integer "quota units" (see **Assumptions** below). A future
`ClaudeUsageProvider` (planned, not built here) will be the only thing that
knows how to translate real Claude usage into these units — it will live in
`infrastructure/` or a new `adapters/` package, never in `domain/`.

### Why the domain layer has no SQLite-specific dependencies

`domain/repository.py` defines abstract ports (`PoolRepository`,
`MemberRepository`, `AllocationRepository`, `QuotaWindowRepository`,
`UsageRecordRepository`, `CapacityRequestRepository`,
`CapacityGrantRepository`, `SharedConsumptionRecordRepository`,
`UnitOfWork`) using only domain types in their signatures — no
`sqlite3.Connection`, no `sqlite3.Row`, no SQL strings. `infrastructure/sqlite/`
is the only place that imports the `sqlite3` module; it translates between
rows and the plain dataclasses in `domain/models.py` at the repository
boundary. `QuotaService` and `CapacityService` both take a
`uow_factory: Callable[[], UnitOfWork]` — neither imports anything from
`infrastructure/` directly. This means a future Postgres-backed
implementation (for the multi-device central server) can implement the same
`UnitOfWork`/repository ports and be swapped in at the composition root
without touching `domain/` or `application/`.

### Why consume() uses a Unit of Work instead of one clever SQL statement

`consume()` needs to (1) check idempotency, (2) check remaining balance,
and (3) write updated usage — all atomically, with no other transaction able
to interleave a conflicting write in between. `SqliteUnitOfWork`
(`infrastructure/sqlite/unit_of_work.py`) opens a fresh SQLite connection per
`with` block and immediately issues `BEGIN IMMEDIATE`, which acquires
SQLite's RESERVED lock at the start of the transaction rather than at the
first write. That closes the classic read-modify-write race: once a
`consume()` call has started its transaction, no other connection can begin
a competing write transaction against the same database file until this one
commits or rolls back. A transaction that isn't explicitly committed (an
exception, an early `return` after a rejection) is rolled back in `__exit__`,
so a rejected or failed `consume()` is guaranteed to leave stored state
byte-for-byte unchanged. Business rules (what counts as "insufficient",
what an idempotent replay returns) stay in `QuotaService` and
`QuotaWindow.consume()` — the SQLite layer only provides the transactional
envelope, not the domain logic.

### Reads also serialize, on purpose (for now)

`check_quota()` and `get_status()` are read-only, but they run through the
same `with self._uow_factory() as uow:` path as `consume()`, which means
they too open a `BEGIN IMMEDIATE` transaction and take SQLite's RESERVED
lock for their duration. In other words, *every* operation — not just
writes — fully serializes against every other operation on the database;
two `get_status()` calls cannot even run concurrently with each other.
This is a deliberate safety-over-throughput tradeoff for a trusted,
small-pool MVP: it's the simplest possible concurrency model, it's
trivially correct, and at this scale (a handful of members, occasional
CLI invocations) the serialization cost is unmeasurable. It is not the
right tradeoff once a central server introduces real concurrent load
(many members, many simultaneous readers) — at that point reads should
move to a separate deferred/no-lock transaction mode (or a WAL-mode
snapshot read) so they stop blocking writers and each other. Revisit this
when the central server milestone is built; it isn't addressed here.

## Assumptions made explicit in this milestone

These are deliberate placeholder decisions, called out so they're easy to
find and revisit later — none of them are implied by the prompt as
permanent design choices.

1. **Fixed-duration windows.** `reset_at` is computed once, at member
   creation, as `window_start + 5h` (FIVE_HOUR) or `window_start + 7d`
   (WEEKLY) (`domain/models.py`: `FIVE_HOUR_WINDOW_DURATION`,
   `WEEKLY_WINDOW_DURATION`). There is no real Claude usage provider yet to
   report actual window boundaries, and this milestone does not implement
   any rollover/reset scheduling — usage simply accumulates against the
   window created at pool-creation time. A future `UsageWindowProvider`
   is expected to replace this fixed-duration placeholder and to own
   rolling/resetting windows over time.

2. **Quota units are entirely abstract.** Nothing in `domain/` or
   `application/` converts units to/from tokens, requests, or dollars. An
   "amount" passed to `consume()` is just a positive integer the caller
   asserts represents that much normalized usage.

3. **Basis points double as per-window unit capacity.** `Allocation.bps`
   (0–10,000, summing to exactly 10,000 across a pool's members) is used
   directly as `QuotaWindow.allocation_units` for *both* the FIVE_HOUR and
   WEEKLY windows. In other words, this milestone treats each window as
   having a nominal total capacity of 10,000 abstract units, split by the
   same bps share the member holds for that pool. There is no
   separate "pool has N total units per window" input, because none was
   specified in scope for this milestone (no Claude usage numbers exist
   yet to size it against). This keeps a member's fairness share identical
   and consistent across both window granularities. When a real
   `ClaudeUsageProvider` is introduced, it will likely need to scale
   `allocation_units` by an actual provider-reported capacity per window;
   that scaling factor does not exist yet and is out of scope here.

4. **One `Allocation` per member, not per (member, window type).** Since a
   member's fairness share is the same percentage regardless of window
   granularity, `Allocation` is stored once per member and used to seed
   both windows' `allocation_units` at creation time. Both windows still
   persist and track their own `allocation_units`/`usage_units`
   independently (see `quota_windows` table), so this is a
   data-normalization choice, not a behavioral coupling — consuming in one
   window never touches the other.

5. **`user_id` is a generated placeholder.** `create_pool` has no external
   identity provider to draw real user ids from in this milestone, so each
   `Member.user_id` is a freshly generated UUID distinct from `Member.id`.
   A future auth/identity milestone will presumably replace this with a
   real external user id.

## Persistence model (Milestone 1)

SQLite tables (`infrastructure/sqlite/schema.py`): `pools`, `members`,
`allocations`, `quota_windows` (primary key `(member_id, window_type)`), and
`usage_records` (`idempotency_key` has a `UNIQUE` constraint, which is the
first line of idempotency defense; the application layer also explicitly
checks for and handles a pre-existing record with the same key before
attempting an insert, so a repeat call returns a normal `ConsumeResult`
rather than surfacing a constraint-violation error).

## Deliberately out of scope for Milestone 1

Per the Milestone 1 brief: no Claude API/Claude Code integration, no
networking or HTTP, no browser extension, no central server/Postgres, no
OAuth/Anthropic auth, no SOLID/SHARED capacity delegation, no
requests/approvals/grants, no notifications, no dashboard, no
multi-device/multi-session concerns. The `UnitOfWork`/repository
abstraction and the "abstract quota units" domain model exist specifically
so those features can be layered on later without reshaping this
milestone's core.

---

## Milestone 2 — SOLID / SHARED capacity delegation

### The lifecycle

```
request_capacity()  -> CapacityRequest (PENDING)      no capacity moves yet
approve_request()   -> CapacityGrant (ACTIVE)          atomic check + create
reject_request()    -> CapacityRequest (REJECTED)
  consume()          reads ACTIVE grants each call      grant-aware admission
revoke_grant()      -> CapacityGrant (REVOKED)          effective immediately
  (expiry)           -> CapacityGrant (EXPIRED)          lazy, on next access
```

`requester_member_id` is who would receive capacity; `target_member_id` is
who owns it and must approve or reject. `CapacityService`
(`application/capacity_service.py`) implements all five Milestone 2
methods, following the same `uow_factory` injection pattern as
`QuotaService`, and every method runs inside one `BEGIN IMMEDIATE`
transaction (see above) — so, exactly like `consume()` in Milestone 1, a
rejected `approve_request()` leaves the request PENDING and creates no
grant, and two concurrent `approve_request()` calls against the same
source's capacity fully serialize rather than racing.

### Base allocation vs. guaranteed vs. potential capacity

Milestone 1's `QuotaWindow.allocation_units` (the *base* allocation) is
never mutated by a grant. Grants are a pure overlay, computed fresh from
currently-ACTIVE grant rows every time they're needed:

- **guaranteed_units** = `base_allocation - solid_sent + solid_received`.
  This is a real ceiling — `consume()` enforces it directly as the
  member's own admission limit (`domain/capacity.py:compute_guaranteed_units`).
- **potential_units** = `guaranteed_units + shared_borrowed_potential`.
  This is an *upper bound*, not a promise: how much of it is actually
  drawable depends on the SHARED grants' sources' own usage at the moment
  `consume()` is called, which can change between when you check
  `get_effective_capacity()` and when you actually try to spend it.

`get_status()` (Milestone 1) deliberately still reports only base
allocation/usage/remaining, unchanged — it is not grant-aware. Grant-aware
figures live exclusively in `CapacityService.get_effective_capacity()`.
Keeping these separate means Milestone 1's existing behavior and tests are
untouched, and callers who want the grant-aware view have one obvious
place to get the complete picture (base, solid_sent, solid_received,
guaranteed, shared_offered, shared_borrowed_potential, potential) rather
than guessing which of several overloaded numbers they're looking at.
`ConsumeResult.allocation_units`/`remaining_units` **are** grant-aware
(they report the member's own guaranteed ceiling, since that's what
`consume()` actually checked against) — for a member with no SOLID grants
this is numerically identical to Milestone 1's base allocation, so nothing
about Milestone 1's own test expectations changes.

**Bug fixed during Milestone 3 doc verification**: `QuotaWindow.__post_init__`
originally rejected `usage_units > allocation_units` as an invariant
violation. That was correct for Milestone 1 (no grants exist, so
`allocation_units` really was the ceiling), but became wrong the moment a
SOLID recipient's `guaranteed_units` could exceed their own window's
`allocation_units` (base): `consume()` correctly computed the larger
guaranteed ceiling and tried to admit the call, but the resulting
`QuotaWindow` construction raised `ValueError` anyway, since the entity
itself was still enforcing the old, now-incorrect assumption. This wasn't
caught by the Milestone 2 test suite because no test exercised "a SOLID
recipient consumes an amount strictly between their own base allocation
and their (larger) guaranteed ceiling, without needing any SHARED draw" -
every SOLID test either stayed within the recipient's own base or only
checked the *source's* reduced ceiling. Found by manually re-running this
document's own SHARED walkthrough end-to-end (Bob, a SOLID recipient with
guaranteed=6000 against a base of 5000, consuming 5300 total). Fixed by
removing the invariant from `QuotaWindow.__post_init__` - `usage_units` is
no longer validated against `allocation_units` there, since that
comparison is a Milestone-1-only special case of the real, grant-aware
ceiling the application layer already computes. A regression test
(`tests/test_capacity_service.py::test_solid_recipient_can_consume_beyond_own_base_allocation`)
now covers exactly this path. One side effect worth knowing: `get_status()`'s
`WindowStatus.remaining_units` (`allocation_units - usage_units`, base-only
by design - see above) can now display as a negative number for a member
who has both received SOLID capacity and used more than their own base;
that's expected given `get_status()`'s deliberately base-only semantics,
not a bug - `get_effective_capacity()` is the place to see the real,
always-non-negative picture.

### SOLID vs SHARED accounting at approve time

Both checks happen in `CapacityService.approve_request()`, using
`application/capacity_queries.py:member_grant_summary()` to fetch and sum
the source's currently-active grants:

- **SOLID**: `base_allocation - solid_sent >= amount`, where `solid_sent`
  only sums the source's other active SOLID grants.
- **SHARED**: `shared_offered + amount <= base_allocation`, where
  `shared_offered` only sums the source's other active SHARED grants.

These two checks are independent of each other by design, matching the
spec text literally: a SOLID check never looks at SHARED grants and vice
versa. This means, in principle, a source could have sent away 100% of
their base via SOLID and still have an approved SHARED offer sitting on
top of it. That is not a bug: at `consume()` time, the *real* ceiling for
any SHARED draw is the source's live `guaranteed_units - usage_units`,
which already reflects SOLID commitments. If a source's guaranteed
capacity is fully committed via SOLID, any SHARED recipient's draw
against them simply finds `source_available == 0` regardless of what was
"offered" on paper. The approve-time checks exist to catch the more
common mistake (over-promising within the same type across several
requests); the consume-time check is what actually prevents overselling
in every case.

### Owner priority mechanics

"Source always has priority" is not a special-cased rule — it falls out
of how `consume()` computes availability. When a SHARED recipient's own
guaranteed capacity can't cover a `consume()` call, the shortfall is drawn
from their active SHARED grants (oldest `created_at` first), and for each
grant the amount available to draw is:

```
min(
    grant.amount - <lifetime amount already drawn from this specific grant>,
    source.guaranteed_units - source.usage_units,   # source's LIVE remaining balance
    <remaining shortfall to cover>,
)
```

The middle term is read fresh, inside the same transaction, at the moment
of the recipient's `consume()` call — never a cached or stale snapshot.
So if the source has already spent their own capacity (via their own
prior, already-committed `consume()` calls), that shows up immediately as
a reduced or zero `source_available` for anyone trying to borrow from
them. There is no reservation system and no advance notice: the source is
simply never blocked by a SHARED grant, because the grant's availability
is *defined* in terms of what the source hasn't used yet. The whole
check-and-draw (recomputing every source's live balance, capping by each
grant's lifetime ceiling, and writing the result) happens inside one
`BEGIN IMMEDIATE` transaction per `consume()` call, so a source's own
concurrent `consume()` cannot interleave mid-calculation with a
recipient's draw against them (`tests/test_capacity_service.py::test_shared_concurrent_consumption_never_oversells_source`
exercises this directly with real threads).

A `consume()` call that needs to draw from more than one SHARED grant (or
from a SHARED grant plus its own guaranteed capacity) is still all-or-nothing:
if the total available across own-guaranteed-capacity plus every eligible
SHARED grant can't fully cover `amount`, the whole call is rejected and no
partial draw happens anywhere (mirrors Milestone 1's no-partial-consumption
guarantee).

**A SHARED grant's `amount` is a lifetime ceiling for that specific grant**,
not a per-call or per-window-reset ceiling: it's enforced by summing all
`SharedConsumptionRecord` rows ever created against that `grant_id`
(`application/capacity_queries.py:grant_lifetime_drawn`). Since this
milestone's windows don't roll over (see Milestone 1 assumption #1), "for
the life of the grant" and "for the life of the window" are the same
period in practice here.

### The SharedConsumptionRecord audit trail

When a `consume()` call draws from one or more SHARED grants, each draw is
recorded as a `SharedConsumptionRecord` (`id`, `usage_record_id`,
`grant_id`, `amount`, `timestamp`), linking the recipient's `UsageRecord`
(which always records the *full* `amount` the recipient successfully
consumed) to every grant that contributed to it and how much each
contributed. This is what lets `consume()` be honest about where capacity
actually came from: **a SHARED draw increments the *source's* own
`QuotaWindow.usage_units`, not the recipient's** — the recipient's own
`usage_units` only grows by the portion covered by their own guaranteed
capacity. This is also what makes owner priority "just work": since a
shared draw is booked as real usage against the source's own window, the
source's own future `consume()` calls see reduced availability exactly as
if they'd spent it themselves — because, from the ledger's point of view,
that capacity really has been spent, just on the recipient's behalf.
`CapacityService`/`QuotaService` never expose a separate "shared usage"
counter on `QuotaWindow` — the audit trail *is* `SharedConsumptionRecord`,
queryable by `usage_record_id` (what did this one consume() call draw?)
or by `grant_id` (how much has ever been drawn against this grant? — used
to enforce the lifetime ceiling above).

An idempotent replay of a `consume()` call that originally drew from
SHARED grants re-reads its original `SharedConsumptionRecord` rows (by
`usage_record_id`) rather than recomputing anything, so `shared_draws`/
`shared_units_used` on the replayed `ConsumeResult` are identical to the
original call's — consistent with Milestone 1's idempotency guarantee
that a replay "returns the same result both times."

### Grant expiration policy

Chosen: **lazy expiration, checked against wall-clock time on access, not
a background sweep.** There is no daemon or scheduled job in this
milestone (none is in scope - see below). Instead,
`application/capacity_queries.py:active_grants_as_of()` is the single
place that decides whether a grant currently counts: a grant with
`status == ACTIVE` but `expires_at <= now` is treated as inactive for
the current computation *and* is persisted as `EXPIRED` as a side effect,
right there in the same transaction. Every code path that needs "this
member's active grants" (`consume()`, `approve_request()`'s over-commit
checks, `get_effective_capacity()`) goes through this one function, so
expiration is applied consistently everywhere and a grant's stored status
eventually reflects reality the next time anything touches it — without
needing a separate always-running process. The tradeoff: a grant that is
never touched again after expiring will sit in storage with `status = ACTIVE`
forever (harmless, since nothing ever treats it as usable once
`expires_at` has passed, but slightly stale bookkeeping). A future
milestone introducing a background agent could add an explicit sweep for
tidiness; it isn't needed for correctness here.

**Default expiration policy**: `approve_request()` sets a new grant's
`expires_at` to the *source's* `QuotaWindow.reset_at` for that window
type at approval time (i.e., "this grant lives until the source's current
window ends"). Since all members of a pool get their windows created at
the same instant in `create_pool()` (Milestone 1), using the source's vs.
the recipient's window makes no practical difference today, but the
source was chosen as the canonical reference since the capacity being
delegated is denominated in the source's own window.

### Open questions / deliberate simplifications (flagged, not resolved here)

- `CapacityRequest.status` includes `EXPIRED`, `CANCELLED`, and
  `COMPLETED` per the spec's enum, but no method in this milestone's
  scope (`request_capacity`, `approve_request`, `reject_request`)
  transitions a request into any of those three states — only
  `PENDING -> APPROVED` and `PENDING -> REJECTED` are exercised. They're
  left defined for forward compatibility (e.g. a future request-level
  expiry or a cancel-by-requester action) rather than removed.
- `CapacityRequest.expires_at` (request-level, as opposed to
  `CapacityGrant.expires_at`) is accepted by the dataclass but never set
  or read by any Milestone 2 method — same reasoning as above.
- `EffectiveCapacity.shared_borrowed_potential` sums the full `amount` of
  every active SHARED grant a member holds as recipient, matching the
  spec's literal wording ("sum of amounts in active SHARED grants ...
  this is a ceiling, NOT a guarantee"). It is **not** reduced by amounts
  already drawn historically against those grants (unlike the real
  `consume()`-time check, which does apply the lifetime cap). Treat
  `potential_units` as a coarse, optimistic upper bound, not an accurate
  "what's left" figure.
- The SOLID approval check ("base allocation minus other active SOLID
  grants") does not subtract the source's own current `usage_units`. In
  the (rare) case where a source has already spent heavily against their
  own window before sending capacity away via SOLID, their
  `guaranteed_units` can drop below what they've already used. `consume()`
  handles this safely by clamping `own_available` at `max(guaranteed - usage, 0)`
  rather than going negative — the practical effect is just that the
  source has zero remaining until the window resets or the grant is
  revoked, with no other side effect.
- CLI `request --from/--to` mapping: `--from` is the capacity owner who
  must approve (`target_member_id`), `--to` is the requester/eventual
  recipient (`requester_member_id`) — read as "request capacity *from*
  the owner, *to* the requester." The spec's CLI sketch didn't say which
  way round these map; this reading was chosen as the more natural
  sentence for whoever runs the command (typically the person who wants
  more capacity, asking the owner for it).

## Deliberately out of scope for Milestone 2

Per the Milestone 2 brief: no local agent/background daemon, no Claude
Code integration or hooks, no networking/HTTP/central server, no browser
extension, no notifications, no "request from anyone"/multi-contributor
convenience feature, no minimum-reserve settings, no dashboard beyond the
CLI. In particular, the lack of a background daemon is why grant
expiration is handled lazily (above) rather than via a scheduled sweep.

---

## Milestone 3 — Local Agent (identity/device layer)

### What problem this solves

Milestones 1-2 require every caller to know and pass a raw `member_id` on
every single call. That's fine for a test harness or for one person
scripting against their own pool, but it's a poor foundation for the
Claude Code hook (Milestone 4) or a real CLI user: nobody wants to paste
a UUID into every command. Milestone 3 adds a thin layer that answers
"which user, on which device, acting as which pool member, is running
this command right now" - once, at `login`/`join` time - so everything
above it (this CLI today, the Claude Code hook tomorrow) can omit
`member_id`/`pool_id` and let it default.

Critically, this layer changes **nothing** about how quota is computed or
stored. `Device` is bookkeeping only: a member_id already uniquely
identifies one quota ledger in Milestones 1-2, and it still does. Two
devices logged in as the same `user_id` and joined to the same
`pool_id`/`member_id` already share one quota ledger today, for free -
this milestone just makes "which member_id am I" persistent and explicit
per machine, instead of requiring it to be retyped every time.

### Why local identity is a JSON file, not a domain entity/table

The local identity ("this machine currently acts as pool P, member M,
under user U, as device D") describes *this one machine's* CLI session
state - it is not shared pool/quota data, and a second device for the
same `user_id` has its own, completely independent choice of which
pool/member it's currently pointed at (a `user_id` can belong to members
in more than one pool). Putting it in the shared SQLite database would
imply it's global multi-device state that needs to be looked up and
reconciled, which is exactly the wrong model - it's local, single-machine
configuration, closer in spirit to a kubeconfig or a `.git/config` than
to application data. A flat JSON file is the simplest structure that
satisfies "persist this between CLI invocations," and needs no schema
migration story of its own.

**Location and format**: `~/.claude-share/config.json` by default
(mirrors the SQLite DB default path, `~/.claude-share/claude_share.db`),
overridable via `--config <path>` on any CLI command or the
`CLAUDE_SHARE_CONFIG` environment variable - the same override pattern
already used for `--db`/`CLAUDE_SHARE_DB`. Every CLI test in this
repository passes an explicit `--config` pointing at a temp file, so
tests never read or write the real machine's config. The file is plain,
uncompressed JSON with exactly five top-level string-or-null keys,
matching `agent.identity.LocalIdentity` field-for-field:

```json
{
  "pool_id": "6f2b...",
  "member_id": "a91c...",
  "user_id": "3d4e...",
  "device_id": "77aa...",
  "device_name": "Alice's Laptop"
}
```

`pool_id`/`member_id` are `null` after `login()` but before `join_pool()`
- a device can be logged in as a user without yet having chosen which
pool/member it acts as. `agent/identity.py` (`load_local_identity`,
`save_local_identity`) is the only code that reads or writes this file;
`agent/commands.py` and the CLI never touch it directly. There is no file
locking or atomic-rename-on-write here: per the product's trust model
(cooperative, single-operator-per-machine), a torn write from a crash
mid-save is an acceptable, self-correcting failure mode (rerun
`login`/`join`), not something worth building infrastructure to prevent.

### Why "login" here is not real authentication

`agent.commands.login()` does exactly one thing: it looks up an
already-existing `user_id` (one that was minted for some `Member` by a
prior `create_pool()` call) and points this machine's local config at it.
There is no password, token, or credential of any kind, and nothing
verifies that the person typing the command is actually who they claim to
be - anyone who knows (or guesses) a valid `user_id` can "log in" as it.
This is intentional and matches the current state of the project, not an
oversight: there is no networking or central server until Milestone 5,
and the product's trust model is explicitly cooperative ("all members are
trusted... a user could disable software on their own machine - that's
acceptable"). Adding real credential-checking now would be security
theater with nothing behind it to actually protect, since anyone with
filesystem/CLI access already has unrestricted access to the same local
SQLite database `login` reads from. **This is a placeholder Milestone 5
will need to address**: once there's a central server and multiple
untrusted-by-default devices talking to it over a network, `login` needs
to become real authentication (a credential exchange, a token, something
verifiable) rather than "type in a UUID you already know." Until then,
`login` is best read as "configure," not "authenticate."

### Explicit CLI args vs. configured local identity: explicit always wins

`status`, `consume`, `capacity`, and `request`/`grant revoke` all now
accept their identity-related flags (`--member`, `--pool`, `--to`, `--by`)
as optional. When omitted, the CLI loads the local identity file (if any)
and fills in the corresponding field; when given explicitly, the argument
is used as-is and the local identity is never consulted for that field
(`cli/main.py:_resolve_member_id`/`_resolve_pool_id`). This means:

- A machine with no local identity configured must pass every id
  explicitly, exactly like Milestones 1-2 - nothing about that path
  changed, satisfying "purely additive."
- A machine that has run `login`+`join` can omit `--member`/`--pool` on
  everyday commands (`status`, `consume`).
- Anyone acting on behalf of a different identity than their own local
  one (e.g. a pool owner approving someone else's request, or a test)
  can still always pass the id explicitly, which overrides the local
  identity for that call - there is no "explicit vs. implicit" ambiguity
  or precedence surprise, explicit input is simply never overridden.

`request`'s `--from` (the capacity owner being asked) and `request
approve/reject`/`grant revoke`'s `--request-id`/`--grant-id` have no
sensible default and remain required in every case - only the fields that
plausibly mean "me" (`--to`, `--by`, `--member`, `--pool`) default from
local identity.

### `join_pool` validates both pool membership and user ownership

`join_pool(pool_id, member_id)` checks two things before persisting: that
`member_id` actually belongs to `pool_id` (`MemberNotInPoolError` if not),
and that `member_id` belongs to the `user_id` this machine already logged
in as (`MemberNotOwnedByUserError` if not). Both are ordinary input
validation, not a trust-model/anti-tampering mechanism - the same
category as any other "does this id actually refer to what you think it
does" check in this codebase. The concrete case it catches is an honest
mistake: a typo'd or copy-pasted `member_id` that happens to be valid but
points at a different person's identity, silently pointing this machine
at the wrong member. A failed check leaves the existing local identity
untouched (same atomicity as every other check in this function).

### Why `user_id` printing was added to `pool create`'s output

Milestones 1-2's `pool create` output only ever printed `member_id`. But
`login` needs a `user_id`, and until now nothing surfaced one - `Member.user_id`
was always a real, accessible field on the domain object, just never
printed by the CLI. `_cmd_pool_create` now prints `user_id=...` alongside
`member_id=...` on the same line (e.g. `Alice: member_id=... user_id=...`).
This is an additive change to CLI *output* only: it doesn't change
`create_pool()`'s behavior, return value, or existing arguments, and the
existing member_id parsing pattern (`member_id=(\S+)`, stopping at the
first whitespace) still matches correctly with the extra trailing text.
Without this, there would be no way to actually exercise `login` from the
CLI, since a user_id is randomly generated internally and would otherwise
be invisible to whoever is supposed to log in with it.

## Deliberately out of scope for Milestone 3

Per the Milestone 3 brief: no networking or HTTP client/server, no Claude
Code integration or hooks, no real authentication/credentials/tokens, no
browser extension, no multi-device conflict resolution or device
revocation, no central server, nothing from Milestones 6+. `Device` is
identity/bookkeeping only - it introduces no new quota-resolution logic
beyond what `member_id` already provided in Milestones 1-2.

---

## Milestone 4 — Claude Code Integration (`UserPromptSubmit` hook)

### The verified integration mechanism

This milestone was scoped against a specific, pre-confirmed mechanism
(confirmed against Anthropic's current official Claude Code hooks
documentation for this project - not assumed or inferred): Claude Code
supports a `UserPromptSubmit` "command" hook, configured in
`.claude/settings.json` (project) or `~/.claude/settings.json` (user),
that fires once per submitted prompt, before Claude processes it. The
hook process:

- receives a JSON payload on stdin (documented fields include at least
  `session_id`, `cwd`, `hook_event_name`, and the prompt text)
- signals its decision by exit code, with stdout/stderr carrying the
  message: exit 2 blocks the prompt (reason shown from stderr text, or a
  `decision: "block"` JSON object - this implementation uses plain
  stderr, not JSON); exit 0 allows it through, and any plain stdout text
  printed on exit 0 is added as context alongside the prompt
- has a default timeout of 30 seconds for this specific event

`src/claude_share/integrations/claude_code/hook.py` implements exactly
this contract and nothing else - no `PreToolUse`, no MCP-based
enforcement, no other event.

**What was inferred, not verified, and where**: the exact JSON shape of
the `hooks` key inside `settings.json` (a per-event list of
`{"hooks": [{"type": "command", "command": "..."}]}` groups, optionally
carrying a `"matcher"` for tool-scoped events) was *not* part of what was
independently re-confirmed for this milestone - it's implemented from
general knowledge of Claude Code's documented hooks configuration format,
the same shape used for other hook events. If this format has since
changed, `integrations/claude_code/settings.py` is the one place to fix
it. Separately, this implementation assumes Claude Code resolves a hook's
`"command"` value the way a shell resolves any command name (i.e. via
`PATH`), which is why `hook install` writes the bare console-script name
(`claude-share-hook`) rather than a resolved absolute path - see "Why the
installed command is a bare name" below. That specific resolution
behavior also was not independently re-verified in this milestone; it is
the standard, expected way for a "command" hook to work and is easy to
override manually (edit `settings.json` to use an absolute path) if it
turns out to be wrong for some Claude Code version.

**Stdin fields this hook actually depends on: none.** `hook.py` reads and
JSON-parses stdin (`_read_stdin_event()`) purely to drain it as good
subprocess hygiene - the quota decision below never inspects
`session_id`, `cwd`, `hook_event_name`, the prompt text, or any other
field. A read/parse failure there is swallowed, not raised, precisely
because nothing downstream depends on it. This was a deliberate design
choice, not an oversight: the local identity config (Milestone 3) already
says unambiguously which member this machine acts as, independent of
which project/cwd/session the prompt came from, so there is nothing in
the stdin payload the quota check actually needs.

### Two limitations, stated here so they aren't discovered later

1. **Placeholder cost, not real usage metering.** There is no Claude
   token/resource cost estimator anywhere in this project - this hook
   cannot know what a prompt will actually cost before it runs. It checks
   availability against a fixed `PLACEHOLDER_PROMPT_COST_UNITS = 1`
   (a named constant in `hook.py`) per prompt, consistent with Milestone
   1's "quota units are entirely abstract" assumption. Real usage
   attribution is out of scope until a future milestone defines a
   `UsageProvider` that can report what a prompt actually cost, after the
   fact.
2. **This hook never calls `consume()`.** It only reads
   (`QuotaService.get_status()` + `CapacityService.get_effective_capacity()`)
   - nothing is deducted. Milestone 4's job is "check before the prompt
   runs," not "meter what it cost." Wiring a fake per-prompt `consume()`
   call here, without a real `UsageProvider` behind it, would create a
   usage counter that looks authoritative but measures nothing real -
   worse than not measuring at all, since it would be actively
   misleading. This is the reason Milestone 4 does not touch `consume()`.

### The check: guaranteed capacity only, not potential/shared

The hook computes `guaranteed_units` (via `CapacityService.get_effective_capacity()`)
and `used_units` (via `QuotaService.get_status()`) for the FIVE_HOUR
window, and compares `remaining = guaranteed_units - used_units` against
`PLACEHOLDER_PROMPT_COST_UNITS`. It deliberately does **not** attempt to
account for SHARED-grant borrowing potential (`potential_units`) when
deciding whether to block or warn. Whether a SHARED draw would actually
succeed depends on a live check of the grant's source's own remaining
balance at the moment of a real `consume()` call (see Milestone 2, "Owner
priority mechanics") - a snapshot taken here, possibly seconds or minutes
before the (not-yet-implemented) real consume happens, could be stale and
present a falsely optimistic number. Guaranteed capacity is the only
figure this member can reliably count on without live-checking someone
else's balance, so it's the conservative, honest signal to gate on.

### Fail-open error handling

Any unexpected exception inside the hook (local DB unreachable, corrupted
config, or anything else) is caught in `main()`, best-effort logged to
`<config_dir>/hook.log`, and results in **exit 0** - never exit 2. A
quota-management tool that crashes and permanently blocks someone's
Claude Code prompts is a categorically worse failure than occasionally
letting one prompt through unchecked; failing closed here would turn a
minor bug into a hard outage of an unrelated tool. Logging failures are
themselves swallowed (`_log_error()`'s own `try/except`), since nothing is
allowed to block a prompt because logging the reason it almost did
so failed.

### Strictly opt-in

If `load_local_identity()` returns `None`, or returns an identity that
hasn't been joined to a pool/member yet (`pool_id`/`member_id` still
`None` - see Milestone 3), the hook exits 0 with no output, before ever
touching SQLite or the application layer. Someone who has never run
`claude-share login`/`join` must see zero difference in their Claude Code
experience - this integration cannot be accidentally "on."

### Warning threshold

`WARNING_THRESHOLD_FRACTION = 0.20` (a named constant in `hook.py`): once
a member's remaining guaranteed FIVE_HOUR capacity drops below 20% of
their own guaranteed ceiling, the prompt is still allowed through, but a
plain-text warning is printed to stdout (added as context, per the
verified mechanism above) instead of staying silent. 20% was chosen as a
reasonable "you're getting close" signal - large enough to give some
advance notice before the hard block, small enough not to nag through the
back half of a window's capacity. It is not user-configurable in this
milestone; a future settings surface could expose it.

### Message format

Both the warning and the block message follow the same two-line
data format, e.g. (block case, exactly matching the spec's example):

```
Claude Share
Allocation exhausted.
Used: 25.0% / 25%
Reset: 1h 32m
```

The two percentages are both expressed as a share of the *pool's* total
(`TOTAL_ALLOCATION_BPS`, i.e. 10,000 = 100%), not of the member's own
allocation: the left number is how much of the whole pool this member has
personally used; the right number is the size of their own guaranteed
slice of the pool. The two are equal exactly when the member has used
their entire guaranteed capacity (hence "25.0% / 25%" in an exhausted,
4-member, no-grants example). `Reset` is the time remaining until the
FIVE_HOUR window's `reset_at`, formatted as `XhYm`/`Xh`/`Ym` depending on
which components are non-zero.

### Why the installed command is a bare name, not an absolute path

`claude-share-hook` (a new console-script entry point, alongside the
existing `claude-share`) is what `hook install` writes into
`settings.json` - not a resolved absolute path like
`/home/alice/.venv/bin/claude-share-hook`. A project-level
`.claude/settings.json` is meant to be committed and shared across a
team; baking in one person's virtualenv path would break for everyone
else. As long as each person has `claude-share` installed in their active
environment, `claude-share-hook` resolves via `PATH` for them
individually - see "What was inferred, not verified" above for the one
assumption this relies on.

### `hook install`/`hook uninstall`: merge, don't overwrite

`integrations/claude_code/settings.py` (`install_hook`/`uninstall_hook`)
never overwrites an existing `settings.json` wholesale - it loads it (or
starts from `{}` if absent), only ever adds/removes the one hook entry
whose `command` matches `claude-share-hook`, and rewrites the file.
Installing is idempotent (a second `install` call is a no-op, detected by
searching for an existing matching command); uninstalling removes only
that entry, dropping an now-empty matcher-group or the whole
`UserPromptSubmit`/`hooks` key only if nothing else is left inside it -
every other hook (for this event or any other) and every other
`settings.json` key is left completely untouched. `hook install`/`hook
uninstall` default to `--project` (`./.claude/settings.json`) when
neither `--project` nor `--user` (`~/.claude/settings.json`) is given.

## Deliberately out of scope for Milestone 4

Per the Milestone 4 brief: no `PreToolUse` or any hook event other than
`UserPromptSubmit`, no MCP-based enforcement, no real token/resource
usage estimation or metering, no `consume()` call with anything other
than the fixed placeholder cost, no VS Code-specific code (VS Code uses
the same Claude Code hook mechanism, so Milestone 3's identity resolution
already covers it without any special-casing), no browser extension, no
central server/networking, no Desktop/Cowork integration, no
notifications beyond the stdout warning / stderr block message.

---

## Milestone 8 — Real token-count-based usage metering (two-hook design)

### What changed from Milestone 4

Milestone 4's `UserPromptSubmit` hook used a fixed placeholder cost
(`PLACEHOLDER_PROMPT_COST_UNITS = 1`) as a pre-check heuristic and
deliberately never called `consume()`. Milestone 8 replaces that metering
gap with **real, measured token-count-based consumption** recorded after
each turn completes, while keeping the pre-prompt hook as a lightweight
availability gate.

This is **genuine measured-token metering** — units are computed from
Claude Code's own reported `input_tokens` / `output_tokens` for each
completed assistant turn — but it is **not identical to Anthropic's
internal account-level accounting**. Claude Share does not see Anthropic's
full billing ledger: system-prompt overhead, prompt-cache read/write
discounts, background API calls that never reach the transcript, and other
vendor-internal pricing nuances are outside what the transcript exposes.
Describe this as "real token-count-based metering from Claude Code's
reported counts," not as "real Anthropic usage."

### The verified integration mechanism (Stop hook)

Researched against Anthropic's official Claude Code hooks reference
(<https://docs.anthropic.com/en/docs/claude-code/hooks>, "Stop" and
"Common input fields" sections, confirmed March 2026):

1. **`Stop` fires once per turn** when the main Claude Code agent finishes
   responding (not on user interrupt; API errors fire `StopFailure`
   instead).
2. **The Stop stdin payload does not include token counts.** Documented
   fields include `session_id`, `transcript_path`, `cwd`,
   `hook_event_name`, `last_assistant_message`, `stop_hook_active`, and
   (v2.1.196+) `prompt_id`. There is no `usage` object on the payload
   itself — community reports and an open feature request
   (anthropics/claude-code#80446) confirm the same.
3. **Token counts live in the session transcript JSONL** that
   `transcript_path` points to. The hooks reference documents that
   transcript writes are **asynchronous** and may lag the in-memory
   conversation when a hook fires; assistant entries carry
   `message.usage` with at least `input_tokens` and `output_tokens`
   (same shape as PostToolUse's documented `tool_response.usage` object).
4. **`stop_hook.py` polls `transcript_path`** with exponential backoff
   (50 ms initial interval, up to 8 seconds total) until the last
   `type: "assistant"` line exposes a parseable `message.usage`, or gives
   up and consumes zero units (fail-open).

Implementation files:

- `integrations/claude_code/stop_hook.py` — Stop hook entry point
  (`claude-share-stop-hook` console script)
- `integrations/claude_code/metering.py` — transcript parsing, weighting
  formula, idempotency-key derivation
- `integrations/claude_code/hook.py` — updated UserPromptSubmit pre-check
- `integrations/claude_code/settings.py` — installs/uninstalls **both**
  hooks idempotently

### Two-hook design: pre-check + post-consume

| Hook | Event | When | Job |
|------|-------|------|-----|
| `claude-share-hook` | `UserPromptSubmit` | Before each prompt | Block/warn if **no** guaranteed FIVE_HOUR capacity remains (`remaining_units <= 0`); cannot know upcoming token cost |
| `claude-share-stop-hook` | `Stop` | After each turn | Read measured token counts from transcript → convert to units → `consume()` |

**Why the pre-check changed:** the upcoming prompt's token cost is
unknowable before it runs. The UserPromptSubmit hook no longer compares
against a placeholder per-prompt cost; it only answers "does this member
have any meaningful guaranteed capacity left right now?" Real cost is
charged after the fact by the Stop hook.

Both hooks remain **strictly opt-in** (no login/join → exit 0, no-op) and
**fail-open** on internal errors (log to `<config_dir>/hook.log`, never
crash or block Claude Code because of a metering bug). The Stop hook always
exits 0 — there is nothing left to block after a turn completes.

### Token → units weighting formula

Named constants in `metering.py` (adjustable without an architecture
change):

```text
INPUT_TOKEN_WEIGHT  = 1
OUTPUT_TOKEN_WEIGHT = 5
TOKEN_WEIGHT_SCALE  = 1000

weighted = (input_tokens * INPUT_TOKEN_WEIGHT) + (output_tokens * OUTPUT_TOKEN_WEIGHT)
units    = max(1, ceil(weighted / TOKEN_WEIGHT_SCALE))   # 0 tokens → 0 units (no consume)
```

**Reasoning:** Anthropic's public API pricing for Sonnet-class models
charges output tokens at roughly five times the input-token rate (e.g.
~$3/M input vs ~$15/M output). Using a 1:5 input:output weight ratio
mirrors that relative cost shape without tying abstract units to dollars.
`TOKEN_WEIGHT_SCALE` keeps typical turns in the tens-of-units range so
numbers stay comparable to Milestone 1 pool allocations (thousands of
units per member per window), not raw token counts.

### `consume()` wiring and remote mode

The Stop hook calls the existing `QuotaService.consume()` (local SQLite
via `SqliteUnitOfWork`) or `RemoteQuotaService.consume()` (HTTP
`POST /quota/consume`) when the local identity has `server_url` set —
the same mode split as `cli/main.py`. Window type is always
`FIVE_HOUR`, matching the UserPromptSubmit pre-check.

If token data is missing/malformed, polling times out, or `consume()`
raises, the hook logs and exits 0 with **zero consumption** — never a
guessed fallback amount.

### Idempotency-key derivation

Stop hooks can re-fire (e.g. session `--resume` replay semantics, hook
re-invocation). `QuotaService.consume()`'s existing idempotency guard
prevents double-charging:

1. **Primary key (preferred):** `claude-code-stop:{prompt_id}` where
   `prompt_id` is the UUID from the Stop payload's common fields
   (documented v2.1.196+, identifies the user prompt for the turn).
2. **Fallback (older Claude Code):** `claude-code-stop:{session_id}:{turn_index}`
   where `turn_index` is the zero-based index of the last
   `type: "assistant"` entry in the transcript when usage is read
   (i.e. count of assistant lines minus one).

A replay with the same key and same amount returns `replayed=True` from
`consume()` without incrementing usage again.

### `hook install` / `hook uninstall` (updated)

`settings.py` now registers **both** console scripts:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "command": "claude-share-hook" }] }
    ],
    "Stop": [
      { "hooks": [{ "type": "command", "command": "claude-share-stop-hook" }] }
    ]
  }
}
```

Merge/idempotent/uninstall behavior from Milestone 4/6 is unchanged: only
these two command entries are added or removed; all other settings and
hooks are preserved.

### Extension and dashboard impact

**No changes required.** Both surfaces already read
`GET /members/{id}/status` (`used_units`) and
`GET /members/{id}/capacity` (`guaranteed_units`). Milestone 8 only
changes *how* `used_units` gets incremented (real `consume()` from the Stop
hook instead of manual CLI `consume` or nothing). Response shapes are
unchanged.

The browser extension still uses its own placeholder pre-check for
claude.ai send interception (Milestone 6 scope); that is a separate
enforcement surface from Claude Code metering.

## Deliberately out of scope for Milestone 8

SOLID/SHARED delegation logic changes, browser-extension core changes
(beyond confirming API compatibility), non-Claude-Code integrations,
TLS/deployment, dashboard batch endpoint, and aligning the extension's
placeholder web pre-check to token-based metering.

---

## Milestone 5 — Central Server (multi-device coordination)

### What problem this solves, and what stays exactly the same

Before this milestone, every local SQLite database was authoritative only
for the one machine it lived on - two devices logged in as the same
`user_id` never actually shared state unless they pointed at the literal
same database file. Milestone 5 adds a central server so that
`Pool`/`Member`/`Allocation`/`QuotaWindow`/`UsageRecord`/
`CapacityRequest`/`CapacityGrant`/`SharedConsumptionRecord` state can live
in one place (PostgreSQL) that every device talks to over HTTP.

Nothing about the domain/application logic changed to make this possible,
by design: `QuotaService`, `CapacityService`, and `AgentService` are the
exact same classes, calling the exact same `UnitOfWork`/repository ports,
that Milestones 1-4 already had. This milestone only (1) adds a second
`UnitOfWork` implementation (`infrastructure/postgres/`) those services can
be pointed at instead of `SqliteUnitOfWork`, (2) adds an HTTP layer
(`server/`) in front of the same services, and (3) adds a second,
clearly-separated client-side code path (`agent/remote_client.py`) so the
CLI can talk to that HTTP layer instead of opening SQLite directly.
`SqliteUnitOfWork` and the pure-local CLI flow from Milestones 1-4 are
completely unchanged and still work with zero server/Postgres setup - see
"Local-only vs server-connected mode" below.

### Layering (updated)

```
domain/          <- unchanged
application/     <- unchanged business rules; +application/tokens.py (device token hashing, new)
infrastructure/
  sqlite/        <- unchanged, still the default for pure-local usage
  postgres/      <- NEW: PostgresUnitOfWork + repositories, same ports as sqlite/
agent/           <- +remote_client.py (HTTP client path), identity.py gained server_url/device_token
integrations/    <- unchanged
server/          <- NEW: FastAPI app exposing QuotaService/CapacityService/AgentService over HTTP
cli/             <- unchanged commands; main() now picks a local or remote service pair per identity
```

### Postgres locking strategy: why, and exactly what it is

**Isolation level: Postgres's default, READ COMMITTED - not
SERIALIZABLE.** The reasoning is spelled out in full in
`infrastructure/postgres/unit_of_work.py`'s module docstring; the short
version: Postgres `SERIALIZABLE` achieves serializability by letting
conflicting transactions proceed and then aborting one at COMMIT time with
a `serialization_failure` the caller must retry. `QuotaService.consume()`/
`CapacityService.approve_request()` have no retry loop around `with
self._uow_factory() as uow: ... uow.commit()`, and this milestone's brief
explicitly forbids touching that code - an aborted commit would surface as
an unhandled `psycopg.errors.SerializationFailure` instead of the graceful
`ConsumeResult(accepted=False, ...)`/`InsufficientSourceCapacityError`
these services already return under SQLite. Explicit locking, taken
*before* a transaction reads a row it might later write, avoids this by
blocking a losing transaction instead of aborting it - a losing caller
just waits longer and then proceeds against fresh data, exactly like
SQLite's `BEGIN IMMEDIATE` already made it wait.

**Locking primitive: Postgres transaction-scoped advisory locks
(`pg_advisory_xact_lock`), not `SELECT ... FOR UPDATE`.** The first working
version of `infrastructure/postgres/repositories.py` used plain row-level
`FOR UPDATE` locking - the more commonly-reached-for idiom for this kind
of problem - and it gave correct answers under light load. Under the real
concurrency test adapted from Milestone 1
(`tests/test_postgres_unit_of_work.py`), it reproducibly hit
`psycopg.errors.DeadlockDetected`. This is a documented Postgres
characteristic, not an ordering bug in this codebase: when 3+ backends all
queue `SELECT ... FOR UPDATE` on one frequently-updated row (exactly what
happens here - a small pool means heavy contention on very few
`QuotaWindow` rows), each `UPDATE` creates a new MVCC tuple version, and
the deadlock detector's wait-for graph over those tuple-version chains can
report a cycle that isn't a real application-level deadlock. Advisory
locks don't have this failure mode: they're plain mutexes in Postgres's
lock manager with no MVCC/tuple involvement, so many transactions queuing
on the same key is the textbook case they're built for - while they still
participate fully in the deadlock detector for genuine cross-key cycles.
See `infrastructure/postgres/locking.py`'s module docstring for the full
writeup of this switch.

**What's locked, and why it's sufficient - two mechanisms:**

1. `QuotaWindowRepository.get()`/`list_by_member()`,
   `CapacityGrantRepository.get()`, and `CapacityRequestRepository.get()`
   each take an advisory lock keyed by the row's logical identity
   (`member_id`+`window_type` for a window, an id for a grant/request)
   *before* reading. Every check-then-write flow in `QuotaService`/
   `CapacityService` reads the relevant `QuotaWindow` row before it reads
   any `CapacityGrant` rows (`consume()`'s own-window fetch happens before
   its shared-grants loop; `approve_request()`'s source-window fetch
   happens before `member_grant_summary()`), so locking just the window is
   sufficient to also serialize the grant-related over-commit checks in
   the same transaction - `CapacityGrantRepository.list_by_source()`/
   `list_by_recipient()` deliberately take no lock of their own, relying
   on that ordering (documented at each call site).
2. `UsageRecordRepository.find_by_idempotency_key()` takes an advisory
   lock keyed by the idempotency key itself, before reading. This closes a
   race row/window locking can't: two concurrent `consume()` calls sharing
   a *never-before-seen* idempotency key have no existing row to lock onto
   at all, so without this, both could see "not found," both proceed, and
   one would hit the `usage_records` `UNIQUE(idempotency_key)` constraint
   as an unhandled `IntegrityError` instead of a graceful idempotent
   replay.

**Consequence, matching Milestone 1's own stated tradeoff exactly:** since
`get_status()`/`check_quota()`/`get_effective_capacity()` read through the
same locking repository methods `consume()`/`approve_request()` do, reads
serialize against writes and against each other here too - the same
"every operation fully serializes" tradeoff Milestone 1's docs already
called out for SQLite's `BEGIN IMMEDIATE` ("not the right tradeoff once a
central server introduces real concurrent load... revisit this when the
central server milestone is built"). This milestone deliberately does
**not** revisit it: proving the exact same "no double-spend, no oversell"
guarantee, unmodified, was the stated priority, and splitting genuinely
read-only queries onto a non-locking snapshot-read path is real, identified,
future work - not addressed here, exactly as flagged for it back in
Milestone 1.

**One known, accepted gap:** two `consume()` calls that draw SHARED
capacity from *each other* in opposite directions at the same moment (A
draws from B's window while B concurrently draws from A's) could in
principle lock their own window, then block waiting for the other's -
a genuine two-key deadlock, which Postgres's detector would correctly
abort one side of (surfacing as an unhandled exception, since neither
service retries). None of the required tests exercise this bidirectional
pattern (the adapted Milestone 1 SHARED test is asymmetric: one member
draws from another who never draws back), and fixing it would mean either
a global lock-ordering scheme or retry logic in `QuotaService` - both out
of scope here (the former adds real complexity for a scenario the trust
model makes rare in a small pool; the latter touches forbidden code).
Flagged here rather than silently accepted.

**Insertion-order surrogate:** `MemberRepository.list_by_pool()` needs to
return members in creation order (many callers, including most of this
project's own tests, destructure `alice, bob = service.list_members(...)`
positionally). SQLite gets this for free from its implicit `rowid`;
Postgres has no equivalent, and `id` is a random UUID that doesn't sort
that way - so `infrastructure/postgres/schema.py`'s `members` table has an
extra `seq BIGSERIAL` column, ordered on but never exposed on the domain
`Member` dataclass.

### Minimal device-token auth: the design and why it's sufficient here

**Mechanism.** `AgentService.register_device()` (extended, not replaced -
existing Milestone 3 callers and tests are unaffected, see below) now also
generates a 256-bit opaque token (`application/tokens.py:
generate_device_token()`, `secrets.token_urlsafe(32)`) and stores only its
SHA-256 hash (`Device.token_hash`) - the plaintext token is set on the
`Device` object `register_device()` returns (`Device.token`) and
*nowhere else*; it is never persisted and never returned again by any
other call. The server's `POST /devices` endpoint is the only response
that ever carries it. Every subsequent request presents it as
`Authorization: Bearer <token>`; `server/auth.py:get_current_device()`
hashes the presented token and looks up the owning `Device` by
`token_hash`, rejecting an unknown/missing one with 401.
`server/auth.py:require_member()` then re-checks, fresh, on every single
request, that the resolved device's `user_id` actually owns the specific
`member_id` the request is trying to act as (same check `join_pool()`
already made locally in Milestone 3, just re-run per-request instead of
once at `join` time, since HTTP has no persistent session to trust
between calls) - a device authenticated as Alice can never act as Bob's
member_id, even though both belong to the same server.

**Why extending `Device`/`register_device()` didn't break Milestones 1-4.**
`Device` gained two fields (`token_hash: str`, `token: str | None = None`)
and `register_device()` now always mints a token - but every existing
Milestone 3 caller (the local `login()`, every existing test) only ever
reads `.id`/`.user_id`/`.device_name` off the returned `Device`, so nothing
about their observed behavior changed; the local-only CLI flow simply
never looks at `.token`. `DeviceRepository` gained one new abstract method
(`find_by_token_hash`), implemented in both `sqlite/` and `postgres/`.

**Why this is intentionally minimal - no OAuth, no JWT, no expiry/refresh,
no scopes.** The trust model this whole project targets is a small,
cooperative, already-trusted group - see the milestone's own framing:
protecting against a malicious member is explicitly out of scope; what's
in scope is "don't accidentally build something trivially broken over a
real network." An opaque, 256-bit, hashed-at-rest bearer token checked on
every request already defeats the realistic threat here (a stray or
misconfigured device, or a stranger with no token at all, accidentally or
opportunistically acting as someone else over the network) - it is not
meant to, and does not need to, resist a sophisticated insider adversary
within the trusted group. Anything more elaborate (rotation, short-lived
JWTs, scoped tokens) would be complexity spent defending against a threat
this milestone's own brief says is out of scope.

**Two bootstrap exceptions - endpoints that require no token at all:**
`POST /pools` (there is no prior identity to check against - this is how
the first members/user_ids in a deployment come to exist; no pool exists
yet, so no device/token can be scoped to one), and `POST /devices` (how a
user_id-holder gets their *first* token - requiring a token to obtain one
is circular). Listing members is not structurally unavoidable: the caller
who just created a pool already receives the full member list in the
`POST /pools` response body, and every other caller can present a valid
bearer token. `GET /pools/{pool_id}/members` therefore requires
authentication like every other endpoint. See `server/auth.py`/
`server/routes.py`'s module docstrings for the full per-endpoint
reasoning.

### Transport security: Tailscale for this deployment (Milestone 9)

Bearer tokens and quota/capacity data are sent as ordinary HTTP request/
response bodies and headers. Over the **public internet** in plain HTTP,
they are as sniffable as a plaintext password.

**`claude-share-server` does not terminate TLS** (see `server/main.py`) —
certificate management stays out of the application, matching how FastAPI/
uvicorn services are normally deployed.

For **this project's chosen deployment** (a small, fixed, trusted group —
not a public service), the documented solution is **Tailscale**: a private
WireGuard mesh so only tailnet members can reach the server, with traffic
encrypted between devices and **no public port 8001 exposure required**. See
[docs/TAILSCALE_SETUP.md](TAILSCALE_SETUP.md) for step-by-step server and
Windows client setup, migrating clients off a public IP, and closing Oracle
Cloud / iptables exposure.

**Why Tailscale here (and not Caddy/nginx on a public domain):** every user
is a known person who can install one app; no anonymous public clients; no
domain or certificate renewal; simpler ops. **Trade-off:** each person's
device must join the tailnet — wrong model for a public-facing SaaS.

Within the tailnet, clients use `http://100.x.y.z:PORT` or MagicDNS
(`http://<device>.<tailnet>.ts.net:PORT`). That is HTTP at the application
layer but **not** cleartext on the public internet — Tailscale encrypts the
path. The codebase accepts any valid `http`/`https` base URL (no hostname-
format restrictions); the browser extension's optional `http://*/*` host
permission already covers Tailscale addresses.

A **public reverse proxy + TLS** remains a valid pattern for other
deployments but is intentionally **not** documented here (Milestone 9
scope).

### Local-only vs server-connected mode: how they coexist

A `LocalIdentity` (`agent/identity.py`) now optionally carries
`server_url`/`device_token`, both `None` for a purely local identity
(Milestones 1-4 behavior, byte-for-byte unchanged - `load_local_identity`
defaults missing keys to `None`, so an existing pre-Milestone-5
`config.json` still round-trips to an identical `LocalIdentity`).
`cli/main.py:main()` makes exactly one decision, once, right after loading
the identity: if `identity.is_remote` (i.e. `server_url` is set), build
`RemoteQuotaService`/`RemoteCapacityService` (`agent/remote_client.py`)
around that server_url/device_token; otherwise, build the same
`QuotaService`/`CapacityService` around `SqliteUnitOfWork` exactly as
before. Every `_cmd_*` function in `cli/main.py` is completely unchanged -
they only ever call methods like `.get_status()`/`.consume()` on whatever
service object they were handed, never construct one themselves or check
which mode they're in. `login --server <url>` is the one command that
*sets* remote mode (`agent.commands.login_remote()`); `pool create
--server <url>` can bootstrap a pool on an arbitrary server even before
any identity is configured (or with a different one already configured),
exactly mirroring the server's own no-auth bootstrap exception.

### Why the remote client is a separate HTTP path, not a UnitOfWork adapter

`UnitOfWork` is a transaction boundary over *repositories* - built so
`consume()` can do "read a row, maybe read a few more, write updated rows,
atomically" without knowing whether SQLite or Postgres is underneath. A
call to `POST /quota/consume` isn't shaped like that: it's one
already-composed round trip to a *remote copy* of the entire `consume()`
method, not a sequence of individual repository operations this process
could wrap in its own transaction. Making `RemoteQuotaService` implement
`UnitOfWork`/the repository ports would mean inventing fake repository
methods that either make their own separate HTTP call each (destroying
atomicity) or buffer state client-side against a batching scheme the
server doesn't support - it doesn't stretch cleanly, it would misrepresent
what's actually happening over the wire. So `agent/remote_client.py`
instead duck-types `QuotaService`/`CapacityService`/`AgentService`'s own
*public methods* directly (one HTTP call each), returning the exact same
`application/dto.py`/`domain/models.py` dataclasses the local services
return - which is what lets every existing `_cmd_*` function and
`agent/commands.py:agent_status()` work unchanged against either mode. See
`agent/remote_client.py`'s own module docstring for the full reasoning.

### HTTP layer: one endpoint per existing application method, nothing more

`server/routes.py` is a thin adapter: every handler either forwards
straight to `QuotaService`/`CapacityService`/`AgentService` and converts
the returned dataclass to a `server/schemas.py` Pydantic model for
serialization, or (for the ownership-checked ones) does that after calling
`server/auth.py:require_member()` once. No business rule, quota
arithmetic, or capacity-delegation logic is duplicated at this layer - a
`DomainError` raised by a service is left to propagate to a FastAPI
exception handler (`server/app.py`, using `server/errors.py`'s status-code
mapping) rather than being caught and re-implemented as a check here. A
`consume()` call rejecting for an ordinary business reason (insufficient
guaranteed+shared capacity) is not an HTTP error at all - it's a normal
`200` response with `accepted: false`, exactly mirroring the local CLI's
non-crashing exit-code-2 for the same case.

### Deliberately out of scope for Milestone 5

Per the milestone brief: no browser extension, no Desktop/Cowork
integration, no dashboard/UI, no real Anthropic OAuth/account integration,
no TLS termination inside the FastAPI app itself, no rate limiting/IP
allowlisting/other adversarial-hardening beyond the identity-verification
auth described above (the trust model here remains "all members are
cooperative" - see the milestone's own framing), and no change whatsoever
to the domain/application quota or capacity-delegation rules. A background
sweep for lazily-expiring grants, splitting read-only queries onto a
non-locking Postgres path, and resolving the bidirectional-SHARED-draw
deadlock gap all remain identified, real, future work - not addressed
here.

---

## Milestone 6 — Browser Extension (claude.ai, Manifest V3)

### What problem this solves, and what it deliberately is not

Milestones 1–5 give quota visibility to CLI users and Claude Code users
(via the `UserPromptSubmit` hook). Milestone 6 adds the same *visibility*
for people using **claude.ai directly in a browser** — backed by the
**same Milestone 5 central server and device-token auth**, with **zero
new quota business logic** on either client or server.

**This is explicitly weaker enforcement than the Claude Code hook.**
Anthropic documents a first-party "block before send" hook for Claude Code;
claude.ai has no equivalent stable API. The extension can only observe/inject
into the page DOM and best-effort intercept send actions. That dependency is
**fragile** (breaks silently when claude.ai's frontend changes) and
**bypassable** (override button, DevTools, alternate UI paths). See
`extension/README.md` for the full reliability limitations — stated
prominently there, not buried.

Design principles carried over from Milestone 4's hook:

- **Soft enforcement only** — persistent indicator + warning + best-effort
  send interception when exhausted, with an obvious override
- **Fail open** — if selectors fail, the server is unreachable, or anything
  throws internally, the extension must not trap the user on claude.ai
  (no stuck UI, no visible page errors)
- **Requires Milestone 5 server** — pure local-SQLite mode is impossible
  from a browser extension

### New component: `extension/`

A separate TypeScript Manifest V3 package (not part of the Python wheel):

```
extension/
  manifest.json          Manifest V3 (Chrome/Chromium)
  src/
    background.ts        polls GET /members/{id}/status + /capacity
    content.ts           indicator + best-effort send intercept on claude.ai
    quota.ts             formatting/decision logic (mirrors hook.py)
    intercept.ts         DOM helpers (testable in isolation)
    api.ts               HTTP client for background/popup only
    popup/popup.ts       configure server URL + device token + member_id
  tests/                 Vitest + jsdom
  README.md              install/configure/limitations (read this first)
```

**Auth:** reuses Milestone 5's existing `POST /devices` bootstrap and
`Authorization: Bearer <token>` on subsequent calls — no second auth
mechanism. Token stored in `chrome.storage.local` (not sync).

**API calls:** originate from the **background service worker and popup**,
never from the claude.ai page context. The content script only receives
quota snapshots via `chrome.runtime` messaging.

### Server-side changes for the extension

**No new endpoints. No CORS middleware added.**

Investigation: Manifest V3 extension pages (background worker, popup) with
**host permissions** granted for the configured server URL make `fetch()`
calls that are **not subject to normal web-page CORS policy**. Content
scripts on claude.ai do not call the server directly (by design), so
claude.ai's origin never needs to be listed in `Access-Control-Allow-Origin`.
The popup requests optional host permission (`http://*/*`, `https://*/*`) at
configure time via `chrome.permissions.request()` for the specific server
origin pattern.

If a future milestone adds a regular web dashboard served from a browser tab,
*that* would likely need explicit CORS configuration — out of scope here.

### DOM dependency (fragile by design)

The content script (`extension/src/intercept.ts`) heuristically targets:

- Send button: `button[aria-label*="Send"]`, `button[data-testid*="send"]`,
  `button[type="submit"]`
- Input area: `[contenteditable="true"][role="textbox"]`, `textarea`,
  composer `[contenteditable]` descendants

If neither element is found, the script **fail-opens** (no UI, no intercept).
A `MutationObserver` re-attaches when the SPA DOM changes. See
`extension/README.md` for the full selector table and bypass/override behavior.

### Deliberately out of scope for Milestone 6

Per the milestone brief: no Chrome Web Store packaging, no Firefox/Safari
support, no new server business logic, no real Anthropic account integration,
no Desktop/Cowork integration, no dashboard beyond the extension's own popup/
indicator UI, and no hard blocking beyond best-effort send interception as
scoped above.

---

## Milestone 7 — Dashboard (read-only web UI)

### Thin client over the Milestone 5 API

The dashboard (`dashboard/`) is a static single-page app served by the
FastAPI app at `/dashboard/` when `dashboard/dist/` exists (see
`server/app.py`). It displays pool/member quota state and exposes
approve/reject/revoke actions, but **implements no quota or capacity
business logic** — every figure is read from an existing API field, and
every mutation calls an existing Milestone 5 endpoint.

Authentication matches the extension: a device bearer token and `member_id`
stored in `localStorage`, sent as `Authorization: Bearer ...` on each
request. When served from the same origin as the API, the setup form can
leave Server URL blank (relative fetches).

Polling refreshes views every 60 seconds (no websockets), consistent with
the browser extension.

### New read endpoints (Milestone 7 only additions)

Milestone 2/5 never exposed HTTP list endpoints for pending requests or
active grants. The dashboard needed two minimal read-only additions:

| Endpoint | Purpose |
|----------|---------|
| `GET /members/{member_id}/capacity/requests/pending` | PENDING requests where this member is the target (incoming approvals) |
| `GET /members/{member_id}/capacity/grants` | ACTIVE grants sent and received (`{ sent, received }`) |

Both require bearer auth and `require_member()` for the path `member_id`.
Implementation is thin repository reads plus existing
`active_grants_as_of()` for grant expiration — no new business rules.

Repository port added: `CapacityRequestRepository.list_pending_by_target()`.
Application methods: `CapacityService.list_pending_requests_for_target()`
and `list_active_grants()`.

### Pool overview fan-out (superseded by Milestone 10)

Milestone 7 originally called `GET /pools/{pool_id}/members` once, then
for each member `GET /members/{id}/status` plus `GET /members/{id}/capacity`
for both window types — **1 + 3N HTTP calls** per refresh (e.g. 13 calls
for a 4-person pool). That was acceptable for small pools but was flagged
as something a batch endpoint should eventually replace.

### Milestone 10 — `GET /pools/{pool_id}/overview`

Adds a read-only aggregation endpoint that returns, for every pool member
in one response:

- `member` summary (`MemberOut`)
- `status` (`MemberStatusOut`, both windows)
- `capacity` map (`EffectiveCapacityOut` for `five_hour` and `weekly`)

**Authorization:** bearer token required; `require_pool_membership()`
checks that the authenticated device's `user_id` owns a member row in
`pool_id` (same ownership rule as `require_member()`, pool-scoped).

**Implementation:** the handler calls existing
`QuotaService.get_status()` and `CapacityService.get_effective_capacity()`
once per member — no duplicated quota math. Each service method opens its
own `UnitOfWork` transaction internally; the batch endpoint does **not**
wrap all members in a single database transaction. That matches what the
old client-side fan-out did (independent per-member reads) and avoids
refactoring services to accept an external unit-of-work. A single
point-in-time snapshot across all members would require that deeper
change; for the small/moderate pools this project targets, per-member
consistency is sufficient.

The dashboard pool overview (and member-name lookups on pending/grants
views) now call this endpoint instead of fanning out.

Per-member `GET /members/{id}/status` and `GET /members/{id}/capacity`
remain unchanged for the CLI and extension.

Pagination was not added — realistic pool sizes here stay small enough
that one response per pool is fine.

### Deliberately out of scope for Milestone 7

Per the milestone brief: no websockets, no request-creation UI (CLI
remains the primary way to open new SOLID/SHARED requests), no multi-pool
admin, no new auth mechanism, no duplicated quota math on the client.
See `dashboard/README.md` for build/serve/configure instructions.
