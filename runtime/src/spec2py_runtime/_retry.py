"""Retry policy: which failures to retry, and how long to wait between attempts."""

import email.utils
import random
import time
from dataclasses import dataclass
from typing import Final

import httpx2

__all__ = ["IDEMPOTENT_METHODS", "RetryPolicy"]

IDEMPOTENT_METHODS: Final = frozenset(
    {"GET", "HEAD", "OPTIONS", "PUT", "DELETE", "TRACE", "QUERY"}
)
_RETRY_STATUSES: Final = frozenset({408, 409, 429, 500, 502, 503, 504})


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential backoff with jitter that honours ``Retry-After``.

    Requests are retried only when they are safe to repeat: idempotent methods,
    or any method carrying an idempotency key. A server may force the decision
    either way with an ``x-should-retry: true|false`` header.
    """

    max_retries: int = 2
    initial_delay: float = 0.5
    max_delay: float = 8.0
    jitter: float = 0.25
    max_retry_after: float = 60.0
    retry_statuses: frozenset[int] = _RETRY_STATUSES

    def can_retry(
        self, request: httpx2.Request, idempotency_header: str | None
    ) -> bool:
        if request.method.upper() in IDEMPOTENT_METHODS:
            return True
        return bool(idempotency_header and idempotency_header in request.headers)

    def should_retry_response(self, response: httpx2.Response) -> bool:
        match response.headers.get("x-should-retry", "").lower():
            case "true":
                return True
            case "false":
                return False
            case _:
                return response.status_code in self.retry_statuses

    def delay(self, attempt: int, response: httpx2.Response | None = None) -> float:
        """Seconds to sleep before retry number ``attempt`` (0-based)."""
        if response is not None and (hinted := self._retry_after(response)) is not None:
            return hinted
        backoff = min(self.initial_delay * 2.0**attempt, self.max_delay)
        return backoff * (1 - self.jitter * random.random())

    def _retry_after(self, response: httpx2.Response) -> float | None:
        headers = response.headers
        seconds: float | None = None
        if (ms := headers.get("retry-after-ms")) is not None:
            try:
                seconds = float(ms) / 1000
            except ValueError:
                seconds = None
        if seconds is None and (value := headers.get("retry-after")) is not None:
            try:
                seconds = float(value)
            except ValueError:
                try:
                    parsed = email.utils.parsedate_to_datetime(value)
                except TypeError, ValueError:
                    parsed = None
                if parsed is not None:
                    seconds = max(parsed.timestamp() - time.time(), 0.0)
        if seconds is None or not 0 <= seconds <= self.max_retry_after:
            return None
        return seconds
