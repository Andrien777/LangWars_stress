from __future__ import annotations

import random
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import quote

from .accounts import Account, AuthError, ProvisionError, RateLimited, login_account, preflight_accounts
from .http import Client, Response, query
from .metrics import Recorder
from .solutions import DEFAULT_TASKS, TASK_SOLUTIONS, correct_source, task_key, wrong_source
from .util import eprint, jitter
from .variants import OVERRIDE_CANDIDATES, CodeBook, PlayerCode


@dataclass
class LoadSettings:
    base_url: str
    scenario: str = "mixed"
    players: int = 10
    duration: float = 60.0
    ramp_up: float = 5.0
    think_time: float = 4.0
    request_timeout: float = 30.0
    insecure: bool = False
    ca_bundle: str | None = None
    seed: int = 1
    job_timeout: float = 90.0
    poll_interval: float = 0.5
    task_keys: list[str] = field(default_factory=list)
    preflight: bool = True
    allow_fewer: bool = False
    shadow_group_size: int = 4
    core_override_every: int = 5
    override_ops: list[str] = field(default_factory=list)


@dataclass
class JobOutcome:
    state: str
    payload: dict | None = None
    error_code: str | None = None
    polls: int = 0
    elapsed_ms: float = 0.0
    verdict: str | None = None
    execution_ms: float = 0.0
    submission_id: str | None = None
    rating_delta: float | None = None


def _execution_ms(payload: dict) -> float:
    submission = payload.get("submission")
    if isinstance(submission, dict):
        value = submission.get("durationMs")
        if isinstance(value, (int, float)):
            return float(value)
    tests = payload.get("tests")
    if isinstance(tests, list):
        return float(sum(item.get("durationMs", 0) for item in tests if isinstance(item, dict) and isinstance(item.get("durationMs", 0), (int, float))))
    return 0.0


def _outcome(payload: dict, elapsed_ms: float, polls: int) -> JobOutcome:
    # GET /api/jobs/{id} wraps the judge result as {"status":"completed","result":{...}}.
    result = payload.get("result")
    if not isinstance(result, dict):
        result = payload
    submission = result.get("submission")
    if isinstance(submission, dict):
        rating = result.get("ratingUpdate")
        delta = rating.get("delta") if isinstance(rating, dict) else None
        return JobOutcome(
            state="completed",
            payload=result,
            polls=polls,
            elapsed_ms=elapsed_ms,
            verdict=submission.get("status"),
            execution_ms=_execution_ms(result),
            submission_id=submission.get("id"),
            rating_delta=float(delta) if isinstance(delta, (int, float)) else None,
        )
    return JobOutcome(
        state="completed",
        payload=result,
        polls=polls,
        elapsed_ms=elapsed_ms,
        verdict=result.get("status"),
        execution_ms=_execution_ms(result),
    )


def poll_job(
    client: Client,
    token: str,
    job_id: str,
    *,
    started_at: float,
    timeout: float,
    interval: float,
    stop: threading.Event | None,
    recorder: Recorder | None,
    rng,
) -> JobOutcome:
    """Poll one job; returns state ``unauthorized`` if the caller must log in again."""
    deadline = time.monotonic() + timeout
    polls = 0
    while True:
        if stop is not None and stop.is_set():
            return JobOutcome("stopped", polls=polls, elapsed_ms=(time.monotonic() - started_at) * 1000)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if recorder:
                recorder.job_timed_out()
            return JobOutcome("timeout", polls=polls, elapsed_ms=(time.monotonic() - started_at) * 1000)
        response = client.request(
            "GET /api/jobs/{id}",
            "GET",
            "/api/jobs/" + quote(job_id, safe=""),
            token=token,
            timeout=min(10.0, max(1.0, remaining)),
        )
        polls += 1
        if recorder:
            recorder.job_poll()
        if response.status == 200 and isinstance(response.payload, dict):
            state = response.payload.get("status")
            if state in ("queued", "running"):
                if stop is not None:
                    stop.wait(jitter(rng, interval, 0.25, 0.05, max(0.1, interval * 3)))
                else:
                    time.sleep(jitter(rng, interval, 0.25, 0.05, max(0.1, interval * 3)))
                continue
            if state == "completed":
                return _outcome(response.payload, (time.monotonic() - started_at) * 1000, polls)
            if state == "failed":
                error = response.payload.get("error")
                code = error.get("code") if isinstance(error, dict) else None
                return JobOutcome("failed", response.payload, code, polls, (time.monotonic() - started_at) * 1000)
            return JobOutcome("error", response.payload, "UNKNOWN_JOB_STATE", polls, (time.monotonic() - started_at) * 1000)
        if response.status == 401:
            return JobOutcome("unauthorized", polls=polls, elapsed_ms=(time.monotonic() - started_at) * 1000)
        if response.status == 404:
            return JobOutcome("error", response.payload, response.code or "NOT_FOUND", polls, (time.monotonic() - started_at) * 1000)
        if response.status == 429:
            wait_for = min(10.0, response.retry_after() or 1.0)
            (stop.wait(wait_for) if stop is not None else time.sleep(wait_for))
            continue
        if response.transport_error:
            wait_for = 0.5
            (stop.wait(wait_for) if stop is not None else time.sleep(wait_for))
            continue
        # Unexpected response: retry within the deadline rather than spinning.
        wait_for = 0.5
        (stop.wait(wait_for) if stop is not None else time.sleep(wait_for))


