from __future__ import annotations

import random
import threading
import time
from collections import Counter
from dataclasses import dataclass, field

from .util import percentile, utc_now


class Histogram:
    """Count/sum plus a bounded reservoir for percentile estimates."""

    __slots__ = ("count", "total", "minimum", "maximum", "samples", "capacity", "_rng")

    def __init__(self, capacity: int = 100_000, seed: int = 0):
        self.count = 0
        self.total = 0.0
        self.minimum: float | None = None
        self.maximum: float | None = None
        self.samples: list[float] = []
        self.capacity = capacity
        self._rng = random.Random(seed)

    def add(self, value: float) -> None:
        self.count += 1
        self.total += value
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        if len(self.samples) < self.capacity:
            self.samples.append(value)
        else:
            index = self._rng.randrange(self.count)
            if index < self.capacity:
                self.samples[index] = value

    def snapshot(self) -> dict:
        values = sorted(self.samples)
        return {
            "count": self.count,
            "meanMs": round(self.total / self.count, 3) if self.count else 0.0,
            "minMs": round(self.minimum, 3) if self.minimum is not None else None,
            "p50Ms": round(percentile(values, 0.50), 3),
            "p95Ms": round(percentile(values, 0.95), 3),
            "p99Ms": round(percentile(values, 0.99), 3),
            "maxMs": round(self.maximum, 3) if self.maximum is not None else None,
        }


@dataclass
class OpStats:
    count: int = 0
    success: int = 0
    client_errors: int = 0
    server_errors: int = 0
    transport_errors: int = 0
    latency: Histogram = field(default_factory=Histogram)
    codes: Counter = field(default_factory=Counter)

    def record(self, status: int, elapsed_ms: float, code: str | None, transport_error: str | None) -> None:
        self.count += 1
        self.latency.add(elapsed_ms)
        if transport_error:
            self.transport_errors += 1
            self.codes["TRANSPORT:" + transport_error.split(":", 1)[0]] += 1
            return
        if 200 <= status < 400:
            self.success += 1
        elif 400 <= status < 500:
            self.client_errors += 1
            self.codes[code or f"HTTP_{status}"] += 1
        elif status >= 500:
            self.server_errors += 1
            self.codes[code or f"HTTP_{status}"] += 1

    def snapshot(self) -> dict:
        data = {
            "count": self.count,
            "success": self.success,
            "clientErrors": self.client_errors,
            "serverErrors": self.server_errors,
            "transportErrors": self.transport_errors,
            "codes": dict(self.codes.most_common(20)),
        }
        data.update({key: value for key, value in self.latency.snapshot().items() if key != "count"})
        return data


@dataclass
class JudgeStats:
    submitted: int = 0
    accepted: int = 0
    rejected: int = 0
    rejection_codes: Counter = field(default_factory=Counter)
    completed: int = 0
    failed: int = 0
    timeouts: int = 0
    verdicts: Counter = field(default_factory=Counter)
    errors: Counter = field(default_factory=Counter)
    polls: int = 0
    e2e: Histogram = field(default_factory=Histogram, metadata={"name": "e2e"})
    execution: Histogram = field(default_factory=Histogram)
    queue_estimate: Histogram = field(default_factory=Histogram)
    rating_delta: Histogram = field(default_factory=Histogram)

    def snapshot(self) -> dict:
        finished = self.completed + self.failed + self.timeouts
        return {
            "submitted": self.submitted,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "rejectionCodes": dict(self.rejection_codes.most_common(20)),
            "completed": self.completed,
            "failed": self.failed,
            "timeouts": self.timeouts,
            "inFlight": max(0, self.accepted - self.completed - self.failed - self.timeouts),
            "completionRatio": round(self.completed / finished, 4) if finished else None,
            "verdicts": dict(self.verdicts.most_common()),
            "errors": dict(self.errors.most_common(20)),
            "polls": self.polls,
            "e2e": self.e2e.snapshot(),
            "execution": self.execution.snapshot(),
            "queueEstimate": self.queue_estimate.snapshot(),
            "ratingDelta": self.rating_delta.snapshot(),
        }


class Recorder:
    def __init__(self, *, timeline: bool = True):
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.started_at = utc_now()
        self.ops: dict[str, OpStats] = {}
        self.judge = JudgeStats()
        self.auth_relogins = 0
        self.timeline: dict[int, list[int]] = {} if timeline else {}
        self.enabled_timeline = timeline

    def record(self, op: str, status: int, elapsed_ms: float, code: str | None, transport_error: str | None) -> None:
        with self.lock:
            stats = self.ops.get(op)
            if stats is None:
                stats = OpStats()
                self.ops[op] = stats
            stats.record(status, elapsed_ms, code, transport_error)
            if self.enabled_timeline:
                bucket = int(time.monotonic() - self.started)
                entry = self.timeline.setdefault(bucket, [0, 0])
                entry[0] += 1
                if transport_error or status >= 500:
                    entry[1] += 1

    def record_auth_relogin(self) -> None:
        with self.lock:
            self.auth_relogins += 1

    def job_submitted(self, mode: str) -> None:
        with self.lock:
            self.judge.submitted += 1

    def job_rejected(self, code: str | None) -> None:
        with self.lock:
            self.judge.rejected += 1
            self.judge.rejection_codes[code or "UNKNOWN"] += 1

    def job_accepted(self) -> None:
        with self.lock:
            self.judge.accepted += 1

    def job_poll(self) -> None:
        with self.lock:
            self.judge.polls += 1

    def job_completed(self, verdict: str | None, e2e_ms: float, execution_ms: float, rating_delta: float | None) -> None:
        with self.lock:
            self.judge.completed += 1
            self.judge.verdicts[verdict or "UNKNOWN"] += 1
            self.judge.e2e.add(e2e_ms)
            self.judge.execution.add(max(0.0, execution_ms))
            self.judge.queue_estimate.add(max(0.0, e2e_ms - execution_ms))
            if rating_delta:
                self.judge.rating_delta.add(rating_delta)

    def job_failed(self, code: str | None) -> None:
        with self.lock:
            self.judge.failed += 1
            self.judge.errors[code or "UNKNOWN"] += 1

    def job_timed_out(self) -> None:
        with self.lock:
            self.judge.timeouts += 1

    def finish(self) -> None:
        self.finished = time.monotonic()

    def snapshot(self) -> dict:
        elapsed = max(0.001, getattr(self, "finished", time.monotonic()) - self.started)
        with self.lock:
            ops = {name: stats.snapshot() for name, stats in self.ops.items()}
            total = sum(stats["count"] for stats in ops.values())
            client_errors = sum(stats["clientErrors"] for stats in ops.values())
            server_errors = sum(stats["serverErrors"] for stats in ops.values())
            transport_errors = sum(stats["transportErrors"] for stats in ops.values())
            timeline = []
            for bucket in sorted(self.timeline):
                count, errors = self.timeline[bucket]
                timeline.append({"second": bucket, "requests": count, "errors": errors})
            return {
                "startedAt": self.started_at,
                "elapsedSeconds": round(elapsed, 3),
                "requests": total,
                "requestsPerSecond": round(total / elapsed, 3),
                "clientErrors": client_errors,
                "serverErrors": server_errors,
                "transportErrors": transport_errors,
                "authRelogins": self.auth_relogins,
                "ops": ops,
                "judge": self.judge.snapshot(),
                "timeline": timeline,
            }
