from __future__ import annotations

import httpx
import pytest

from src.sources.retry import with_retries


def _response(status: int, retry_after: str | None = None) -> httpx.Response:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return httpx.Response(status, headers=headers, request=httpx.Request("GET", "https://example.test"))


def test_retries_retryable_status_three_times_with_exponential_backoff():
    attempts = []
    delays = []

    def operation():
        attempts.append(len(attempts))
        return _response(503 if len(attempts) < 4 else 200)

    result = with_retries(
        operation,
        should_retry=lambda response: response.status_code in {429, 500, 502, 503, 504},
        sleep=delays.append,
    )

    assert result.status_code == 200
    assert len(attempts) == 4
    assert delays == [1.0, 2.0, 4.0]


def test_retry_after_header_overrides_backoff():
    delays = []
    attempts = []

    def operation():
        attempts.append(1)
        return _response(429, "5") if len(attempts) == 1 else _response(200)

    result = with_retries(operation, should_retry=lambda response: response.status_code == 429, sleep=delays.append)

    assert result.status_code == 200
    assert delays == [5.0]


def test_nonretryable_status_returns_without_sleep():
    delays = []
    result = with_retries(lambda: _response(404), should_retry=lambda response: response.status_code >= 500, sleep=delays.append)
    assert result.status_code == 404
    assert delays == []


def test_retries_request_errors_then_returns_success():
    attempts = []
    delays = []

    def operation():
        attempts.append(1)
        if len(attempts) < 4:
            raise httpx.ConnectError("offline")
        return "ok"

    result = with_retries(operation, retry_exceptions=(httpx.RequestError,), sleep=delays.append)

    assert result == "ok"
    assert len(attempts) == 4
    assert delays == [1.0, 2.0, 4.0]


def test_final_request_error_is_raised_after_three_retries():
    attempts = []
    delays = []

    def operation():
        attempts.append(1)
        raise httpx.ConnectError("offline")

    with pytest.raises(httpx.ConnectError):
        with_retries(operation, retry_exceptions=(httpx.RequestError,), sleep=delays.append)

    assert len(attempts) == 4
    assert delays == [1.0, 2.0, 4.0]