def task_override_limit(task: dict) -> int:
    """Per-task override budget, defaulting to the server default of 2."""
    value = task.get("coreOverrideLimit", 2)
    if value is None:
        return 2
    if type(value) is bool:
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 2


def _probe_post(client: Client, account: Account, path: str, body: dict, timeout: float) -> Response:
    response = None
    for _attempt in range(6):
        if not account.token:
            login_account(client, account, timeout=timeout)
        response = client.request(
            "SETUP /api/tasks/{id}/run",
            "POST",
            path,
            body=body,
            token=account.token,
            timeout=timeout,
            record=False,
        )
        if response.status == 401:
            account.token = None
            continue
        if response.status == 429:
            time.sleep(min(30.0, response.retry_after() or 2.0))
            continue
        return response
    assert response is not None
    return response


def probe_override_ops(
    accounts: list[Account],
    code_book: CodeBook,
    tasks: list[dict],
    settings: LoadSettings,
) -> list[str]:
    """Ask the deployment which core operations this player may override.

    A ``run`` is used so the capability probe never publishes an override. The
    check is player-facing and does not require administrator credentials.
    """
    if not accounts:
        print("core override probe: skipped (no usable accounts)")
        return []
    if settings.core_override_every <= 0:
        return []
    # The server default is two overrides per task. Prefer a known task so the
    # capability probe can return OK; for a custom task a WRONG_ANSWER still
    # proves the override was accepted and executed.
    task = next(
        (
            item for item in tasks
            if task_key(item) in TASK_SOLUTIONS and task_override_limit(item) >= 1
        ),
        None,
    )
    if task is None:
        task = next((item for item in tasks if task_override_limit(item) >= 1), None)
    if task is None:
        print("core override probe: skipped (no selected task permits core overrides)")
        return []
    account = accounts[0]
    code = code_book.player(0)
    client = Client(
        settings.base_url,
        timeout=settings.request_timeout,
        insecure=settings.insecure,
        ca_bundle=settings.ca_bundle,
    )
    available: list[str] = []
    try:
        for operation in OVERRIDE_CANDIDATES:
            source = code.with_override(task, operation)
            response = _probe_post(
                client,
                account,
                "/api/tasks/" + quote(task_key(task), safe="") + "/run",
                {"code": source, "languageVersion": "v0.3"},
                settings.request_timeout,
            )
            if response.status != 202 or not isinstance(response.payload, dict) or not response.payload.get("jobId"):
                print(f"core override probe: {operation} was not accepted (HTTP {response.status} {response.code or ''})")
                continue
            outcome = poll_job(
                client,
                account.token or "",
                response.payload["jobId"],
                started_at=time.monotonic(),
                timeout=min(30.0, max(10.0, settings.job_timeout)),
                interval=0.3,
                stop=None,
                recorder=None,
                rng=random.Random(17),
            )
            if outcome.state == "completed" and outcome.verdict in ("OK", "WRONG_ANSWER"):
                available.append(operation)
                continue
            codes = set()
            if isinstance(outcome.payload, dict):
                codes = {
                    item.get("code") for item in outcome.payload.get("diagnostics", [])
                    if isinstance(item, dict)
                }
            if "OVERRIDE_DISABLED" in codes:
                continue
            if outcome.state == "completed":
                print(f"core override probe: {operation} produced {outcome.verdict}; it will not be used")
            else:
                print(f"core override probe: {operation} job state was {outcome.state}")
            if codes & {"INVALID_NAME", "RESERVED_NAME", "NAME_LENGTH", "OVERRIDE_LIMIT"}:
                break
    except (AuthError, RateLimited, ProvisionError, OSError, ValueError) as error:
        print(f"core override probe unavailable: {error}")
    finally:
        client.close()
    if available:
        print(f"core overrides available to players: {', '.join(available)}")
    else:
        print("core overrides unavailable; continuing with identifier variants only")
    return available


