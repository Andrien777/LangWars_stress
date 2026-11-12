"""Adversarial API probes for the LangWars stress pack.

The probes assert validation, authentication and authorization boundaries. They
are read-mostly: apart from one job enqueued by ``job_privacy`` (and failed
login/registration attempts), nothing is created or modified.
"""
from __future__ import annotations

import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import quote

from .accounts import Account, AuthError, RateLimited, login_account
from .http import Client, Response, query
from .solutions import TASK_SOLUTIONS, correct_source, oversized_source, pathological_nesting, task_key
from .util import eprint, format_ms


LANGUAGE_VERSION = "v0.3"
OVERSIZED_BODY = b'{"code":"' + b"x" * (513 * 1024) + b'"}'


@dataclass
class ProbeResult:
    name: str
    ok: bool
    skipped: bool = False
    expected: str = ""
    actual: str = ""
    detail: str = ""
    elapsed_ms: float = 0.0


# -- result helpers -----------------------------------------------------------
def _short(value, limit: int = 160) -> str:
    if not value:
        return ""
    return " ".join(str(value).split())[:limit]


def _actual(response: Response) -> str:
    if response.transport_error:
        return f"HTTP {response.status} transport_error={_short(response.transport_error)}"
    if response.code:
        return f"HTTP {response.status} {response.code}"
    return f"HTTP {response.status}"


def _expected(status: int, code: str | None = None) -> str:
    return f"HTTP {status} {code}" if code else f"HTTP {status}"


def _probe(
    name: str,
    response: Response,
    *,
    status: int,
    code: str | None = None,
    code_optional: bool = False,
    check=None,
) -> ProbeResult:
    expected = _expected(status, code)
    ok = not response.transport_error and response.status == status
    if code is not None and not code_optional:
        ok = ok and response.code == code
    detail = ""
    if not ok:
        detail = _short(response.transport_error or response.message)
    if check is not None and not response.transport_error:
        try:
            passed, problem = check(response)
        except Exception:
            passed, problem = False, "invalid response payload"
        if not passed:
            ok = False
            detail = _short(problem) or detail
    return ProbeResult(
        name=name,
        ok=ok,
        expected=expected,
        actual=_actual(response),
        detail=detail,
        elapsed_ms=response.elapsed_ms,
    )


def _check_health(response: Response) -> tuple[bool, str]:
    if not isinstance(response.payload, dict) or response.payload.get("status") != "ok":
        return False, "payload status is not ok"
    return True, ""


def _check_public_tasks(response: Response) -> tuple[bool, str]:
    payload = response.payload
    if not isinstance(payload, list) or not payload:
        return False, "expected a non-empty task list"
    if not isinstance(payload[0], dict):
        return False, "task list item is not an object"
    if "tests" in payload[0]:
        return False, "public task list exposes tests"
    return True, ""


def _check_public_task(response: Response) -> tuple[bool, str]:
    payload = response.payload
    if not isinstance(payload, dict):
        return False, "expected a task object"
    if "tests" in payload:
        return False, "public task detail exposes tests"
    return True, ""


# -- account setup ------------------------------------------------------------
def _login_with_retries(client: Client, account: Account, *, timeout: float, verbose: bool) -> None:
    delay = 1.0
    for _attempt in range(3):
        try:
            login_account(client, account, record=False, timeout=timeout)
            return
        except RateLimited as error:
            wait = min(60.0, max(1.0, error.wait or delay))
            if verbose:
                eprint(f"abuse: login throttled; waiting {wait:.0f}s")
            time.sleep(wait)
            delay = min(60.0, delay * 2)
        except AuthError as error:
            raise RuntimeError(f"stored credentials were rejected during setup: {error}") from error
    raise RuntimeError("login remained rate limited after 3 attempts")


def _refresh_accounts(accounts: list[Account], client: Client, *, timeout: float, verbose: bool) -> None:
    for account in accounts:
        if account.token:
            response = client.request(
                "SETUP /api/me",
                "GET",
                "/api/me",
                token=account.token,
                timeout=timeout,
                record=False,
            )
            if response.status == 200:
                if isinstance(response.payload, dict):
                    account.user_id = response.payload.get("id") or account.user_id
                continue
            if response.status != 401:
                # Leave the token in place; the probes will report the real issue.
                continue
            account.token = None
        _login_with_retries(client, account, timeout=timeout, verbose=verbose)


