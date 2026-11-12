from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from . import __version__
from .accounts import (
    AccountStore,
    AuthError,
    ProvisionError,
    cleanup_accounts,
    default_accounts_path,
    provision_accounts,
)
from .report import (
    artifact,
    load_exit_code,
    print_abuse,
    print_probe,
    print_smoke,
    render_load,
)
from .smoke import run_smoke
from .util import eprint, utc_now
from .workload import LoadSettings, run_load, run_probe

DEFAULT_URL = "http://127.0.0.1:8080"


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--url", default=os.environ.get("LANGSWARS_URL", DEFAULT_URL),
                        help="base URL of the deployment (default: %(default)s)")
    parser.add_argument("--accounts", default=os.environ.get("LANGSWARS_ACCOUNTS"),
                        help="account/token state file (default: stress/state/accounts-<host>.json)")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds")
    parser.add_argument("--insecure", action="store_true", help="do not verify TLS certificates")
    parser.add_argument("--ca-bundle", help="PEM file with a private CA")
    parser.add_argument("--verbose", action="store_true", help="print extra progress information")
    parser.add_argument("--json-out", help="write a JSON report to this path (use '-' for no report)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stress/run.py",
        description="Player-workload and robustness tests for a remote LangWars deployment.",
    )
    parser.add_argument("--version", action="version", version=f"langwars-stress {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    provision = subparsers.add_parser("provision", help="create or verify player accounts (uses admin once)")
    _common(provision)
    provision.add_argument("--players", type=int, default=100, help="number of player accounts to ensure")
    provision.add_argument("--prefix", default="st", help="generated username prefix")
    provision.add_argument("--invite-code", default=os.environ.get("LANGSWARS_INVITE_CODE"),
                           help="invitation code; avoids needing administrator credentials")
    provision.add_argument("--admin-username", default=os.environ.get("LANGSWARS_ADMIN_USERNAME"))
    provision.add_argument("--admin-password-file", help="file containing the administrator password")
    provision.add_argument("--min-interval", type=float, default=0.0,
                           help="seconds between registrations (auto-raised after a 429)")
    provision.add_argument("--skip-verify", action="store_true", help="do not re-check existing sessions")

    smoke = subparsers.add_parser("smoke", help="functional end-to-end checks for one player session")
    _common(smoke)
    smoke.add_argument("--write", action="store_true", help="also submit a solution (publishes to the shared world)")
    smoke.add_argument("--task", help="prefer this task id or slug")
    smoke.add_argument("--strict-world", action="store_true",
                       help="fail if the world version changes during a run (only safe on an idle deployment)")

    load = subparsers.add_parser("load", help="run a many-player workload")
    _common(load)
    load.add_argument("--scenario", choices=["browse", "judge", "run", "submit", "mixed", "auth"], default="mixed")
    load.add_argument("--players", type=int, default=50)
    load.add_argument("--duration", type=float, default=120.0, help="seconds of measured load")
    load.add_argument("--ramp-up", type=float, default=10.0, help="seconds over which players start")
    load.add_argument("--think-time", type=float, default=3.0, help="mean player pause between actions")
    load.add_argument("--seed", type=int, default=int(time.time()) & 0x7FFFFFFF)
    load.add_argument("--task", action="append", default=[], help="restrict to a task id/slug (repeatable)")
    load.add_argument("--job-timeout", type=float, default=90.0, help="maximum seconds to wait for one job")
    load.add_argument("--poll-interval", type=float, default=0.5, help="mean seconds between job polls")
    load.add_argument("--no-preflight", action="store_true", help="skip the session check before load")
    load.add_argument("--allow-fewer", action="store_true", help="use fewer accounts than --players if necessary")
    load.add_argument("--shadow-group-size", type=int, default=4,
                      help="players sharing and rotating one identifier block; 1 disables shadowing")
    load.add_argument("--core-override-every", type=int, default=5,
                      help="every Nth player may redefine permitted core operations; 0 disables")
    load.add_argument("--max-transport-error-rate", type=float, default=0.01)
    load.add_argument("--max-5xx-rate", type=float, default=0.005)
    load.add_argument("--no-completion-threshold", action="store_true", help="do not fail when jobs time out")
    load.add_argument("--strict", action="store_true", help="fail on any unexpected 4xx response")

    probe = subparsers.add_parser("probe", help="anonymous read-only concurrent probe")
    _common(probe)
    probe.add_argument("--clients", type=int, default=50)
    probe.add_argument("--requests", type=int, default=300)
    probe.add_argument("--duration", type=float, default=0.0, help="use a fixed duration instead of a request count")

    abuse = subparsers.add_parser("abuse", help="player-side malformed and adversarial requests")
    _common(abuse)
    abuse.add_argument("--workers", type=int, default=2)
    abuse.add_argument("--iterations", type=int, default=5, help="cycles per worker")
    abuse.add_argument("--think-time", type=float, default=2.0)

    cleanup = subparsers.add_parser("cleanup", help="delete provisioned test users (uses admin)")
    _common(cleanup)
    cleanup.add_argument("--yes", action="store_true", help="confirm deletion of the accounts in the state file")
    cleanup.add_argument("--admin-username", default=os.environ.get("LANGSWARS_ADMIN_USERNAME"))
    cleanup.add_argument("--admin-password-file", help="file containing the administrator password")

    return parser


def _accounts_path(args) -> Path:
    if args.accounts:
        return Path(args.accounts)
    return default_accounts_path(args.url)


def _load_accounts(args, *, required: bool = True):
    path = _accounts_path(args)
    store = AccountStore(path, args.url)
    accounts = store.load()
    if required and not accounts:
        raise ProvisionError(f"no accounts in {path}; run the provision command first")
    return store, accounts


def _artifact_path(args, label: str) -> Path | None:
    if getattr(args, "json_out", None) == "-":
        return None
    if getattr(args, "json_out", None):
        return Path(args.json_out)
    root = Path(__file__).resolve().parent.parent
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    return root / "results" / f"{stamp}-{label}.json"


def _write_report(args, label: str, payload: dict) -> None:
    path = _artifact_path(args, label)
    if path is not None:
        artifact(path, payload)


def _base_payload(args, label: str) -> dict:
    return {
        "pack": "langwars-stress",
        "packVersion": __version__,
        "command": label,
        "target": args.url,
        "createdAt": utc_now(),
    }


def _cmd_provision(args) -> int:
    if args.players < 1 or args.players > 10_000:
        raise ProvisionError("--players must be between 1 and 10000")
    path = _accounts_path(args)
    accounts = provision_accounts(
        base_url=args.url,
        path=path,
        count=args.players,
        prefix=args.prefix,
        timeout=args.timeout,
        insecure=args.insecure,
        ca_bundle=args.ca_bundle,
        invite_code=args.invite_code,
        admin_username=args.admin_username,
        admin_password_file=args.admin_password_file,
        min_interval=max(0.0, args.min_interval),
        skip_verify=args.skip_verify,
    )
    payload = _base_payload(args, "provision")
    payload.update({"requestedAccounts": args.players, "accountCount": len(accounts)})
    _write_report(args, "provision", payload)
    return 0


def _cmd_smoke(args) -> int:
    store, accounts = _load_accounts(args)
    if not accounts:
        raise ProvisionError("no accounts; run provision first")
    checks = run_smoke(
        accounts[0],
        accounts[1] if len(accounts) > 1 else None,
        base_url=args.url,
        timeout=args.timeout,
        insecure=args.insecure,
        ca_bundle=args.ca_bundle,
        write=args.write,
        strict_world=args.strict_world,
        task_key_wanted=args.task,
    )
    code = print_smoke(checks)
    store.save(accounts)
    payload = _base_payload(args, "smoke")
    payload["checks"] = [check.__dict__ for check in checks]
    _write_report(args, "smoke", payload)
    return code


def _cmd_load(args) -> int:
    if args.players < 1 or args.players > 10_000:
        raise ProvisionError("--players must be between 1 and 10000")
    store, accounts = _load_accounts(args)
    if not accounts:
        raise ProvisionError("no accounts; run provision first")
    settings = LoadSettings(
        base_url=args.url,
        scenario=args.scenario,
        players=args.players,
        duration=max(1.0, args.duration),
        ramp_up=max(0.0, args.ramp_up),
        think_time=max(0.0, args.think_time),
        request_timeout=args.timeout,
        insecure=args.insecure,
        ca_bundle=args.ca_bundle,
        seed=args.seed,
        job_timeout=max(5.0, args.job_timeout),
        poll_interval=max(0.05, args.poll_interval),
        task_keys=list(args.task),
        preflight=not args.no_preflight,
        allow_fewer=args.allow_fewer,
        shadow_group_size=max(1, args.shadow_group_size),
        core_override_every=max(0, args.core_override_every),
    )
    recorder, used = run_load(accounts, settings)
    snapshot = recorder.snapshot()
    print()
    print(render_load(snapshot, target=args.url, scenario=settings.scenario, players=settings.players,
                      duration=settings.duration, accounts=len(used)))
    store.save(accounts)
    code, problems = load_exit_code(
        snapshot,
        max_transport_rate=max(0.0, args.max_transport_error_rate),
        max_5xx_rate=max(0.0, args.max_5xx_rate),
        min_job_completion=None if args.no_completion_threshold else 0.95,
        strict=args.strict,
    )
    if problems:
        print("\nthreshold failures:")
        for problem in problems:
            print(f"  - {problem}")
    payload = _base_payload(args, "load")
    payload.update({
        "scenario": settings.scenario,
        "requestedPlayers": args.players,
        "usedPlayers": len(used),
        "durationSeconds": args.duration,
        "rampUpSeconds": args.ramp_up,
        "thinkTimeSeconds": args.think_time,
        "seed": args.seed,
        "tasks": settings.task_keys,
        "shadowGroupSize": settings.shadow_group_size,
        "coreOverrideEvery": settings.core_override_every,
        "coreOverrides": settings.override_ops,
        "metrics": snapshot,
        "problems": problems,
    })
    _write_report(args, "load", payload)
    return code


def _cmd_probe(args) -> int:
    if args.clients < 1 or args.clients > 2000:
        raise ProvisionError("--clients must be between 1 and 2000")
    if args.requests < 1 or args.requests > 1_000_000:
        raise ProvisionError("--requests must be between 1 and 1000000")
    recorder = run_probe(
        base_url=args.url,
        clients=args.clients,
        requests=args.requests,
        duration=max(0.0, args.duration),
        request_timeout=args.timeout,
        insecure=args.insecure,
        ca_bundle=args.ca_bundle,
    )
    snapshot = recorder.snapshot()
    code = print_probe(snapshot, target=args.url, clients=args.clients)
    payload = _base_payload(args, "probe")
    payload.update({"clients": args.clients, "requests": args.requests, "duration": args.duration, "metrics": snapshot})
    _write_report(args, "probe", payload)
    return code


def _cmd_abuse(args) -> int:
    from .probes import run_abuse

    store, accounts = _load_accounts(args)
    if not accounts:
        raise ProvisionError("no accounts; run provision first")
    results = run_abuse(
        accounts,
        base_url=args.url,
        workers=max(1, args.workers),
        iterations=max(1, args.iterations),
        think_time=max(0.0, args.think_time),
        request_timeout=args.timeout,
        insecure=args.insecure,
        ca_bundle=args.ca_bundle,
        verbose=args.verbose,
    )
    code = print_abuse(results)
    store.save(accounts)
    payload = _base_payload(args, "abuse")
    payload["results"] = [result.__dict__ for result in results]
    _write_report(args, "abuse", payload)
    return code


def _cmd_cleanup(args) -> int:
    if not args.yes:
        raise ProvisionError("cleanup deletes accounts; pass --yes to confirm")
    path = _accounts_path(args)
    deleted, missing = cleanup_accounts(
        base_url=args.url,
        path=path,
        timeout=args.timeout,
        insecure=args.insecure,
        ca_bundle=args.ca_bundle,
        admin_username=args.admin_username,
        admin_password_file=args.admin_password_file,
    )
    print(f"deleted {deleted} account(s); {missing} already absent or unknown; state file updated")
    payload = _base_payload(args, "cleanup")
    payload.update({"deleted": deleted, "missing": missing})
    _write_report(args, "cleanup", payload)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "provision":
            return _cmd_provision(args)
        if args.command == "smoke":
            return _cmd_smoke(args)
        if args.command == "load":
            return _cmd_load(args)
        if args.command == "probe":
            return _cmd_probe(args)
        if args.command == "abuse":
            return _cmd_abuse(args)
        if args.command == "cleanup":
            return _cmd_cleanup(args)
        parser.error("unknown command")
        return 2
    except (ProvisionError, AuthError, RuntimeError) as error:
        eprint(f"error: {error}")
        return 2
    except KeyboardInterrupt:
        eprint("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
