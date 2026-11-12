from __future__ import annotations

from pathlib import Path

from .util import format_ms, write_json_atomic

EXPECTED_CLIENT_CODES = {
    "COOLDOWN", "QUEUE_FULL", "RATE_LIMIT", "NOTIFICATION_BACKLOG",
    "LOGIN_TIMEOUT", "PENALTY_CONFIRMATION_REQUIRED", "HTTP_429",
}


def _table(headers: list[str], rows: list[list[str]], aligns: list[str] | None = None) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    aligns = aligns or ["<"] * len(headers)
    lines = ["  ".join(f"{headers[index]:{aligns[index]}{widths[index]}}" for index in range(len(headers)))]
    lines.append("  ".join("-" * width for width in widths))
    for row in rows:
        lines.append("  ".join(f"{cell:{aligns[index]}{widths[index]}}" for index, cell in enumerate(row)))
    return "\n".join(lines)


def render_load(snapshot: dict, *, target: str, scenario: str, players: int, duration: float, accounts: int) -> str:
    lines = []
    lines.append("LangWars stress")
    lines.append(f"target      {target}")
    lines.append(f"scenario    {scenario}")
    lines.append(f"players     {players} ({accounts} account(s))")
    lines.append(f"duration    {snapshot.get('elapsedSeconds', duration):.1f}s")
    lines.append(f"requests    {snapshot['requests']:,}  ({snapshot['requestsPerSecond']:.1f}/s)")
    lines.append(f"transport   {snapshot['transportErrors']:,}")
    lines.append(f"5xx         {snapshot['serverErrors']:,}")
    lines.append(f"4xx         {snapshot['clientErrors']:,}")
    if snapshot.get("authRelogins"):
        lines.append(f"relogins    {snapshot['authRelogins']:,}")
    lines.append("")

    ops = snapshot.get("ops") or {}
    rows = []
    for name, stats in sorted(ops.items(), key=lambda item: (-item[1]["count"], item[0])):
        if not stats["count"]:
            continue
        rows.append([
            name,
            f"{stats['count']:,}",
            f"{stats['count'] / max(0.001, snapshot['elapsedSeconds']):.1f}",
            format_ms(stats.get("p50Ms", 0)),
            format_ms(stats.get("p95Ms", 0)),
            format_ms(stats.get("p99Ms", 0)),
            f"{stats['clientErrors']:,}",
            f"{stats['serverErrors']:,}",
            f"{stats['transportErrors']:,}",
        ])
    if rows:
        lines.append(_table(
            ["operation", "count", "req/s", "p50", "p95", "p99", "4xx", "5xx", "err"],
            rows,
            ["<", ">", ">", ">", ">", ">", ">", ">", ">"],
        ))

    judge = snapshot.get("judge") or {}
    if judge.get("submitted"):
        lines.append("")
        lines.append("judging")
        lines.append(f"  enqueue attempts {judge['submitted']:,}; accepted {judge['accepted']:,}; rejected {judge['rejected']:,}")
        if judge.get("rejectionCodes"):
            codes = ", ".join(f"{name}={count:,}" for name, count in judge["rejectionCodes"].items())
            lines.append(f"  expected rejections: {codes}")
        lines.append(
            f"  completed {judge['completed']:,}; failed {judge['failed']:,}; timed out {judge['timeouts']:,}; "
            f"in flight at stop {judge.get('inFlight', 0):,}; polls {judge['polls']:,}"
        )
        if judge.get("completionRatio") is not None:
            lines.append(f"  completion ratio {judge['completionRatio'] * 100:.1f}%")
        if judge.get("verdicts"):
            verdicts = ", ".join(f"{name}={count:,}" for name, count in judge["verdicts"].items())
            lines.append(f"  verdicts: {verdicts}")
        if judge.get("errors"):
            errors = ", ".join(f"{name}={count:,}" for name, count in judge["errors"].items())
            lines.append(f"  job errors: {errors}")
        for label, key in (("end-to-end", "e2e"), ("execution", "execution"), ("queue estimate", "queueEstimate")):
            data = judge.get(key) or {}
            if data.get("count"):
                lines.append(
                    f"  {label:<14} p50={format_ms(data['p50Ms'])} "
                    f"p95={format_ms(data['p95Ms'])} p99={format_ms(data['p99Ms'])}"
                )
    return "\n".join(lines)


