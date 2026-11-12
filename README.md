# LangWars stress pack

A local, dependency-free workload generator for a **remote** LangWars deployment.
It models many independent players: each one has a session, browses tasks and
rankings, reads the shared namespace and notifications, and periodically runs or
submits LWLang solutions while polling its own jobs.

The pack deliberately **does not model administrator traffic**. Administrator
credentials are used only by `provision` (to create an invitation when the
deployment is invite-only) and by the optional `cleanup` command. Every load,
smoke and abuse scenario talks to player endpoints only.

> **This is a destructive-looking test tool for a shared game.** Submissions are
> real: accepted code publishes names into the shared world, changes world
> versions, can trigger rechecks, and affects ratings. Run it against a dedicated
> test deployment or a maintenance window, never against an event with real
> players. Use `--scenario browse` or `probe` for read-only checks.

The scripts are standalone files. They do not run git commands, install
dependencies, or modify the service repository.

## Requirements

- Python 3.10 or newer. Only the standard library is used.
- Network access from this machine to the deployment's API (the same origin the
  browser uses, for example `https://langwars.example.org`).
- A dedicated player account pool. The tool creates one for you; see
  [Provisioning](#provisioning).

## Quick start

Run these commands from the workspace root (the parent of `stress/`).

### 1. Get player accounts

```powershell
$env:LANGSWARS_URL = "https://langwars.example.org"
$env:LANGSWARS_ADMIN_USERNAME = "admin"
python stress/run.py provision --players 200
```

If `LANGSWARS_ADMIN_PASSWORD` is not set, `provision` prompts for it without
echoing. The password is never accepted as a command-line argument and is never
printed. If you already have an invitation code, you can avoid administrator
credentials entirely:

```powershell
python stress/run.py provision --players 200 --invite-code "<invitation token>"
```

Accounts, passwords and session tokens are written to
`stress/state/accounts-<host>.json` with owner-only permissions where supported.
This file is a test secret: keep it out of source control. Existing accounts are
reused, and `provision` refreshes expired sessions.

### 2. Check the deployment

```powershell
python stress/run.py smoke
```

Without `--write`, smoke uses runs only, so it does not publish to the world.
Add `--write` to also verify a real `submit`, publication and code privacy.

### 3. Run a player workload

```powershell
# Read-heavy: no submissions or world changes (an occasional profile edit only).
python stress/run.py load --scenario browse --players 200 --duration 300

# Realistic mix of browsing, runs, submissions, notifications and history.
python stress/run.py load --scenario mixed --players 200 --duration 600 --think-time 3

# Tune shadowing and core redefinitions explicitly.
python stress/run.py load --scenario judge --players 100 --duration 300 \
  --shadow-group-size 4 --core-override-every 5

# Judge-heavy: short, cheap solutions and their result polling.
python stress/run.py load --scenario judge --players 100 --duration 300 --think-time 2
```

The default cooldowns still apply: a player can run once every 2 seconds and
submit once every 10 seconds until an administrator changes those settings.
With 200 players those limits still allow thousands of judge requests per minute.
Generate load with more **players**, not by hammering a single account. The pack
never changes server settings to bypass cooldowns.

Code-bearing scenarios generate player code from the deployment's allowed dictionary.
Identifiers are deliberately neither unique per player nor identical for
everyone: a shadow group shares one block of names and rotates it between its
members, so submissions overwrite and shadow each other's globals. This is
normal LangWars behaviour and can turn earlier submissions `BROKEN` during
rechecks. In `judge`, `run`, `submit` and `mixed`, every fifth player by default
also republishes a permitted core operation (`eq`, `get` or `concat`) as a
contract-safe wrapper around its `__builtin_*__` form. The pack probes those
operations with a player `run` first; it does not use administrator endpoints.
Disable either feature with `--shadow-group-size 1` or
`--core-override-every 0`. Each selected player publishes as many different
overrides as the task permits, using the server default of two when
`coreOverrideLimit` is absent.

### 4. Run adversarial checks

`abuse` is a separate negative-path command, not a `load --scenario`. It uses the
first two provisioned player accounts and sends malformed, oversized and
unauthorized requests, asserting that the API rejects each one with the expected
status and error code:

```powershell
$env:LANGSWARS_URL = "https://langwars.example.org"
python stress/run.py abuse --workers 2 --iterations 5 --think-time 2
```

Options:

- `--workers` — parallel probe workers (default `2`);
- `--iterations` — cycles per worker (default `5`);
- `--think-time` — seconds between cycles (default `2`);
- `--json-out -` — skip the JSON report, for example in CI.

It needs the accounts created by `provision`, but no administrator credentials.
It does not submit solutions or publish to the world; the only job it may enqueue
is a short `run` used to verify that one player cannot read another player's job.
Cooldown rejections are reported as skipped probes rather than failures. The
command exits non-zero only for real mismatches:

```powershell
python stress/run.py abuse --workers 2 --iterations 3 --think-time 2 --json-out -
```

### 5. Optional cleanup

```powershell
$env:LANGSWARS_ADMIN_PASSWORD = "..."
python stress/run.py cleanup --yes
```

This deletes only the users recorded in the state file. Published definitions
and other players' data are untouched; deleting users is an administrative
action and is not part of any load scenario.

## Commands

| Command | Purpose | Writes world? |
| --- | --- | --- |
| `provision` | Create/verify player accounts; uses admin only when inviting | No |
| `smoke` | End-to-end functional checks for one player session | Only with `--write` |
| `load` | Multi-player workload (`browse`, `judge`, `run`, `submit`, `mixed`, `auth`) | `run`/`browse` never publish; `judge`/`submit`/`mixed` submit real solutions |
| `probe` | Anonymous concurrent reads of public endpoints | No |
| `abuse` | Player-side malformed, oversized and unauthorized requests | Only one queued run for the privacy probe |
| `cleanup` | Delete the accounts recorded in the state file (admin) | No |

## CLI arguments

Arguments must follow the subcommand, for example
`python stress/run.py load --url https://host --players 100`. `-h`/`--help`
prints the same information for any subcommand, and the top-level `--version`
prints the pack version. Administrator passwords are intentionally not accepted
as command-line arguments.

### Common to every subcommand

| Argument | Default | Description |
| --- | --- | --- |
| `--url URL` | `LANGSWARS_URL` or `http://127.0.0.1:8080` | Base URL of the deployment. A path prefix is supported; query and fragment are not. |
| `--accounts PATH` | `LANGSWARS_ACCOUNTS`, or `stress/state/accounts-<url-slug>.json` | Account/token state file. The generated slug includes scheme, host, port and any path prefix. |
| `--timeout SECONDS` | `30.0` | Per-request HTTP timeout. Keep it above the server's slowest expected response. |
| `--insecure` | off | Disable TLS certificate verification. Use only for a disposable test deployment. Takes precedence over `--ca-bundle`. |
| `--ca-bundle PATH` | off | PEM file with a private CA certificate for TLS verification. |
| `--verbose` | off | Print additional diagnostics. Currently used mainly by `abuse` to echo skipped/failed probes. |
| `--json-out PATH` | `stress/results/<timestamp>-<command>.json` | Where to write the JSON report. Use `--json-out -` to suppress the report entirely. |

### `provision`

| Argument | Default | Description |
| --- | --- | --- |
| `--players N` | `100` | Number of usable player accounts to ensure. Must be 1–10000. Existing accounts are kept and verified. |
| `--prefix TEXT` | `st` | Generated username prefix. 1–12 ASCII letters, digits or underscores. |
| `--invite-code CODE` | `LANGSWARS_INVITE_CODE` | Existing invitation token. Supply this to provision without administrator credentials while the invitation remains valid. |
| `--admin-username NAME` | `LANGSWARS_ADMIN_USERNAME`, else `admin` | Administrator login used only when an invitation must be created. |
| `--admin-password-file PATH` | off | File whose first line is the administrator password. Otherwise `LANGSWARS_ADMIN_PASSWORD` is used, then an interactive hidden prompt. |
| `--min-interval SECONDS` | `0.0` | Delay between registration requests. May be raised automatically after a `429`. |
| `--skip-verify` | off | Do not check existing account sessions. New accounts are still registered; `load` can refresh sessions later. |

`provision` reads `/api/config` to discover the registration mode. In
`invite` mode it creates a reusable invitation when needed; in `open` mode it
registers directly; in `closed` mode it exits with an error.

### `smoke`

| Argument | Default | Description |
| --- | --- | --- |
| `--write` | off | Also submit a correct solution. Without it, smoke uses runs only and does not publish to the shared world. |
| `--task ID_OR_SLUG` | first task | Prefer this task for the run/submit checks. Falls back to the normal task order if it is absent. |
| `--strict-world` | off | Fail if the world version changes during the "run does not publish" check. Use only on an idle deployment. |

`smoke` uses the first account in the state file and the second account (when
present) for job and submission privacy checks.

### `load`

| Argument | Default | Description |
| --- | --- | --- |
| `--scenario NAME` | `mixed` | One of `browse`, `judge`, `run`, `submit`, `mixed`, `auth`. |
| `--players N` | `50` | Number of simulated players/accounts. Must be 1–10000. If the state file has fewer usable accounts, load fails unless `--allow-fewer` is set. |
| `--duration SECONDS` | `120.0` | Measured load duration. Values below 1 are raised to 1. |
| `--ramp-up SECONDS` | `10.0` | Time over which players start. Values below 0 are raised to 0. |
| `--think-time SECONDS` | `3.0` | Mean lognormal pause between player actions. `0` removes the pause. |
| `--seed INTEGER` | time-based | Seed for task choice, think times, shadow rotation and probe selection. Use a fixed value for reproducibility. |
| `--task ID_OR_SLUG` | known default tasks | Restrict load to a task. Repeat the option for several tasks. A value that matches nothing is an error. |
| `--job-timeout SECONDS` | `90.0` | Maximum time to wait for one job. Values below 5 are raised to 5. |
| `--poll-interval SECONDS` | `0.5` | Mean delay between job-status polls. Values below 0.05 are raised to 0.05. |
| `--no-preflight` | off | Skip the `/api/me` session check before load. Invalid tokens are then discovered by the players themselves. |
| `--allow-fewer` | off | Use the usable accounts that exist instead of failing when there are fewer than `--players`. |
| `--shadow-group-size N` | `4` | Players sharing and rotating one identifier block. `1` disables sharing and gives each player a separate block. |
| `--core-override-every N` | `5` | Every Nth player republishes permitted core operations on every selected task, up to that task's override limit (default `2`). `0` disables overrides. |
| `--max-transport-error-rate FRACTION` | `0.01` | Fail above this fraction of transport errors (1%). |
| `--max-5xx-rate FRACTION` | `0.005` | Fail above this fraction of HTTP 5xx responses (0.5%). |
| `--no-completion-threshold` | off | Disable the default requirement that at least 95% of finished judge jobs completed. |
| `--strict` | off | Also fail on any unexpected 4xx response. Expected cooldown, queue and rate-limit rejections are always allowed. |

`browse` never submits or publishes. `run` executes visible tests without
publishing. `judge`, `submit` and `mixed` publish real submissions. `auth`
cycles login/logout and does not generate solution code.

### `probe`

| Argument | Default | Description |
| --- | --- | --- |
| `--clients N` | `50` | Number of concurrent anonymous readers. Must be 1–2000. |
| `--requests N` | `300` | Total requests when `--duration` is 0. Must be 1–1000000. |
| `--duration SECONDS` | `0.0` | When greater than 0, run for this long and ignore `--requests`. |

`probe` uses only public read endpoints and does not read the account state file.

### `abuse`

| Argument | Default | Description |
| --- | --- | --- |
| `--workers N` | `2` | Parallel probe workers. Must be at least 1. |
| `--iterations N` | `5` | Probe cycles per worker. Must be at least 1. |
| `--think-time SECONDS` | `2.0` | Pause between cycles in each worker. Values below 0 are raised to 0. |

`abuse` uses the first two accounts in the state file and does not use
administrator credentials. It asserts exact status/error codes; cooldown skips
are reported separately and do not fail the command.

### `cleanup`

| Argument | Default | Description |
| --- | --- | --- |
| `--yes` | required | Confirm deletion. Without it, cleanup refuses to run. |
| `--admin-username NAME` | `LANGSWARS_ADMIN_USERNAME`, else `admin` | Administrator login used for deletion. |
| `--admin-password-file PATH` | off | File whose first line is the administrator password. Otherwise `LANGSWARS_ADMIN_PASSWORD` is used, then an interactive hidden prompt. |

Cleanup deletes only accounts recorded in the state file, then removes those
entries from the file. Published world definitions remain.

### Environment variables

| Variable | Used as |
| --- | --- |
| `LANGSWARS_URL` | Default for `--url`. |
| `LANGSWARS_ACCOUNTS` | Default for `--accounts`. |
| `LANGSWARS_ADMIN_USERNAME` | Default for `--admin-username`. |
| `LANGSWARS_ADMIN_PASSWORD` | Administrator password when `--admin-password-file` is absent. Never printed. |
| `LANGSWARS_INVITE_CODE` | Default for `--invite-code`. |

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Command completed and all checks/thresholds passed. |
| `1` | A smoke/abuse check failed, or a load/probe threshold was exceeded. |
| `2` | Usage, configuration, authentication or setup error. |
| `130` | Interrupted with Ctrl-C. |

## Workload model

Each simulated player is one OS thread with its own keep-alive HTTP connection
and its own account. Players start over a configurable ramp-up, pause by a
lognormal think time, and choose actions from a scenario-specific mix.

| Scenario | Typical actions |
| --- | --- |
| `browse` | Task list/detail, namespace, global and per-task rankings, news, own history, task history, notifications, insights, occasional display-name edit |
| `judge` | Runs and submissions (mostly correct, some starter-code wrong answers), job polling, light browsing |
| `run` | Runs only (visible examples, no publication) plus light reads |
| `submit` | Submissions only plus light reads; handles the resubmit-penalty confirmation |
| `mixed` | Browsing plus runs, submissions, notification read/create and history |
| `auth` | Login, `/api/me`, logout cycles; expects edge/account rate limits |
| `probe` | Anonymous reads only |
| `abuse` | Validation, authentication and authorization probes with expected status codes |

For `judge`, `run`, `submit` and `mixed`, every player gets differently named
functions and variables. Players are allocated in shadow groups of four by
default: members share a name block and rotate the mapping, so a name used for a
function by one player can be a parameter or local for another. Different groups
get different blocks. Selected players also republish core overrides on every
selected task; all tasks are assumed to permit them. The load
report therefore contains a realistic mix of `OK`, `WRONG_ANSWER`, `CE` and
`BROKEN` outcomes rather than only successful submissions.

Player behavior includes the things a browser does around a job: it polls
`GET /api/jobs/{id}` until the worker finishes, reads its submission detail on
success, and backs off when the API returns `COOLDOWN`, `QUEUE_FULL`,
`RATE_LIMIT` or `NOTIFICATION_BACKLOG`. A player that loses a session re-logs in
once and retries the request that failed authorization.

The judge collector reports:

- enqueue attempts, accepted jobs and expected admission rejections;
- completed, failed and timed-out jobs;
- verdict distribution;
- end-to-end time from `POST` to terminal job state;
- interpreter execution time from the result, and the estimated queue/transport
  share (`end-to-end - execution`).

## Rate limits and provisioning

The stock deployment limits `/api/auth/login` and `/api/auth/register` to about
60 requests per minute per client IP at the nginx edge, with a burst. It also has
a 600/minute per-IP backend budget and a 15/minute per-account login budget, plus
scrypt password hashing. Provisioning many accounts through the public edge is
therefore intentionally slow, especially on the first run:

- `provision` spaces registrations and automatically slows down after a `429`.
- Session tokens last `SESSION_TTL_HOURS` (seven days by default). Reusing the
  same state file avoids paying the registration cost again.
- For a large pool, the fastest option is to run provisioning from the private
  network, or to generate one reusable invitation and provision from a host that
  is not behind the public auth limit. Do not remove the auth rate limit just for
  the test.
- `load` preflights stored sessions; expired sessions are refreshed sequentially
  to avoid a login storm. It fails early with a clear message if you ask for more
  players than there are usable accounts (use `--allow-fewer` to override).

## Interpreting results

The console prints per-operation request counts, throughput, latency percentiles
and error counts, followed by judge metrics. Reports are JSON and contain the
same aggregates plus a one-second timeline; they do not contain response bodies,
tokens, passwords or private code.

Default failure thresholds for `load`:

- transport errors at most 1% of requests;
- 5xx responses at most 0.5% of requests;
- at least 95% of finished judge jobs completed (disable with
  `--no-completion-threshold`);
- `--strict` additionally fails on any unexpected 4xx response.

Expected admission rejections (`COOLDOWN`, `QUEUE_FULL`, `RATE_LIMIT`,
`NOTIFICATION_BACKLOG`, `LOGIN_TIMEOUT`, `PENALTY_CONFIRMATION_REQUIRED`) are
listed separately and do not by themselves fail the run. They are part of the
game's normal backpressure. A high queue estimate with no 5xx generally means
the worker pool is saturated; add workers or reduce judge concurrency.

`smoke` prints one line per check and exits non-zero if any check fails. Run it
against an idle deployment when using `--strict-world`, because other players
publishing simultaneously will change the world version during a run. The
default treats that as an inconclusive warning.

`abuse` exits non-zero only for real mismatches. A job-privacy probe that cannot
enqueue because of a cooldown is reported as skipped. Repeated failures usually
mean the deployment returns a different error code than this release, which is
worth investigating before treating it as a load result.

## Checks for the pack itself

The workload code has no network dependency in its unit tests:

```sh
python -m unittest discover -s stress/tests -v
python -m compileall -q stress
```

## Notes and limitations

- HTTP latency includes the network path; this is an external black-box test,
  not an in-process benchmark.
- The worker queue is asynchronous. A `202` response measures admission, not
  execution. End-to-end timings include queue wait and polling granularity.
- The pack measures player-facing behavior. It does not measure browser rendering,
  WebSocket traffic (the service has none), PostgreSQL internals, or administrator
  operations.
- Name shadowing is intentional. Accepted submissions can be rechecked and turn
  `BROKEN` when another player publishes a colliding name. This is expected game
  behaviour, not a stress-pack error. Use `--shadow-group-size 1` for isolated
  names.
- Core overrides are contract-safe wrappers around the built-in operation. They
  intentionally trigger recheck sweeps but do not change operation semantics. If
  an administrator disables an operation, the pack detects that during its player
  probe and does not use it.
- The pack is not a security scanner. The abuse probes cover known validation and
  authorization boundaries; they are not a substitute for the server test suite.
- Default task solutions are copied from the public default fixtures. Custom or
  renamed tasks fall back to their starter code, which produces wrong answers but
  still exercises the queue and interpreter.
- A very large `--players` value creates one thread and one connection per player.
  Keep it within the deployment's connection budget; start around 50-200 and scale
  with measured queue latency.
- Submissions change the shared world permanently until superseded, restored or
  deleted by an administrator. Use a dedicated environment.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `administrator login was rejected` | Wrong username. The Compose example uses `admin`; set `LANGSWARS_ADMIN_USERNAME` to the real bootstrap login |
| `registration is closed` | The deployment is not accepting new accounts; ask an administrator or use an existing account file |
| `INVALID_INVITE` | The supplied invitation expired or was consumed; use admin credentials or a new link |
| Many `429` from `auth` scenario | Expected at the public edge; lower the login rate or run from a trusted network |
| `no usable accounts` | Run `provision --players N` and keep the state file |
| TLS certificate errors | Use a trusted CA with `--ca-bundle`, or `--insecure` for a disposable test deployment only |
| `job completion ratio` failure | Workers are overloaded, stopped, or jobs are timing out; inspect server and worker logs and queue depth |
