from __future__ import annotations

import random
import time
from dataclasses import dataclass

from .accounts import Account, AuthError, RateLimited, login_account
from .http import Client, Response
from .solutions import correct_source, syntax_error_source, task_key, wrong_source
from .workload import poll_job


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""
    warning: str = ""
    elapsed_ms: float = 0.0


def _login_or_refresh(client: Client, account: Account, timeout: float) -> None:
    for attempt in range(4):
        try:
            login_account(client, account, timeout=timeout)
            return
        except RateLimited as error:
            if attempt == 3:
                raise
            time.sleep(min(30.0, max(1.0, error.wait)))
        except AuthError as error:
            raise RuntimeError(f"account {account.username} cannot log in: {error}") from error


def _session(client: Client, account: Account, timeout: float) -> dict:
    if account.token:
        response = client.request("GET /api/me", "GET", "/api/me", token=account.token, timeout=timeout, record=False)
        if response.status == 200 and isinstance(response.payload, dict):
            return response.payload
    _login_or_refresh(client, account, timeout)
    response = client.request("GET /api/me", "GET", "/api/me", token=account.token, timeout=timeout, record=False)
    if response.status != 200 or not isinstance(response.payload, dict):
        raise RuntimeError(f"GET /api/me returned HTTP {response.status}")
    return response.payload


def _request(client: Client, account: Account, op: str, method: str, path: str, *, timeout: float, **kwargs) -> Response:
    response = client.request(op, method, path, token=account.token, timeout=timeout, record=False, **kwargs)
    if response.status == 401:
        _login_or_refresh(client, account, timeout)
        response = client.request(op, method, path, token=account.token, timeout=timeout, record=False, **kwargs)
    return response


def _post_judge(client: Client, account: Account, task: dict, mode: str, source: str, timeout: float) -> Response:
    path = "/api/tasks/" + task_key(task) + "/" + mode
    body = {"code": source, "languageVersion": "v0.3"}
    for attempt in range(3):
        response = _request(client, account, f"POST /api/tasks/{mode}", "POST", path, timeout=timeout, body=body)
        if response.status == 409 and response.code == "PENALTY_CONFIRMATION_REQUIRED":
            points = ((response.payload or {}).get("error") or {}).get("points")
            if type(points) is int:
                body = {**body, "acceptResubmitPenalty": True, "expectedPenaltyPoints": points}
                continue
        if response.status == 429:
            wait = min(30.0, response.retry_after() or 3.0)
            time.sleep(wait)
            continue
        return response
    return response


def _verdict(payload: dict | None, mode: str) -> str | None:
    if not isinstance(payload, dict):
        return None
    result = payload.get("result")
    if not isinstance(result, dict):
        result = payload
    if mode == "submit":
        return ((result.get("submission") or {}).get("status"))
    return result.get("status")


def _wait_job(client: Client, account: Account, job_id: str, *, timeout: float) -> tuple[str, dict | None, str]:
    started = time.monotonic()
    outcome = poll_job(
        client,
        account.token,
        job_id,
        started_at=started,
        timeout=timeout,
        interval=0.35,
        stop=None,
        recorder=None,
        rng=random.Random(7),
    )
    if outcome.state == "unauthorized":
        _login_or_refresh(client, account, timeout)
        return _wait_job(client, account, job_id, timeout=timeout)
    return outcome.state, outcome.payload, outcome.error_code or ""


def _timed(fn) -> tuple[object, float]:
    started = time.perf_counter()
    value = fn()
    return value, (time.perf_counter() - started) * 1000