# -- adversarial probes -------------------------------------------------------
def _job_privacy(client: Client, owner: Account, viewer: Account, run_path: str, source: str) -> ProbeResult:
    expected = "B HTTP 404 NOT_FOUND; A HTTP 200"
    created = client.request(
        "POST /api/tasks/{id}/run",
        "POST",
        run_path,
        body={"code": source, "languageVersion": LANGUAGE_VERSION},
        token=owner.token,
        record=False,
    )
    if created.transport_error:
        return ProbeResult(
            "job_privacy",
            False,
            expected=expected,
            actual=_actual(created),
            detail=_short(created.transport_error),
            elapsed_ms=created.elapsed_ms,
        )
    if created.status == 429:
        return ProbeResult(
            "job_privacy",
            False,
            skipped=True,
            expected=expected,
            actual=_actual(created),
            detail=_short(created.message),
            elapsed_ms=created.elapsed_ms,
        )
    job_id = created.payload.get("jobId") if isinstance(created.payload, dict) else None
    if created.status == 202 and not job_id:
        return ProbeResult(
            "job_privacy",
            False,
            skipped=True,
            expected=expected,
            actual=_actual(created),
            detail="accepted response did not contain a jobId",
            elapsed_ms=created.elapsed_ms,
        )
    if created.status != 202 or not isinstance(job_id, str):
        return ProbeResult(
            "job_privacy",
            False,
            expected=expected,
            actual=_actual(created),
            detail=_short(created.message),
            elapsed_ms=created.elapsed_ms,
        )

    job_path = "/api/jobs/" + quote(job_id, safe="")
    other = client.request("GET /api/jobs/{id}", "GET", job_path, token=viewer.token, record=False)
    own = client.request("GET /api/jobs/{id}", "GET", job_path, token=owner.token, record=False)
    ok = (
        not other.transport_error
        and not own.transport_error
        and other.status == 404
        and other.code == "NOT_FOUND"
        and own.status == 200
    )
    detail = ""
    if not ok:
        detail = _short(
            other.transport_error
            or other.message
            or own.transport_error
            or own.message
        )
    return ProbeResult(
        "job_privacy",
        ok,
        expected=expected,
        actual=f"B {_actual(other)}; A {_actual(own)}",
        detail=detail,
        elapsed_ms=created.elapsed_ms + other.elapsed_ms + own.elapsed_ms,
    )