SCENARIO_WEIGHTS: dict[str, dict[str, int]] = {
    "browse": {
        "tasks": 18, "task": 20, "namespace": 10, "rating": 14, "rating_task": 5,
        "tables": 3, "news": 5, "config": 2, "me": 3, "submissions": 8,
        "task_submissions": 6, "notifications": 8, "submission_detail": 2,
        "insights": 3, "profile": 1,
    },
    "judge": {
        "run_ok": 27, "submit_ok": 18, "run_wrong": 6, "submit_wrong": 5,
        "tasks": 6, "task": 6, "namespace": 4, "rating": 4, "me": 2,
        "notifications": 5, "submissions": 4, "insights": 2,
    },
    "run": {
        "run_ok": 55, "run_wrong": 12, "tasks": 6, "task": 6, "namespace": 4,
        "me": 3, "rating": 3, "notifications": 4, "submissions": 4,
    },
    "submit": {
        "submit_ok": 55, "submit_wrong": 12, "tasks": 6, "task": 5,
        "namespace": 4, "me": 3, "rating": 3, "notifications": 5,
        "submissions": 4, "insights": 3,
    },
    "mixed": {
        "tasks": 12, "task": 14, "namespace": 6, "rating": 9, "rating_task": 4,
        "tables": 2, "news": 3, "config": 1, "me": 3, "submissions": 6,
        "task_submissions": 4, "notifications": 7, "notifications_read": 4,
        "notification_create": 1, "submission_detail": 3, "insights": 4,
        "profile": 1,
        "run_ok": 10, "run_wrong": 3, "submit_ok": 9, "submit_wrong": 3,
    },
    "auth": {
        "login": 55, "me": 30, "logout": 15,
    },
}