def load_exit_code(
    snapshot: dict,
    *,
    max_transport_rate: float,
    max_5xx_rate: float,
    min_job_completion: float | None,
    strict: bool,
) -> tuple[int, list[str]]:
    problems = []
    requests = max(1, snapshot["requests"])
    transport_rate = snapshot["transportErrors"] / requests
    server_rate = snapshot["serverErrors"] / requests
    if transport_rate > max_transport_rate:
        problems.append(f"transport error rate {transport_rate * 100:.2f}% exceeds {max_transport_rate * 100:.2f}%")
    if server_rate > max_5xx_rate:
        problems.append(f"5xx rate {server_rate * 100:.2f}% exceeds {max_5xx_rate * 100:.2f}%")
    judge = snapshot.get("judge") or {}
    completion = judge.get("completionRatio")
    if min_job_completion is not None and completion is not None and completion < min_job_completion:
        problems.append(f"job completion ratio {completion * 100:.1f}% is below {min_job_completion * 100:.1f}%")
    unexpected = 0
    for stats in (snapshot.get("ops") or {}).values():
        for code, count in stats.get("codes", {}).items():
            if code not in EXPECTED_CLIENT_CODES and not code.startswith("TRANSPORT:"):
                unexpected += count
    if strict and unexpected:
        problems.append(f"{unexpected} unexpected client error(s) in strict mode")
    return (1 if problems else 0), problems


def print_probe(snapshot: dict, *, target: str, clients: int) -> int:
    print(render_load(snapshot, target=target, scenario="probe (anonymous reads)", players=clients, duration=snapshot["elapsedSeconds"], accounts=0))
    bad = snapshot["serverErrors"] + snapshot["transportErrors"] + snapshot["clientErrors"]
    if bad:
        print(f"\n{bad} unexpected response(s)")
        return 1
    return 0


def print_smoke(checks) -> int:
    failed = 0
    for check in checks:
        if check.ok and check.warning:
            status = "WARN"
        elif check.ok:
            status = "PASS"
        else:
            status = "FAIL"
            failed += 1
        line = f"{status:<4}  {check.name:<28} {check.detail}"
        if check.warning:
            line += f"  [{check.warning}]"
        print(line)
    print(f"\n{len(checks) - failed}/{len(checks)} checks passed")
    return 1 if failed else 0


def print_abuse(results) -> int:
    failed = [item for item in results if not item.ok and not item.skipped]
    skipped = [item for item in results if item.skipped]
    by_name: dict[str, list] = {}
    for item in results:
        by_name.setdefault(item.name, []).append(item)
    print("LangWars abuse probes")
    print(f"probes {len(results):,}; failed {len(failed):,}; skipped {len(skipped):,}")
    rows = []
    for name, items in sorted(by_name.items()):
        bad = sum(1 for item in items if not item.ok and not item.skipped)
        skip = sum(1 for item in items if item.skipped)
        rows.append([name, f"{len(items):,}", f"{bad:,}", f"{skip:,}"])
    print()
    print(_table(["probe", "runs", "failed", "skipped"], rows, ["<", ">", ">", ">"]))
    if failed:
        print("\nfailures")
        for item in failed[:40]:
            print(f"  {item.name}: expected {item.expected}; got {item.actual} {item.detail}".rstrip())
        if len(failed) > 40:
            print(f"  ... {len(failed) - 40} more")
    return 1 if failed else 0


def artifact(path: Path, payload: dict) -> None:
    write_json_atomic(path, payload)
    print(f"\nreport: {path}")
