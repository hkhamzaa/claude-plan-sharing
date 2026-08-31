"""Minimal argparse CLI for the local quota engine.

    claude-share pool create --name <name> --members <name1,name2,...>
    claude-share status [--member <member_id>]
    claude-share consume [--member <member_id>] --window <five_hour|weekly> \\
        --amount <units> --idempotency-key <key>

    claude-share request [--pool <id>] --from <member_id> [--to <member_id>] \\
        --window <five_hour|weekly> --amount <units> --type <solid|shared> \\
        [--message <text>]
    claude-share request approve --request-id <id> [--by <member_id>]
    claude-share request reject --request-id <id> [--by <member_id>]
    claude-share grant revoke --grant-id <id> [--by <member_id>]
    claude-share capacity [--member <member_id>] --window <five_hour|weekly>

    claude-share login --user-id <user_id> --device-name <name>
    claude-share join --pool <pool_id> --member <member_id>
    claude-share whoami

    claude-share hook install [--project | --user]
    claude-share hook uninstall [--project | --user]

`--from`/`--to` on `request` read as "request capacity from the owner, to
the requester": `--from` is the member who owns the capacity and must
approve (`target_member_id`); `--to` is the member who would receive it
(`requester_member_id`). See docs/architecture.md for why this mapping was
chosen over the alternative reading.

Bracketed args above (`--member`, `--pool`, `--to`, `--by`) fall back to
this machine's local identity (set via `login`/`join`) when omitted -
explicit arguments always override the local identity when both are
given. See docs/architecture.md ("Local identity / config file").

This module only formats input/output; all behaviour lives in
`claude_share.application.quota_service.QuotaService`,
`claude_share.application.capacity_service.CapacityService`, and
`claude_share.agent.commands` (login/join/whoami logic).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from claude_share.agent import commands as agent_commands
from claude_share.agent.errors import AgentError
from claude_share.agent.identity import DEFAULT_CONFIG_PATH, LocalIdentity, load_local_identity
from claude_share.application.capacity_service import CapacityService
from claude_share.application.dto import MemberStatus, WindowStatus
from claude_share.application.quota_service import QuotaService
from claude_share.domain.errors import DomainError
from claude_share.domain.models import CapacityType, WindowType
from claude_share.infrastructure.sqlite.schema import init_db
from claude_share.infrastructure.sqlite.unit_of_work import SqliteUnitOfWork
from claude_share.integrations.claude_code.settings import install_hook, uninstall_hook

DEFAULT_DB_PATH = Path.home() / ".claude-share" / "claude_share.db"


def _resolve_db_path(cli_value: str | None) -> Path:
    if cli_value:
        return Path(cli_value)
    env_value = os.environ.get("CLAUDE_SHARE_DB")
    if env_value:
        return Path(env_value)
    return DEFAULT_DB_PATH


def _resolve_config_path(cli_value: str | None) -> Path:
    if cli_value:
        return Path(cli_value)
    env_value = os.environ.get("CLAUDE_SHARE_CONFIG")
    if env_value:
        return Path(env_value)
    return DEFAULT_CONFIG_PATH


def _resolve_member_id(explicit: str | None, identity: LocalIdentity | None) -> str | None:
    """Explicit --member/--to/--by always wins; local identity is only a fallback."""
    if explicit:
        return explicit
    if identity is not None and identity.member_id is not None:
        return identity.member_id
    return None


def _resolve_pool_id(explicit: str | None, identity: LocalIdentity | None) -> str | None:
    if explicit:
        return explicit
    if identity is not None and identity.pool_id is not None:
        return identity.pool_id
    return None


_NO_IDENTITY_HINT = "run `claude-share login` and `claude-share join` first, or pass it explicitly"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="claude-share", description="Local Claude quota-sharing engine.")
    parser.add_argument(
        "--db",
        dest="db",
        default=None,
        help="Path to the SQLite database file (default: %(default)s, or $CLAUDE_SHARE_DB)"
        % {"default": str(DEFAULT_DB_PATH)},
    )
    parser.add_argument(
        "--config",
        dest="config",
        default=None,
        help="Path to the local identity config file (default: %(default)s, or $CLAUDE_SHARE_CONFIG)"
        % {"default": str(DEFAULT_CONFIG_PATH)},
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    pool_parser = subparsers.add_parser("pool", help="Manage pools.")
    pool_subparsers = pool_parser.add_subparsers(dest="pool_command", required=True)

    pool_create_parser = pool_subparsers.add_parser("create", help="Create a pool and its members.")
    pool_create_parser.add_argument("--name", required=True, help="Pool name.")
    pool_create_parser.add_argument(
        "--members",
        required=True,
        help="Comma-separated member display names, e.g. 'Alice,Bob,Carol'.",
    )

    status_parser = subparsers.add_parser("status", help="Show a member's quota status.")
    status_parser.add_argument(
        "--member", dest="member_id", default=None, help="Member id (default: this machine's joined member)."
    )

    consume_parser = subparsers.add_parser("consume", help="Consume quota units for a member.")
    consume_parser.add_argument(
        "--member", dest="member_id", default=None, help="Member id (default: this machine's joined member)."
    )
    consume_parser.add_argument(
        "--window",
        required=True,
        choices=[wt.value for wt in WindowType],
        help="Which quota window to consume from.",
    )
    consume_parser.add_argument("--amount", required=True, type=int, help="Units to consume (positive integer).")
    consume_parser.add_argument(
        "--idempotency-key",
        required=True,
        dest="idempotency_key",
        help="Unique key identifying this consume attempt; retries with the same key never double-spend.",
    )

    # `claude-share request ...` (create) and `claude-share request approve|reject ...`
    # share one subparser: an optional leading positional distinguishes them,
    # since argparse subparsers can't easily express "flags-only, or a
    # further subcommand" at the same level.
    request_parser = subparsers.add_parser(
        "request", help="Create a capacity request, or approve/reject an existing one."
    )
    request_parser.add_argument(
        "action",
        nargs="?",
        choices=["approve", "reject"],
        default=None,
        help="Omit to create a new request.",
    )
    request_parser.add_argument(
        "--pool", dest="pool_id", default=None, help="Pool id (for creation; default: local identity's pool)."
    )
    request_parser.add_argument(
        "--from", dest="from_member_id", default=None, help="Capacity owner's member id (for creation)."
    )
    request_parser.add_argument(
        "--to",
        dest="to_member_id",
        default=None,
        help="Requesting/recipient member id (for creation; default: local identity's member).",
    )
    request_parser.add_argument(
        "--window", dest="window", choices=[wt.value for wt in WindowType], default=None
    )
    request_parser.add_argument("--amount", dest="amount", type=int, default=None)
    request_parser.add_argument(
        "--type", dest="capacity_type", choices=[t.value for t in CapacityType], default=None
    )
    request_parser.add_argument("--message", dest="message", default=None)
    request_parser.add_argument("--request-id", dest="request_id", default=None, help="For approve/reject.")
    request_parser.add_argument(
        "--by",
        dest="by_member_id",
        default=None,
        help="For approve/reject (default: local identity's member).",
    )

    grant_parser = subparsers.add_parser("grant", help="Manage capacity grants.")
    grant_subparsers = grant_parser.add_subparsers(dest="grant_command", required=True)
    grant_revoke_parser = grant_subparsers.add_parser("revoke", help="Revoke an active grant early.")
    grant_revoke_parser.add_argument("--grant-id", dest="grant_id", required=True)
    grant_revoke_parser.add_argument(
        "--by", dest="by_member_id", default=None, help="Default: local identity's member."
    )

    capacity_parser = subparsers.add_parser(
        "capacity", help="Show a member's grant-aware effective capacity for a window."
    )
    capacity_parser.add_argument(
        "--member", dest="member_id", default=None, help="Member id (default: this machine's joined member)."
    )
    capacity_parser.add_argument("--window", dest="window", required=True, choices=[wt.value for wt in WindowType])

    login_parser = subparsers.add_parser("login", help="Point this machine at an existing user_id.")
    login_parser.add_argument("--user-id", dest="user_id", required=True)
    login_parser.add_argument("--device-name", dest="device_name", required=True)

    join_parser = subparsers.add_parser(
        "join", help="Point this machine's local identity at a specific pool/member."
    )
    join_parser.add_argument("--pool", dest="pool_id", required=True)
    join_parser.add_argument("--member", dest="member_id", required=True)

    subparsers.add_parser("whoami", help="Show this machine's local identity and status.")

    hook_parser = subparsers.add_parser("hook", help="Manage the Claude Code UserPromptSubmit hook.")
    hook_subparsers = hook_parser.add_subparsers(dest="hook_command", required=True)

    hook_install_parser = hook_subparsers.add_parser(
        "install", help="Install the quota-check hook into Claude Code settings.json."
    )
    _add_hook_scope_args(hook_install_parser)

    hook_uninstall_parser = hook_subparsers.add_parser(
        "uninstall", help="Remove the quota-check hook from Claude Code settings.json."
    )
    _add_hook_scope_args(hook_uninstall_parser)

    return parser


def _add_hook_scope_args(parser: argparse.ArgumentParser) -> None:
    scope_group = parser.add_mutually_exclusive_group()
    scope_group.add_argument(
        "--project",
        dest="scope",
        action="store_const",
        const="project",
        help="Target ./.claude/settings.json (default).",
    )
    scope_group.add_argument(
        "--user",
        dest="scope",
        action="store_const",
        const="user",
        help="Target ~/.claude/settings.json.",
    )
    parser.set_defaults(scope="project")


def _build_services(db_path: Path) -> tuple[QuotaService, CapacityService]:
    init_db(db_path)
    uow_factory = lambda: SqliteUnitOfWork(db_path)
    return QuotaService(uow_factory=uow_factory), CapacityService(uow_factory=uow_factory)


def _cmd_pool_create(service: QuotaService, args: argparse.Namespace) -> int:
    member_names = [name.strip() for name in args.members.split(",") if name.strip()]
    if not member_names:
        print("error: --members must contain at least one non-empty name", file=sys.stderr)
        return 1

    pool = service.create_pool(args.name, member_names)
    members = service.list_members(pool.id)

    print(f"Created pool {pool.id!r} ({pool.name!r}) with {pool.member_count} member(s):")
    for member in members:
        print(f"  {member.display_name}: member_id={member.id} user_id={member.user_id}")
    return 0


def _format_window_line(window_type: str, status: WindowStatus) -> str:
    return (
        f"  [{window_type}] allocation={status.allocation_units} used={status.used_units} "
        f"remaining={status.remaining_units} window_start={status.window_start.isoformat()} "
        f"reset_at={status.reset_at.isoformat()}"
    )


def _cmd_status(service: QuotaService, args: argparse.Namespace, identity: LocalIdentity | None) -> int:
    member_id = _resolve_member_id(args.member_id, identity)
    if member_id is None:
        print(f"error: no member specified ({_NO_IDENTITY_HINT} with --member).", file=sys.stderr)
        return 1

    status: MemberStatus = service.get_status(member_id)
    print(f"Member {status.member_id!r} ({status.display_name!r}) in pool {status.pool_id!r}:")
    for window_type in WindowType:
        window_status = status.windows.get(window_type)
        if window_status is not None:
            print(_format_window_line(window_type.value, window_status))
    return 0


def _cmd_consume(service: QuotaService, args: argparse.Namespace, identity: LocalIdentity | None) -> int:
    member_id = _resolve_member_id(args.member_id, identity)
    if member_id is None:
        print(f"error: no member specified ({_NO_IDENTITY_HINT} with --member).", file=sys.stderr)
        return 1
    if args.amount <= 0:
        print("error: --amount must be a positive integer", file=sys.stderr)
        return 1

    result = service.consume(
        member_id=member_id,
        window_type=WindowType(args.window),
        amount=args.amount,
        idempotency_key=args.idempotency_key,
    )

    if result.accepted:
        replay_note = " (idempotent replay - no new consumption)" if result.replayed else ""
        shared_note = ""
        if result.shared_units_used:
            shared_note = f", {result.shared_units_used} of which drawn from SHARED grant(s)"
        print(
            f"Consumed {result.amount} unit(s) from {result.window_type.value} window"
            f"{replay_note}{shared_note}. own_guaranteed_remaining={result.remaining_units}/{result.allocation_units}"
        )
        return 0

    print(
        f"Rejected: requested {result.amount} unit(s) from {result.window_type.value} window "
        f"but only {result.remaining_units}/{result.allocation_units} guaranteed remain, and "
        f"available SHARED grants could not cover the shortfall (reason={result.reason}).",
        file=sys.stderr,
    )
    return 2


def _cmd_request_create(service: CapacityService, args: argparse.Namespace, identity: LocalIdentity | None) -> int:
    pool_id = _resolve_pool_id(args.pool_id, identity)
    to_member_id = _resolve_member_id(args.to_member_id, identity)

    required = {
        "--pool": pool_id,
        "--from": args.from_member_id,
        "--to": to_member_id,
        "--window": args.window,
        "--amount": args.amount,
        "--type": args.capacity_type,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        print(
            f"error: creating a request requires {', '.join(missing)} "
            f"(--pool/--to fall back to local identity if configured; {_NO_IDENTITY_HINT}).",
            file=sys.stderr,
        )
        return 2
    if args.amount <= 0:
        print("error: --amount must be a positive integer", file=sys.stderr)
        return 1

    request = service.request_capacity(
        pool_id=pool_id,
        requester_member_id=to_member_id,
        target_member_id=args.from_member_id,
        window_type=WindowType(args.window),
        amount=args.amount,
        type=CapacityType(args.capacity_type),
        message=args.message,
    )
    print(
        f"Created {request.type.value} request {request.id!r}: {request.requester_member_id!r} is "
        f"asking {request.target_member_id!r} for {request.amount} unit(s) in "
        f"{request.window_type.value} window. status={request.status.value}"
    )
    return 0


def _cmd_request_approve(service: CapacityService, args: argparse.Namespace, identity: LocalIdentity | None) -> int:
    by_member_id = _resolve_member_id(args.by_member_id, identity)
    if not args.request_id or not by_member_id:
        print(f"error: approve requires --request-id and --by ({_NO_IDENTITY_HINT} with --by).", file=sys.stderr)
        return 2
    grant = service.approve_request(args.request_id, by_member_id)
    print(
        f"Approved. Created {grant.type.value} grant {grant.id!r}: {grant.source_member_id!r} -> "
        f"{grant.recipient_member_id!r}, {grant.amount} unit(s) in {grant.window_type.value} window, "
        f"expires_at={grant.expires_at.isoformat()}"
    )
    return 0


def _cmd_request_reject(service: CapacityService, args: argparse.Namespace, identity: LocalIdentity | None) -> int:
    by_member_id = _resolve_member_id(args.by_member_id, identity)
    if not args.request_id or not by_member_id:
        print(f"error: reject requires --request-id and --by ({_NO_IDENTITY_HINT} with --by).", file=sys.stderr)
        return 2
    request = service.reject_request(args.request_id, by_member_id)
    print(f"Rejected request {request.id!r}. status={request.status.value}")
    return 0


def _cmd_grant_revoke(service: CapacityService, args: argparse.Namespace, identity: LocalIdentity | None) -> int:
    by_member_id = _resolve_member_id(args.by_member_id, identity)
    if not by_member_id:
        print(f"error: revoke requires --by ({_NO_IDENTITY_HINT} with --by).", file=sys.stderr)
        return 2
    grant = service.revoke_grant(args.grant_id, by_member_id)
    print(f"Revoked grant {grant.id!r}. status={grant.status.value} revoked_at={grant.revoked_at.isoformat()}")
    return 0


def _cmd_capacity(service: CapacityService, args: argparse.Namespace, identity: LocalIdentity | None) -> int:
    member_id = _resolve_member_id(args.member_id, identity)
    if member_id is None:
        print(f"error: no member specified ({_NO_IDENTITY_HINT} with --member).", file=sys.stderr)
        return 1

    effective = service.get_effective_capacity(member_id, WindowType(args.window))
    print(
        f"Effective capacity for {effective.member_id!r} [{effective.window_type.value}]:\n"
        f"  base_allocation={effective.base_allocation_units}\n"
        f"  solid_sent={effective.solid_sent} solid_received={effective.solid_received}\n"
        f"  guaranteed_units={effective.guaranteed_units}\n"
        f"  shared_offered={effective.shared_offered} "
        f"shared_borrowed_potential={effective.shared_borrowed_potential} (ceiling, not guaranteed)\n"
        f"  potential_units={effective.potential_units} (upper bound, not guaranteed)"
    )
    return 0


def _cmd_login(config_path: Path, uow_factory, args: argparse.Namespace) -> int:
    identity = agent_commands.login(config_path, uow_factory, args.user_id, args.device_name)
    print(
        f"Logged in as user_id={identity.user_id!r} on device {identity.device_name!r} "
        f"(device_id={identity.device_id!r})."
    )
    print("Run `claude-share join --pool <pool_id> --member <member_id>` next to select a pool/member.")
    return 0


def _cmd_join(config_path: Path, uow_factory, args: argparse.Namespace) -> int:
    identity = agent_commands.join_pool(config_path, uow_factory, args.pool_id, args.member_id)
    print(f"Joined pool {identity.pool_id!r} as member {identity.member_id!r}.")
    return 0


def _cmd_whoami(config_path: Path, quota_service: QuotaService, capacity_service: CapacityService) -> int:
    view = agent_commands.agent_status(config_path, quota_service, capacity_service)

    if not view.logged_in:
        print("Not logged in. Run `claude-share login --user-id <user_id> --device-name <name>`.")
        return 0

    identity = view.identity
    print(f"user_id={identity.user_id} device_id={identity.device_id} device_name={identity.device_name!r}")

    if not view.joined_pool:
        print("Not joined to a pool yet. Run `claude-share join --pool <pool_id> --member <member_id>`.")
        return 0

    print(f"pool_id={identity.pool_id} member_id={identity.member_id}")
    for window_type in WindowType:
        window_status = view.member_status.windows.get(window_type)
        effective = view.effective_capacity.get(window_type)
        if window_status is not None:
            print(
                f"  [{window_type.value}] base_allocation={window_status.allocation_units} "
                f"used={window_status.used_units} remaining={window_status.remaining_units}"
            )
        if effective is not None:
            print(
                f"    guaranteed={effective.guaranteed_units} "
                f"potential={effective.potential_units} (upper bound, not guaranteed)"
            )
    return 0


def _resolve_hook_settings_path(scope: str) -> Path:
    if scope == "user":
        return Path.home() / ".claude" / "settings.json"
    return Path.cwd() / ".claude" / "settings.json"


def _cmd_hook_install(args: argparse.Namespace) -> int:
    settings_path = _resolve_hook_settings_path(args.scope)
    installed = install_hook(settings_path)
    if installed:
        print(f"Installed the Claude Share UserPromptSubmit hook into {settings_path}.")
    else:
        print(f"Claude Share hook is already installed in {settings_path} (no changes made).")
    return 0


def _cmd_hook_uninstall(args: argparse.Namespace) -> int:
    settings_path = _resolve_hook_settings_path(args.scope)
    removed = uninstall_hook(settings_path)
    if removed:
        print(f"Removed the Claude Share hook from {settings_path}.")
    else:
        print(f"No Claude Share hook entry found in {settings_path} (no changes made).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    db_path = _resolve_db_path(args.db)
    config_path = _resolve_config_path(args.config)

    try:
        if args.command == "hook" and args.hook_command == "install":
            return _cmd_hook_install(args)
        if args.command == "hook" and args.hook_command == "uninstall":
            return _cmd_hook_uninstall(args)

        quota_service, capacity_service = _build_services(db_path)
        uow_factory = lambda: SqliteUnitOfWork(db_path)

        if args.command == "login":
            return _cmd_login(config_path, uow_factory, args)
        if args.command == "join":
            return _cmd_join(config_path, uow_factory, args)
        if args.command == "whoami":
            return _cmd_whoami(config_path, quota_service, capacity_service)

        identity = load_local_identity(config_path)

        if args.command == "pool" and args.pool_command == "create":
            return _cmd_pool_create(quota_service, args)
        if args.command == "status":
            return _cmd_status(quota_service, args, identity)
        if args.command == "consume":
            return _cmd_consume(quota_service, args, identity)
        if args.command == "request":
            if args.action == "approve":
                return _cmd_request_approve(capacity_service, args, identity)
            if args.action == "reject":
                return _cmd_request_reject(capacity_service, args, identity)
            return _cmd_request_create(capacity_service, args, identity)
        if args.command == "grant" and args.grant_command == "revoke":
            return _cmd_grant_revoke(capacity_service, args, identity)
        if args.command == "capacity":
            return _cmd_capacity(capacity_service, args, identity)

        parser.error(f"unrecognized command: {args.command}")
        return 2  # pragma: no cover - argparse.error() already exits
    except (DomainError, AgentError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
