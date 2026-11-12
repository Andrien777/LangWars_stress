from __future__ import annotations

import http.client
import json
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit


class _NoBody:
    pass


NO_BODY = _NoBody()


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes
    elapsed_ms: float
    payload: Any = None
    code: str | None = None
    message: str | None = None
    transport_error: str | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")[:1000]

    def retry_after(self) -> float | None:
        value = self.headers.get("retry-after")
        if not value:
            return None
        try:
            return max(0.0, float(value.strip()))
        except ValueError:
            return None


class Client:
    """One keep-alive HTTP connection per thread.

    No third-party HTTP dependency is needed. The client records latency and
    error codes, but never logs request headers, credentials or response bodies.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        insecure: bool = False,
        ca_bundle: str | None = None,
        recorder=None,
        user_agent: str = "langwars-stress/1.0",
    ):
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"invalid base URL: {base_url!r}")
        if parts.query or parts.fragment:
            raise ValueError("base URL must not contain a query or fragment")
        self.base_url = base_url.rstrip("/")
        self.scheme = parts.scheme
        self.host = parts.hostname
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        self.base_path = parts.path.rstrip("/")
        self.timeout = timeout
        self.recorder = recorder
        self.user_agent = user_agent
        self._local = threading.local()
        if insecure:
            self._context = ssl._create_unverified_context()
        elif ca_bundle:
            self._context = ssl.create_default_context(cafile=ca_bundle)
        else:
            self._context = ssl.create_default_context()

    # -- connection management -------------------------------------------------
    def _new_connection(self):
        if self.scheme == "https":
            return http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=self._context)
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    def _connection(self):
        connection = getattr(self._local, "connection", None)
        last_used = getattr(self._local, "last_used", 0.0)
        # Uvicorn closes idle keep-alive connections after a few seconds. Do not
        # send a request into a socket the server is about to close; reconnect
        # proactively instead. This also avoids ambiguous POST retries.
        if connection is not None and time.monotonic() - last_used > 3.5:
            self._discard_connection()
            connection = None
        if connection is None:
            connection = self._new_connection()
            self._local.connection = connection
        return connection

    def _discard_connection(self):
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        self._local.connection = None

    def close(self):
        self._discard_connection()

    # -- requests --------------------------------------------------------------
    def request(
        self,
        op: str,
        method: str,
        path: str,
        *,
        body: Any = NO_BODY,
        raw_body: bytes | None = None,
        token: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        record: bool = True,
        retry_safe: bool = True,
    ) -> Response:
        method = method.upper()
        target = self.base_path + (path if path.startswith("/") else "/" + path)
        request_headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
            "Connection": "keep-alive",
        }
        if token:
            request_headers["Authorization"] = "Bearer " + token
        if raw_body is not None:
            data: bytes | None = raw_body
            request_headers["Content-Type"] = "application/json"
        elif body is not NO_BODY:
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        else:
            data = None
        if headers:
            request_headers.update(headers)

        response = self._request_once(method, target, data, request_headers, timeout)
        if response.transport_error and retry_safe and method in ("GET", "HEAD"):
            time.sleep(0.05)
            response = self._request_once(method, target, data, request_headers, timeout)
        if record and self.recorder is not None:
            self.recorder.record(op, response.status, response.elapsed_ms, response.code, response.transport_error)
        return response

    def _request_once(self, method: str, target: str, data: bytes | None, headers: dict[str, str], timeout: float | None) -> Response:
        started = time.perf_counter()
        try:
            connection = self._connection()
            if timeout is not None:
                connection.timeout = timeout
            else:
                connection.timeout = self.timeout
            connection.request(method, target, body=data, headers=headers)
            raw = connection.getresponse()
            payload_bytes = raw.read()
            self._local.last_used = time.monotonic()
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            response_headers = {key.lower(): value for key, value in raw.getheaders()}
            payload = None
            if payload_bytes:
                try:
                    payload = json.loads(payload_bytes)
                except (ValueError, UnicodeError):
                    payload = None
            code, message = _error_details(payload)
            return Response(raw.status, response_headers, payload_bytes, elapsed_ms, payload, code, message)
        except (http.client.HTTPException, OSError, ValueError) as error:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            self._discard_connection()
            self._local.last_used = time.monotonic()
            detail = f"{type(error).__name__}: {str(error)[:160]}"
            return Response(0, {}, b"", elapsed_ms, None, None, None, detail)


def _error_details(payload: Any) -> tuple[str | None, str | None]:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message")
            return (code if isinstance(code, str) else None,
                    message if isinstance(message, str) else None)
        if "detail" in payload:
            return "VALIDATION", None
    return None, None


def query(path: str, **params) -> str:
    values = {key: value for key, value in params.items() if value is not None}
    if not values:
        return path
    return path + ("&" if "?" in path else "?") + urlencode(values)