class Player:
    def __init__(
        self,
        index: int,
        account: Account,
        tasks: list[dict],
        settings: LoadSettings,
        stop: threading.Event,
        recorder: Recorder,
        code_book: CodeBook | None = None,
    ):
        self.index = index
        self.account = account
        self.tasks = tasks
        self.settings = settings
        self.stop = stop
        self.recorder = recorder
        self.rng = random.Random(settings.seed * 1_000_003 + index)
        self.code: PlayerCode | None = code_book.player(index) if code_book else None
        self.redefines_core = (
            bool(settings.override_ops)
            and settings.core_override_every > 0
            and index % settings.core_override_every == 0
        )
        self.core_runs = 0
        self.core_submits = 0
        self.client = Client(
            settings.base_url,
            timeout=settings.request_timeout,
            insecure=settings.insecure,
            ca_bundle=settings.ca_bundle,
            recorder=recorder,
        )
        self.weights = SCENARIO_WEIGHTS.get(settings.scenario) or SCENARIO_WEIGHTS["mixed"]
        self.choices = list(self.weights)
        self.blocked: dict[str, float] = {}
        self.recent_submissions: deque[str] = deque(maxlen=8)
        self.display_name = f"Player {index:04d}"
        self.fatal = False
        self.start_delay = settings.ramp_up * (index / max(1, settings.players - 1))

    # -- helpers ---------------------------------------------------------------
    def _task(self) -> dict:
        return self.tasks[self.rng.randrange(len(self.tasks))]

    def _path_task(self, suffix: str = "") -> str:
        return "/api/tasks/" + quote(task_key(self._task()), safe="") + suffix

    def _request(self, op: str, method: str, path: str, **kwargs) -> Response:
        if not self.account.token:
            self._login()
        response = self.client.request(op, method, path, token=self.account.token, **kwargs)
        if response.status == 401:
            self.recorder.record_auth_relogin()
            self.account.token = None
            self._login()
            response = self.client.request(op, method, path, token=self.account.token, **kwargs)
        return response

    def _login(self) -> None:
        from .accounts import AuthError, login_account

        try:
            login_account(self.client, self.account, record=self.settings.scenario == "auth")
        except RateLimited:
            raise
        except AuthError:
            if not self.fatal:
                eprint(f"player {self.index}: stored credentials for {self.account.username} were rejected; stopping this player")
            self.fatal = True

    def _block(self, action: str, seconds: float) -> None:
        self.blocked[action] = max(self.blocked.get(action, 0.0), time.monotonic() + max(0.1, min(seconds, 120.0)))

    def _available(self, action: str) -> bool:
        return self.blocked.get(action, 0.0) <= time.monotonic()

    def _choose(self) -> str:
        available_choices = [action for action in self.choices if self._available(action)]
        if not available_choices:
            # Every selected action is cooling down; idle like a player reading a page.
            self.stop.wait(0.25)
            return "me" if self._available("me") else "tasks"
        weights = [self.weights[action] for action in available_choices]
        return self.rng.choices(available_choices, weights=weights, k=1)[0]

    def _think(self) -> None:
        if self.settings.think_time <= 0:
            return
        self.stop.wait(jitter(self.rng, self.settings.think_time, 0.55, 0.15, max(1.0, self.settings.think_time * 8)))

    # -- player actions --------------------------------------------------------
    def act_tasks(self):
        response = self._request("GET /api/tasks", "GET", "/api/tasks")
        if response.status == 200 and isinstance(response.payload, list):
            allowed = {task_key(task) for task in self.tasks}
            current = [
                task for task in response.payload
                if isinstance(task, dict) and task_key(task) in allowed
            ]
            if len(current) == len(self.tasks):
                self.tasks = current

    def act_task(self):
        self._request("GET /api/tasks/{id}", "GET", self._path_task())

    def act_namespace(self):
        self._request("GET /api/namespace", "GET", "/api/namespace")

    def act_rating(self):
        path = "/api/rating"
        if self.rng.random() < 0.2:
            path = query(path, q=self.account.username[:6])
        self._request("GET /api/rating", "GET", path)

    def act_rating_task(self):
        path = query("/api/rating", table="task:" + task_key(self._task()))
        self._request("GET /api/rating?task", "GET", path)

    def act_tables(self):
        self._request("GET /api/rating/tables", "GET", "/api/rating/tables")

    def act_news(self):
        self._request("GET /api/news", "GET", "/api/news")

    def act_config(self):
        self._request("GET /api/config", "GET", "/api/config")

    def act_me(self):
        response = self._request("GET /api/me", "GET", "/api/me")
        if response.status == 200 and isinstance(response.payload, dict):
            self.account.user_id = response.payload.get("id") or self.account.user_id

    def act_submissions(self):
        if not self.account.user_id:
            self.act_me()
        if not self.account.user_id:
            # Without a user id the collection endpoint is a global feed, so a
            # player without that id should read a task-scoped personal history.
            self.act_task_submissions()
            return
        path = query("/api/submissions", limit=20, userId=self.account.user_id)
        response = self._request("GET /api/submissions", "GET", path)
        if response.status == 200 and isinstance(response.payload, list):
            for item in response.payload[:3]:
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    self.recent_submissions.append(item["id"])

    def act_task_submissions(self):
        scope = "all" if self.rng.random() < 0.7 else "mine"
        path = query(self._path_task("/submissions"), scope=scope, limit=20)
        self._request("GET /api/tasks/{id}/submissions", "GET", path)

    def act_notifications(self):
        self._request("GET /api/notifications", "GET", query("/api/notifications", limit=20))

    def act_notifications_read(self):
        response = self._request("GET /api/notifications", "GET", query("/api/notifications", limit=20))
        if response.status == 200 and isinstance(response.payload, dict) and response.payload.get("unreadCount"):
            self._request("POST /api/notifications/read", "POST", "/api/notifications/read", body={"all": True})

    def act_notification_create(self):
        self._request(
            "POST /api/notifications",
            "POST",
            "/api/notifications",
            body={"kind": "info", "message": f"load marker {self.index}-{self.rng.randrange(1_000_000)}"},
        )

    def act_profile(self):
        if self.rng.random() < 0.7:
            body = {"displayName": self.display_name}
        else:
            body = {"avatarHue": self.rng.randrange(0, 361)}
        self._request("PATCH /api/me", "PATCH", "/api/me", body=body)

    def act_submission_detail(self):
        if not self.recent_submissions:
            self.act_submissions()
            return
        submission_id = self.recent_submissions[self.rng.randrange(len(self.recent_submissions))]
        self._request("GET /api/submissions/{id}", "GET", "/api/submissions/" + quote(submission_id, safe=""))

    def act_insights(self):
        self._request("GET /api/tasks/{id}/insights", "GET", self._path_task("/insights"))

    def _judge(self, mode: str, correct: bool):
        task = self._task()
        if correct:
            source = self.code.variant(task) if self.code else correct_source(task)
        else:
            source = self.code.wrong(task) if self.code else wrong_source(task)
        override_ops: list[str] = []
        include_override = False
        if correct and self.redefines_core and self.code:
            if mode == "submit" and self.core_submits % 3 == 0:
                include_override = True
            elif mode == "run" and self.core_runs % 4 == 0:
                include_override = True
            if include_override:
                limit = task_override_limit(task)
                count = min(limit, len(self.settings.override_ops))
                start = self.index % len(self.settings.override_ops)
                override_ops = [
                    self.settings.override_ops[(start + offset) % len(self.settings.override_ops)]
                    for offset in range(count)
                ]
        if override_ops and self.code:
            source = "".join(self.code.override(op) for op in override_ops) + self.code.variant(task)
        path = "/api/tasks/" + quote(task_key(task), safe="") + "/" + mode
        op = f"POST /api/tasks/{{id}}/{mode}"
        body = {"code": source, "languageVersion": "v0.3"}
        self.recorder.job_submitted(mode)
        enqueued_at = time.monotonic()
        response = self._request(op, "POST", path, body=body)
        if response.status == 409 and response.code == "PENALTY_CONFIRMATION_REQUIRED":
            error = (response.payload or {}).get("error") or {}
            points = error.get("points")
            if type(points) is int:
                body = {**body, "acceptResubmitPenalty": True, "expectedPenaltyPoints": points}
                response = self._request(op, "POST", path, body=body)
        if response.status == 202 and isinstance(response.payload, dict) and response.payload.get("jobId"):
            self.recorder.job_accepted()
            if correct and self.redefines_core:
                if mode == "submit":
                    self.core_submits += 1
                elif mode == "run":
                    self.core_runs += 1
            self._poll(response.payload["jobId"], enqueued_at)
            return
        if response.status == 202:
            self.recorder.job_rejected("MALFORMED_RESPONSE")
            return
        self.recorder.job_rejected(response.code or f"HTTP_{response.status}")
        if response.status == 429:
            wait_for = response.retry_after() or (5.0 if response.code in ("QUEUE_FULL", "NOTIFICATION_BACKLOG") else 1.0)
            self._block(mode, wait_for)
        elif response.status in (401, 403):
            self._block(mode, 5.0)

    def _poll(self, job_id: str, enqueued_at: float) -> None:
        polls = 0
        deadline = enqueued_at + self.settings.job_timeout
        while True:
            if self.stop.is_set():
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.recorder.job_timed_out()
                return
            response = self._request(
                "GET /api/jobs/{id}",
                "GET",
                "/api/jobs/" + quote(job_id, safe=""),
                timeout=min(10.0, max(1.0, remaining)),
            )
            polls += 1
            self.recorder.job_poll()
            if response.status == 200 and isinstance(response.payload, dict):
                state = response.payload.get("status")
                if state in ("queued", "running"):
                    self.stop.wait(jitter(self.rng, self.settings.poll_interval, 0.25, 0.05, max(0.1, self.settings.poll_interval * 4)))
                    continue
                if state == "completed":
                    outcome = _outcome(response.payload, (time.monotonic() - enqueued_at) * 1000, polls)
                    self.recorder.job_completed(outcome.verdict, outcome.elapsed_ms, outcome.execution_ms, outcome.rating_delta)
                    if outcome.submission_id:
                        self.recent_submissions.append(outcome.submission_id)
                    return
                if state == "failed":
                    error = response.payload.get("error")
                    code = error.get("code") if isinstance(error, dict) else None
                    self.recorder.job_failed(code)
                    return
                self.recorder.job_failed("UNKNOWN_JOB_STATE")
                return
            if response.status == 404:
                self.recorder.job_failed(response.code or "NOT_FOUND")
                return
            if response.status == 401:
                # _request already retried after one re-login; count this job as failed
                # when the session cannot be restored.
                self.recorder.job_failed("UNAUTHORIZED")
                return
            if response.status == 429:
                self.stop.wait(min(10.0, response.retry_after() or 1.0))
                continue
            self.stop.wait(0.5)

    def act_run_ok(self):
        self._judge("run", True)

    def act_run_wrong(self):
        self._judge("run", False)

    def act_submit_ok(self):
        self._judge("submit", True)

    def act_submit_wrong(self):
        self._judge("submit", False)

    def act_login(self):
        self.account.token = None
        self._login()
        if self.account.token:
            self._request("GET /api/me", "GET", "/api/me")

    def act_logout(self):
        if not self.account.token:
            return
        self._request("POST /api/auth/logout", "POST", "/api/auth/logout")
        self.account.token = None

    # -- main loop -------------------------------------------------------------
    def run(self) -> None:
        try:
            if self.start_delay and self.stop.wait(self.start_delay):
                return
            if not self.account.token:
                try:
                    self._login()
                except RateLimited as error:
                    self.stop.wait(min(60.0, error.wait))
            while not self.stop.is_set() and not self.fatal:
                action = self._choose()
                method: Callable = getattr(self, "act_" + action, None)
                if method is None:
                    self.stop.wait(0.1)
                    continue
                try:
                    method()
                except RateLimited as error:
                    self.stop.wait(min(60.0, max(0.5, error.wait)))
                except (OSError, ValueError) as error:
                    # Transport failures are already recorded by the client.
                    if self.settings.scenario == "auth":
                        eprint(f"player {self.index}: {type(error).__name__}: {error}")
                    self.stop.wait(0.5)
                self._think()
        finally:
            self.client.close()