def _cycle(client: Client, accounts: list[Account], task_path: str, source: str) -> list[ProbeResult]:
    owner = accounts[0]
    token = owner.token
    results: list[ProbeResult] = []

    results.append(_probe(
        "health_ok",
        client.request("GET /api/health", "GET", "/api/health", record=False),
        status=200,
        check=_check_health,
    ))
    results.append(_probe(
        "unauthorized_me",
        client.request("GET /api/me", "GET", "/api/me", record=False),
        status=401,
        code="UNAUTHORIZED",
    ))
    results.append(_probe(
        "public_tasks",
        client.request("GET /api/tasks", "GET", "/api/tasks", record=False),
        status=200,
        check=_check_public_tasks,
    ))
    results.append(_probe(
        "public_task_detail",
        client.request("GET /api/tasks/{id}", "GET", task_path, record=False),
        status=200,
        check=_check_public_task,
    ))
    results.append(_probe(
        "unknown_task",
        client.request("GET /api/tasks/{id}", "GET", "/api/tasks/definitely-not-a-task", record=False),
        status=404,
        code="NOT_FOUND",
    ))
    results.append(_probe(
        "run_without_auth",
        client.request(
            "POST /api/tasks/{id}/run",
            "POST",
            task_path + "/run",
            body={"code": source, "languageVersion": LANGUAGE_VERSION},
            record=False,
        ),
        status=401,
        code="UNAUTHORIZED",
    ))
    results.append(_probe(
        "run_bad_version",
        client.request(
            "POST /api/tasks/{id}/run",
            "POST",
            task_path + "/run",
            body={"code": source, "languageVersion": "v9"},
            token=token,
            record=False,
        ),
        status=422,
        code="UNSUPPORTED_VERSION",
    ))
    results.append(_probe(
        "run_empty_code",
        client.request(
            "POST /api/tasks/{id}/run",
            "POST",
            task_path + "/run",
            body={"code": "", "languageVersion": LANGUAGE_VERSION},
            token=token,
            record=False,
        ),
        status=422,
        code="BAD_BODY",
    ))
    results.append(_probe(
        "run_oversized_code",
        client.request(
            "POST /api/tasks/{id}/run",
            "POST",
            task_path + "/run",
            body={"code": oversized_source(), "languageVersion": LANGUAGE_VERSION},
            token=token,
            record=False,
        ),
        status=422,
        code="BAD_BODY",
    ))
    results.append(_probe(
        "malformed_json",
        client.request(
            "POST /api/tasks/{id}/run",
            "POST",
            task_path + "/run",
            raw_body=b'{"code":',
            token=token,
            record=False,
        ),
        status=400,
        code="BAD_JSON",
    ))
    results.append(_probe(
        "nonfinite_json",
        client.request(
            "POST /api/tasks/{id}/run",
            "POST",
            task_path + "/run",
            raw_body=b'{"code":NaN}',
            token=token,
            record=False,
        ),
        status=400,
        code="BAD_JSON",
    ))
    results.append(_probe(
        "deep_json",
        client.request(
            "POST /api/tasks/{id}/run",
            "POST",
            task_path + "/run",
            raw_body=pathological_nesting(),
            token=token,
            record=False,
        ),
        status=400,
        code="BAD_JSON",
    ))
    results.append(_probe(
        "oversized_body",
        client.request(
            "POST /api/tasks/{id}/run",
            "POST",
            task_path + "/run",
            raw_body=OVERSIZED_BODY,
            token=token,
            record=False,
        ),
        status=413,
        code="BODY_TOO_LARGE",
        code_optional=True,
    ))
    results.append(_probe(
        "bad_cursor",
        client.request(
            "GET /api/submissions",
            "GET",
            query("/api/submissions", beforeCreated="2020-01-01T00:00:00Z"),
            token=token,
            record=False,
        ),
        status=422,
        code="BAD_CURSOR",
    ))
    results.append(_probe(
        "unknown_job",
        client.request("GET /api/jobs/{id}", "GET", "/api/jobs/job_does_not_exist", token=token, record=False),
        status=404,
        code="NOT_FOUND",
    ))
    results.append(_probe(
        "unknown_submission",
        client.request(
            "GET /api/submissions/{id}",
            "GET",
            "/api/submissions/sub_does_not_exist",
            token=token,
            record=False,
        ),
        status=404,
        code="NOT_FOUND",
    ))
    results.append(_probe(
        "bad_notification_kind",
        client.request(
            "POST /api/notifications",
            "POST",
            "/api/notifications",
            body={"kind": "admin", "message": "x"},
            token=token,
            record=False,
        ),
        status=422,
        code="BAD_BODY",
    ))
    results.append(_probe(
        "bad_notification_read",
        client.request(
            "POST /api/notifications/read",
            "POST",
            "/api/notifications/read",
            body={},
            token=token,
            record=False,
        ),
        status=422,
        code="BAD_BODY",
    ))
    results.append(_probe(
        "bad_avatar_hue",
        client.request(
            "PATCH /api/me",
            "PATCH",
            "/api/me",
            body={"avatarHue": 361},
            token=token,
            record=False,
        ),
        status=422,
        code="BAD_BODY",
    ))
    results.append(_probe(
        "empty_display_name",
        client.request(
            "PATCH /api/me",
            "PATCH",
            "/api/me",
            body={"displayName": ""},
            token=token,
            record=False,
        ),
        status=422,
        code="BAD_BODY",
    ))
    results.append(_probe(
        "unknown_rating_table",
        client.request(
            "GET /api/rating",
            "GET",
            query("/api/rating", table="task:definitely-not-a-task"),
            token=token,
            record=False,
        ),
        status=404,
        code="NOT_FOUND",
    ))
    results.append(_probe(
        "unknown_news",
        client.request("GET /api/news/{id}", "GET", "/api/news/definitely-not-a-news", record=False),
        status=404,
        code="NOT_FOUND",
    ))
    results.append(_probe(
        "bad_username_register",
        client.request(
            "POST /api/auth/register",
            "POST",
            "/api/auth/register",
            body={
                "username": "bad name " + secrets.token_hex(6),
                "password": secrets.token_urlsafe(24),
            },
            record=False,
        ),
        status=422,
        code="BAD_BODY",
    ))
    results.append(_probe(
        "short_password_register",
        client.request(
            "POST /api/auth/register",
            "POST",
            "/api/auth/register",
            body={"username": "pwshort_" + secrets.token_hex(6), "password": "short"},
            record=False,
        ),
        status=422,
        code="BAD_BODY",
    ))
    results.append(_probe(
        "unknown_login",
        client.request(
            "POST /api/auth/login",
            "POST",
            "/api/auth/login",
            body={
                "username": "ghost_" + secrets.token_hex(6),
                "password": secrets.token_urlsafe(16),
            },
            record=False,
        ),
        status=401,
        code="BAD_CREDENTIALS",
    ))

    if len(accounts) > 1:
        results.append(_job_privacy(client, owner, accounts[1], task_path + "/run", source))

    return results


