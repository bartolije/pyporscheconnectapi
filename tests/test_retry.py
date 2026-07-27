"""Unit tests for the shared retry policy (no network, no respx)."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from pyporscheconnectapi.exceptions import PorscheExceptionError
from pyporscheconnectapi.retry import (
    MAX_RETRY_DELAY,
    compute_retry_delay,
    send_with_retries,
)


def _response(status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, headers=headers or {})


@pytest.fixture(autouse=True)
def _instant_sleep(monkeypatch):
    """Make asyncio.sleep instant so retry tests don't burn real seconds."""
    real_sleep = asyncio.sleep

    async def _instant(_delay):
        await real_sleep(0)

    monkeypatch.setattr("pyporscheconnectapi.retry.asyncio.sleep", _instant)


# -- compute_retry_delay ----------------------------------------------------


def test_retry_after_header_is_honored():
    assert compute_retry_delay(_response(429, {"Retry-After": "5"}), attempt=0) == 5.0


def test_retry_after_header_is_capped():
    assert compute_retry_delay(_response(429, {"Retry-After": "3600"}), attempt=0) == MAX_RETRY_DELAY


def test_non_numeric_retry_after_falls_back_to_backoff():
    delay = compute_retry_delay(_response(503, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), attempt=2)
    assert 4.0 <= delay <= 4.3  # 2**2 + jitter(0-0.3)


def test_missing_response_uses_backoff():
    delay = compute_retry_delay(None, attempt=0)
    assert 1.0 <= delay <= 1.3


def test_backoff_is_capped_at_large_attempts():
    assert compute_retry_delay(None, attempt=10) == MAX_RETRY_DELAY


# -- send_with_retries ------------------------------------------------------


def _sender(outcomes: list):
    """Build a send() callable yielding each outcome in turn (exception or response)."""
    calls = {"count": 0}

    async def send() -> httpx.Response:
        outcome = outcomes[calls["count"]]
        calls["count"] += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return send, calls


async def test_immediate_success_returns_first_response():
    send, calls = _sender([_response(200)])
    resp = await send_with_retries(send, description="test")
    assert resp.status_code == 200
    assert calls["count"] == 1


async def test_transient_statuses_are_retried_until_success():
    send, calls = _sender([_response(503), _response(429), _response(200)])
    resp = await send_with_retries(send, description="test")
    assert resp.status_code == 200
    assert calls["count"] == 3


async def test_non_transient_status_is_returned_without_retry():
    for status in (400, 401, 403):
        send, calls = _sender([_response(status)])
        resp = await send_with_retries(send, description="test")
        assert resp.status_code == status
        assert calls["count"] == 1


async def test_exhausted_budget_returns_last_transient_response():
    send, calls = _sender([_response(503)] * 4)
    resp = await send_with_retries(send, description="test")
    assert resp.status_code == 503
    assert calls["count"] == 4  # initial + 3 retries


async def test_transport_error_then_success():
    send, calls = _sender([httpx.ConnectError("boom"), _response(200)])
    resp = await send_with_retries(send, description="test")
    assert resp.status_code == 200
    assert calls["count"] == 2


async def test_persistent_transport_error_raises_wrapped():
    send, calls = _sender([httpx.ConnectError("boom")] * 4)
    with pytest.raises(PorscheExceptionError) as exc_info:
        await send_with_retries(send, description="test")
    assert "transport error" in exc_info.value.message
    assert calls["count"] == 4