def select_tasks(tasks: list[dict], wanted: list[str]) -> list[dict]:
    if wanted:
        selected = []
        for task in tasks:
            if task_key(task) in wanted or task.get("slug") in wanted:
                selected.append(task)
        return selected
    preferred = [task for task in tasks if task_key(task) in DEFAULT_TASKS]
    return preferred or tasks


def run_load(accounts: list[Account], settings: LoadSettings) -> tuple[Recorder, list[Account]]:
    setup_client = Client(settings.base_url, timeout=settings.request_timeout, insecure=settings.insecure, ca_bundle=settings.ca_bundle)
    config: dict = {}
    try:
        response = setup_client.request("SETUP /api/tasks", "GET", "/api/tasks", record=False)
        if response.status != 200 or not isinstance(response.payload, list):
            raise RuntimeError(f"cannot read /api/tasks: HTTP {response.status} {response.code or ''}")
        tasks = select_tasks([task for task in response.payload if isinstance(task, dict) and task_key(task)], settings.task_keys)
        if not tasks:
            if settings.task_keys:
                raise RuntimeError("none of the --task values match a task on this deployment")
            raise RuntimeError("the deployment exposes no tasks")
        config_response = setup_client.request("SETUP /api/config", "GET", "/api/config", record=False)
        if config_response.status == 200 and isinstance(config_response.payload, dict):
            config = config_response.payload
    finally:
        setup_client.close()

    selected_accounts = accounts[: settings.players]
    if settings.preflight:
        selected_accounts = preflight_accounts(
            selected_accounts,
            base_url=settings.base_url,
            timeout=settings.request_timeout,
            insecure=settings.insecure,
            ca_bundle=settings.ca_bundle,
            limit=settings.players,
        )
    if not selected_accounts:
        raise RuntimeError("no usable accounts; run the provision command first")
    if len(selected_accounts) < settings.players:
        if not settings.allow_fewer:
            raise RuntimeError(
                f"requested {settings.players} players but only {len(selected_accounts)} usable account(s); "
                "run provision with a larger --players value or pass --allow-fewer"
            )
        print(f"using {len(selected_accounts)} usable account(s) instead of {settings.players}")
        settings.players = len(selected_accounts)

    code_book: CodeBook | None = None
    if settings.scenario in ("judge", "run", "submit", "mixed"):
        try:
            code_book = CodeBook(
                tasks,
                allowed_words=config.get("allowedWords"),
                max_name_length=config.get("maxNameLength"),
                players=settings.players,
                group_size=settings.shadow_group_size,
            )
        except ValueError as error:
            raise RuntimeError(f"cannot generate per-player identifiers: {error}") from error
        if settings.core_override_every > 0:
            settings.override_ops = probe_override_ops(selected_accounts, code_book, tasks, settings)
        else:
            print("core overrides: disabled (--core-override-every 0)")
    else:
        print(
            f"core overrides: skipped (scenario {settings.scenario!r} does not generate solution code)"
        )

    recorder = Recorder()
    stop = threading.Event()
    shadow = (
        f" shadowGroups={settings.shadow_group_size}"
        if settings.shadow_group_size > 1 else " shadowing=off"
    )
    overrides = (
        f" coreOverrides={','.join(settings.override_ops)}"
        if settings.override_ops else " coreOverrides=off"
    )
    print(
        f"load: scenario={settings.scenario} players={settings.players} duration={settings.duration:.0f}s "
        f"ramp={settings.ramp_up:.0f}s think={settings.think_time:.1f}s tasks={len(tasks)}{shadow}{overrides}",
        flush=True,
    )
    threads = [
        Player(index, account, tasks, settings, stop, recorder, code_book)
        for index, account in enumerate(selected_accounts)
    ]
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=len(threads)) as pool:
        futures = [pool.submit(player.run) for player in threads]
        try:
            stop.wait(settings.duration)
        except KeyboardInterrupt:
            print("interrupted; stopping players", flush=True)
        finally:
            stop.set()
        wait(futures, timeout=settings.request_timeout + settings.job_timeout)
    recorder.finished = time.monotonic()
    print(f"stopped after {time.monotonic() - started:.1f}s", flush=True)
    return recorder, selected_accounts