# -- runner -------------------------------------------------------------------
def run_abuse(
    accounts,
    *,
    base_url,
    workers,
    iterations,
    think_time,
    request_timeout,
    insecure,
    ca_bundle,
    verbose: bool = False,
) -> list[ProbeResult]:
    selected = list(accounts)[:2]
    if not selected:
        raise RuntimeError("at least one provisioned account is required for abuse probes")

    workers = max(1, int(workers))
    iterations = max(0, int(iterations))
    think_time = max(0.0, float(think_time))

    setup_client = Client(
        base_url,
        timeout=request_timeout,
        insecure=insecure,
        ca_bundle=ca_bundle,
    )
    try:
        _refresh_accounts(selected, setup_client, timeout=request_timeout, verbose=verbose)

        listing = setup_client.request(
            "SETUP /api/tasks",
            "GET",
            "/api/tasks",
            timeout=request_timeout,
            record=False,
        )
        if listing.status != 200 or not isinstance(listing.payload, list):
            raise RuntimeError(f"cannot read /api/tasks: HTTP {listing.status} {listing.code or ''}")
        tasks = [
            task for task in listing.payload
            if isinstance(task, dict) and task_key(task)
        ]
        if not tasks:
            raise RuntimeError("the deployment exposes no tasks")

        known = next(
            (
                task for task in tasks
                if task_key(task) in TASK_SOLUTIONS
                or str(task.get("slug") or "") in TASK_SOLUTIONS
            ),
            None,
        )
        task = known or tasks[0]
        task_id = task_key(task)
        task_path = "/api/tasks/" + quote(task_id, safe="")
        source = correct_source(task)
    finally:
        setup_client.close()

    results: list[ProbeResult] = []
    results_lock = threading.Lock()

    def worker(_index: int) -> None:
        client = Client(
            base_url,
            timeout=request_timeout,
            insecure=insecure,
            ca_bundle=ca_bundle,
        )
        try:
            for cycle in range(iterations):
                cycle_results = _cycle(client, selected, task_path, source)
                with results_lock:
                    results.extend(cycle_results)
                if verbose:
                    for result in cycle_results:
                        if result.skipped:
                            eprint(f"abuse: {result.name} skipped ({result.actual})")
                        elif not result.ok:
                            eprint(
                                f"abuse: {result.name} failed after {format_ms(result.elapsed_ms)}: "
                                f"{result.actual} (expected {result.expected})"
                            )
                if think_time and cycle + 1 < iterations:
                    time.sleep(think_time)
        finally:
            client.close()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(worker, index) for index in range(workers)]
        for future in futures:
            future.result()

    return results
