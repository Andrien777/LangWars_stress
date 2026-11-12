from __future__ import annotations

import json
import math
import os
import random
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def jitter(rng: random.Random, mean: float, sigma: float = 0.5, low: float = 0.0, high: float | None = None) -> float:
    """Lognormal positive delay around ``mean``, bounded for predictability."""
    if mean <= 0:
        return 0.0
    ceiling = high if high is not None else max(mean * 8.0, 1.0)
    return max(low, min(ceiling, rng.lognormvariate(math.log(mean), sigma)))


def percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, max(0, int(round((len(sorted_values) - 1) * fraction))))
    return sorted_values[index]


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        return None


def format_ms(value: float | None) -> str:
    if value is None:
        return "-"
    if value >= 1000:
        return f"{value / 1000:.2f}s"
    if value >= 100:
        return f"{value:.0f}ms"
    return f"{value:.1f}ms"


def format_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def private_file(path: Path) -> None:
    """Best-effort permissions for files containing test session tokens."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def write_json_atomic(path: Path, value) -> None:
    ensure_parent(path)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)
    private_file(path)


def slug_for_url(url: str) -> str:
    parts = urlsplit(url)
    pieces = [parts.scheme, parts.hostname or "host", str(parts.port or "")]
    path = parts.path.strip("/")
    if path:
        pieces.append(path)
    raw = "-".join(piece for piece in pieces if piece)
    return re.sub(r"[^A-Za-z0-9._-]+", "_", raw)[:120]


def eprint(*values) -> None:
    print(*values, file=sys.stderr, flush=True)


def mask_token(value: str | None) -> str:
    if not value:
        return "<none>"
    return value[:6] + "..." if len(value) > 9 else "<set>"
