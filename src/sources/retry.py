from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")

RETRY_COUNT = 3
BASE_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 60.0
RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


def _retry_after(response: object) -> float | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        return min(MAX_DELAY_SECONDS, max(0.0, float(value)))
    except (TypeError, ValueError):
        return None


def with_retries(
    operation: Callable[[], T],
    *,
    should_retry: Callable[[T], bool] | None = None,
    retry_exceptions: tuple[type[BaseException], ...] = (),
    retries: int = RETRY_COUNT,
    base_delay: float = BASE_DELAY_SECONDS,
    max_delay: float = MAX_DELAY_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    if retries < 0:
        raise ValueError("retries must not be negative")
    last_error: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            result = operation()
        except retry_exceptions as error:
            last_error = error
            if attempt == retries:
                raise
            delay = min(max_delay, base_delay * (2**attempt))
        else:
            if should_retry is None or not should_retry(result) or attempt == retries:
                return result
            delay = _retry_after(result)
            if delay is None:
                delay = min(max_delay, base_delay * (2**attempt))
        sleep(max(0.0, delay))
    if last_error is not None:
        raise last_error
    raise RuntimeError("retry loop exited without a result")
