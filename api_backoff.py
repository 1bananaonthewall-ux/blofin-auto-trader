"""Thread-safe global API pause after Blofin / Cloudflare 429 rate limits."""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Mapping

log = logging.getLogger(__name__)

_lock = threading.Lock()
_pause_until = 0.0
_last_warn_at = 0.0

DEFAULT_SEC = 180.0
MIN_SEC = 120.0
MAX_SEC = 3600.0
_WARN_INTERVAL = 120.0


class RateLimitPaused(Exception):
    """Raised when outbound API calls are blocked by global backoff."""

    def __init__(self, seconds_left: float, source: str = "") -> None:
        self.seconds_left = max(0.0, float(seconds_left))
        self.source = source
        super().__init__(
            f"API rate limited ({source or 'unknown'}): {self.seconds_left:.0f}s remaining"
        )


def is_paused() -> bool:
    return seconds_left() > 0


def seconds_left() -> float:
    with _lock:
        return max(0.0, _pause_until - time.time())


def parse_retry_after(
    headers: Mapping[str, Any] | None,
    status_code: int | None = None,
    body_text: str | None = None,
) -> float | None:
    """Parse Retry-After from response headers or Cloudflare HTML body."""
    if headers:
        for key in ("Retry-After", "retry-after"):
            raw = headers.get(key)
            if raw is None:
                continue
            try:
                return float(raw)
            except (TypeError, ValueError):
                continue
    text = body_text or ""
    if status_code == 429 or "1015" in text or "429" in text:
        m = re.search(r"retry[- ]after[:\s]+(\d+)", text, re.I)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    return None


def register_429(retry_after_sec: float | None, source: str = "") -> float:
    """
    Extend global pause until timestamp (max wins).
    Returns seconds remaining after registration.
    """
    global _pause_until, _last_warn_at

    sec = float(retry_after_sec if retry_after_sec is not None else DEFAULT_SEC)
    sec = max(MIN_SEC, min(MAX_SEC, sec))
    until = time.time() + sec

    should_warn = False
    with _lock:
        prev_until = _pause_until
        _pause_until = max(_pause_until, until)
        remaining = max(0.0, _pause_until - time.time())
        if _pause_until > prev_until and time.time() - _last_warn_at >= _WARN_INTERVAL:
            _last_warn_at = time.time()
            should_warn = True

    if should_warn:
        log.warning(
            "API rate limit pause (%s) — backoff %.0fs (%.0fs remaining)",
            source or "unknown",
            sec,
            remaining,
        )
    return remaining


def register_short_pause(sec: float, source: str = "") -> float:
    """Brief global pause for transient exchange faults (e.g. TPSL price feed missing)."""
    global _pause_until, _last_warn_at

    sec = max(15.0, min(90.0, float(sec)))
    until = time.time() + sec
    should_warn = False
    with _lock:
        prev_until = _pause_until
        _pause_until = max(_pause_until, until)
        remaining = max(0.0, _pause_until - time.time())
        if _pause_until > prev_until and time.time() - _last_warn_at >= _WARN_INTERVAL:
            _last_warn_at = time.time()
            should_warn = True
    if should_warn:
        log.warning(
            "API short pause (%s) — backoff %.0fs (%.0fs remaining)",
            source or "transient",
            sec,
            remaining,
        )
    return remaining