def run_smoke(account: Account, second: Account | None, *, base_url: str, timeout: float, insecure: bool, ca_bundle: str | None, write: bool, strict_world: bool, task_key_wanted: str | None) -> list[Check]:
    checks: list[Check] = []

    def add(name: str, ok: bool, detail: str = "", warning: str = "", elapsed: float = 0.0):
        checks.append(Check(name, ok, detail, warning, elapsed))

    def require(name: str, response: Response, status: int = 200, code: str | None = None):
        good = response.status == status and (code is None or response.code == code)
        add(name, good, f"HTTP {response.status}" + (f" {response.code}" if response.code else ""), elapsed=response.elapsed_ms)
        return good

    client = Client(base_url, timeout=timeout, insecure=insecure, ca_bundle=ca_bundle)
    second_client = Client(base_url, timeout=timeout, insecure=insecure, ca_bundle=ca_bundle) if second else None
    try:
        health = client.request("GET /api/health", "GET", "/api/health", timeout=timeout, record=False)
        good = health.status == 200 and isinstance(health.payload, dict) and health.payload.get("status") == "ok"
        add("health", good, f"HTTP {health.status} status={((health.payload or {}).get('status') if isinstance(health.payload, dict) else None)}", elapsed=health.elapsed_ms)

        config = client.request("GET /api/config", "GET", "/api/config", timeout=timeout, record=False)
        required = {"registrationMode", "runCooldownSeconds", "submitCooldownSeconds", "maxNameLength"}
        add("config", config.status == 200 and isinstance(config.payload, dict) and required <= set(config.payload),
            f"HTTP {config.status}", elapsed=config.elapsed_ms)

        tasks_response = client.request("GET /api/tasks", "GET", "/api/tasks", timeout=timeout, record=False)
        tasks = tasks_response.payload if isinstance(tasks_response.payload, list) else []
        tasks = [task for task in tasks if isinstance(task, dict) and task_key(task)]
        add("tasks_public", tasks_response.status == 200 and bool(tasks), f"HTTP {tasks_response.status} tasks={len(tasks)}", elapsed=tasks_response.elapsed_ms)
        if tasks:
            add("hidden_tests_redacted", all("tests" not in task for task in tasks), "task list has no tests field")
        if not tasks:
            return checks

        if task_key_wanted:
            preferred = [task for task in tasks if task_key(task) == task_key_wanted or task.get("slug") == task_key_wanted]
            if preferred:
                tasks = preferred + [task for task in tasks if task not in preferred]

        task = tasks[0]
        detail = client.request("GET /api/tasks/" + task_key(task), "GET", "/api/tasks/" + task_key(task), timeout=timeout, record=False)
        good = detail.status == 200 and isinstance(detail.payload, dict) and "tests" not in detail.payload
        add("task_detail_public", good, f"HTTP {detail.status}", elapsed=detail.elapsed_ms)

        namespace = client.request("GET /api/namespace", "GET", "/api/namespace", timeout=timeout, record=False)
        good = namespace.status == 200 and isinstance(namespace.payload, dict) and isinstance(namespace.payload.get("version"), int)
        add("namespace", good, f"HTTP {namespace.status}", elapsed=namespace.elapsed_ms)
        version_before = namespace.payload.get("version") if isinstance(namespace.payload, dict) else None

        me = _session(client, account, timeout)
        add("session", True, f"user={me.get('username')} rating={me.get('rating')}")
        add("profile_private_fields", "passwordHash" not in me and "email" not in me, "no passwordHash/email in /api/me")

        me_response = client.request("GET /api/me", "GET", "/api/me", token=account.token, timeout=timeout, record=False)
        require("me", me_response)

        authenticated_tasks = _request(client, account, "GET /api/tasks", "GET", "/api/tasks", timeout=timeout)
        if authenticated_tasks.status == 200 and isinstance(authenticated_tasks.payload, list):
            first = authenticated_tasks.payload[0] if authenticated_tasks.payload else {}
            add("tasks_authenticated", isinstance(first, dict) and "solved" in first and "status" in first,
                f"HTTP {authenticated_tasks.status}", elapsed=authenticated_tasks.elapsed_ms)
        else:
            add("tasks_authenticated", False, f"HTTP {authenticated_tasks.status}", elapsed=authenticated_tasks.elapsed_ms)

        rating = client.request("GET /api/rating", "GET", "/api/rating", timeout=timeout, record=False)
        good = rating.status == 200 and isinstance(rating.payload, dict) and isinstance(rating.payload.get("rows"), list)
        add("rating", good, f"HTTP {rating.status}", elapsed=rating.elapsed_ms)

        news = client.request("GET /api/news", "GET", "/api/news", timeout=timeout, record=False)
        add("news", news.status == 200 and isinstance(news.payload, list), f"HTTP {news.status}", elapsed=news.elapsed_ms)

        notifications = _request(client, account, "GET /api/notifications", "GET", "/api/notifications", timeout=timeout)
        good = notifications.status == 200 and isinstance(notifications.payload, dict) and "items" in notifications.payload
        add("notifications", good, f"HTTP {notifications.status}", elapsed=notifications.elapsed_ms)

        submissions = _request(client, account, "GET /api/submissions", "GET", "/api/submissions", timeout=timeout)
        add("submissions", submissions.status == 200 and isinstance(submissions.payload, list),
            f"HTTP {submissions.status}", elapsed=submissions.elapsed_ms)

        insights = client.request("GET /api/tasks/{id}/insights", "GET", "/api/tasks/" + task_key(task) + "/insights", timeout=timeout, record=False)
        add("insights", insights.status == 200 and isinstance(insights.payload, dict), f"HTTP {insights.status}", elapsed=insights.elapsed_ms)

        # Runs must execute the visible examples and must not publish state.
        run = _post_judge(client, account, task, "run", correct_source(task), timeout)
        if run.status == 202 and isinstance(run.payload, dict) and run.payload.get("jobId"):
            state, payload, error = _wait_job(client, account, run.payload["jobId"], timeout=max(30.0, timeout))
            add("run_correct_enqueued", True, f"job state={state}")
            verdict = _verdict(payload, "run")
            add("run_correct_verdict", state == "completed" and verdict == "OK", f"state={state} verdict={verdict} error={error}")
            after = client.request("GET /api/namespace", "GET", "/api/namespace", timeout=timeout, record=False)
            version_after = after.payload.get("version") if isinstance(after.payload, dict) else None
            if version_before is not None and version_after is not None and version_after != version_before:
                add("run_does_not_publish", True, f"world version changed {version_before}->{version_after}",
                    warning="concurrent players changed the shared world; publication check was inconclusive" if not strict_world else "")
                if strict_world:
                    checks[-1].ok = False
            else:
                add("run_does_not_publish", True, f"world version {version_after}")
        else:
            add("run_correct_enqueued", False, f"HTTP {run.status} {run.code or ''}", elapsed=run.elapsed_ms)

        wrong = _post_judge(client, account, task, "run", wrong_source(task), timeout)
        if wrong.status == 202 and isinstance(wrong.payload, dict) and wrong.payload.get("jobId"):
            state, payload, error = _wait_job(client, account, wrong.payload["jobId"], timeout=max(30.0, timeout))
            verdict = _verdict(payload, "run")
            add("run_wrong_verdict", state == "completed" and verdict != "OK", f"state={state} verdict={verdict} error={error}")
        else:
            add("run_wrong_verdict", False, f"HTTP {wrong.status} {wrong.code or ''}", elapsed=wrong.elapsed_ms)

        syntax = _post_judge(client, account, task, "run", syntax_error_source(task), timeout)
        if syntax.status == 202 and isinstance(syntax.payload, dict) and syntax.payload.get("jobId"):
            state, payload, error = _wait_job(client, account, syntax.payload["jobId"], timeout=max(30.0, timeout))
            verdict = _verdict(payload, "run")
            add("run_syntax_verdict", state == "completed" and verdict == "CE", f"state={state} verdict={verdict} error={error}")
        else:
            add("run_syntax_verdict", False, f"HTTP {syntax.status} {syntax.code or ''}", elapsed=syntax.elapsed_ms)

        if write:
            submit = _post_judge(client, account, task, "submit", correct_source(task), timeout)
            if submit.status == 202 and isinstance(submit.payload, dict) and submit.payload.get("jobId"):
                state, payload, error = _wait_job(client, account, submit.payload["jobId"], timeout=max(60.0, timeout))
                verdict = _verdict(payload, "submit")
                add("submit_correct", state == "completed" and verdict == "OK", f"state={state} verdict={verdict} error={error}")
                result = payload.get("result") if isinstance(payload, dict) and isinstance(payload.get("result"), dict) else payload
                submission_id = ((result or {}).get("submission") or {}).get("id") if isinstance(result, dict) else None
                if submission_id and second and second_client:
                    _session(second_client, second, timeout)
                    other_view = _request(second_client, second, "GET /api/submissions/{id}", "GET", "/api/submissions/" + submission_id, timeout=timeout)
                    add("submission_code_private", other_view.status == 403, f"HTTP {other_view.status} {other_view.code or ''}")
                    own_view = _request(client, account, "GET /api/submissions/{id}", "GET", "/api/submissions/" + submission_id, timeout=timeout)
                    add("submission_own_detail", own_view.status == 200, f"HTTP {own_view.status}")
            else:
                add("submit_correct", False, f"HTTP {submit.status} {submit.code or ''}", elapsed=submit.elapsed_ms)

        if second and second_client:
            _session(second_client, second, timeout)
            private = _post_judge(client, account, task, "run", correct_source(task), timeout)
            if private.status == 202 and isinstance(private.payload, dict) and private.payload.get("jobId"):
                job_id = private.payload["jobId"]
                other = second_client.request("GET /api/jobs/{id}", "GET", "/api/jobs/" + job_id, token=second.token, timeout=timeout, record=False)
                own = client.request("GET /api/jobs/{id}", "GET", "/api/jobs/" + job_id, token=account.token, timeout=timeout, record=False)
                add("job_private_to_owner", other.status == 404 and own.status == 200, f"other={other.status} own={own.status}")
            elif private.status == 429:
                add("job_private_to_owner", True, f"skipped: HTTP 429 {private.code or ''}", warning="cooldown")
            else:
                add("job_private_to_owner", False, f"HTTP {private.status} {private.code or ''}", elapsed=private.elapsed_ms)
    finally:
        client.close()
        if second_client:
            second_client.close()
    return checks