def run_probe(
    *,
    base_url: str,
    clients: int,
    requests: int,
    duration: float,
    request_timeout: float,
    insecure: bool,
    ca_bundle: str | None,
) -> Recorder:
    recorder = Recorder()
    endpoints = [
        ("GET /api/tasks", "/api/tasks"),
        ("GET /api/config", "/api/config"),
        ("GET /api/rating", "/api/rating"),
        ("GET /api/news", "/api/news"),
        ("GET /api/namespace", "/api/namespace"),
        ("GET /api/rating/tables", "/api/rating/tables"),
    ]
    deadline = time.monotonic() + duration if duration > 0 else None
    work = threading.Lock()
    next_index = [0]

    def worker(_index: int):
        client = Client(base_url, timeout=request_timeout, insecure=insecure, ca_bundle=ca_bundle, recorder=recorder)
        try:
            while True:
                if deadline is not None and time.monotonic() >= deadline:
                    return
                with work:
                    current = next_index[0]
                    if deadline is None and current >= requests:
                        return
                    next_index[0] = current + 1
                op, path = endpoints[current % len(endpoints)]
                client.request(op, "GET", path)
        finally:
            client.close()

    with ThreadPoolExecutor(max_workers=clients) as pool:
        list(pool.map(worker, range(clients)))
    recorder.finished = time.monotonic()
    return recorder
