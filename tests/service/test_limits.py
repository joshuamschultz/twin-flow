"""COMP-121 limits and COMP-120 per-route body limits (T-209, T-210).

Requirements: REQ-168 (size limits), REQ-175 (per-caller rate and concurrency).

Pinned token-bucket arithmetic (``CallerLimiter`` defaults: capacity = burst = 20,
refill = calls_per_minute / 60 = 2 tokens per second, bucket starts full):
- With a frozen clock, 20 immediate calls pass and the 21st is refused with
  ``retry_after_seconds == 0.5`` (time to refill one whole token).
- A caller whose burst is spent gets exactly 120 calls in the following 60 seconds when
  calling greedily on a 0.25 s grid (0.25 is exact in binary, so no float drift); the
  121st call at t = 60.0 s is refused. This is how "121st call in a minute" is read for a
  token bucket: a fresh caller may additionally spend its 20-call burst.
- Refused calls do not consume tokens (no debt); an idle bucket never exceeds 20.

Pinned body-limit routing: ``/api/workspace/schedules`` -> 64 MiB
(``SCHEDULE_BODY_LIMIT_BYTES``); ``/mcp`` and paths under ``/api/workspace/erp/`` ->
``max_mcp_request_bytes``; everything else -> ``max_request_bytes``. A 413 body carries
``code == "size_limit"``, the same code the MCP result-size guard uses.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from pydantic import BaseModel
from starlette.testclient import TestClient
from starlette.types import Receive, Scope, Send

from twinflow.service.limits import (
    CallerLimiter,
    RateLimited,
    ResultTooLarge,
    ensure_result_size,
)
from twinflow.service.middleware import SCHEDULE_BODY_LIMIT_BYTES, ServiceBoundaryMiddleware

MIB = 1024 * 1024


class FakeClock:
    """Injected monotonic clock; tests move time explicitly."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _drain(limiter: CallerLimiter, caller_id: str) -> int:
    """Call until refused; return how many calls passed."""
    passed = 0
    while True:
        try:
            limiter.check_call(caller_id)
        except RateLimited:
            return passed
        passed += 1


# --------------------------------------------------------------------------- token bucket


def test_burst_of_20_passes_and_21st_is_refused_with_retry_after() -> None:
    clock = FakeClock()
    limiter = CallerLimiter(clock=clock)
    for _ in range(20):
        limiter.check_call("arc")

    with pytest.raises(RateLimited) as excinfo:
        limiter.check_call("arc")

    assert excinfo.value.code == "rate_limited"
    assert excinfo.value.retry_after_seconds == pytest.approx(0.5)


def test_retry_after_reflects_partial_refill() -> None:
    clock = FakeClock()
    limiter = CallerLimiter(calls_per_minute=120, burst=20, clock=clock)
    _drain(limiter, "arc")
    clock.now = 0.1  # 0.2 token accrued, 0.8 missing at 2 tokens/s

    with pytest.raises(RateLimited) as excinfo:
        limiter.check_call("arc")

    assert excinfo.value.retry_after_seconds == pytest.approx(0.4)


def test_sustained_rate_is_120_per_minute_and_121st_is_refused() -> None:
    clock = FakeClock()
    limiter = CallerLimiter(calls_per_minute=120, burst=20, clock=clock)
    assert _drain(limiter, "arc") == 20  # burst spent at t=0

    passed = 0
    for tick in range(1, 241):  # t = 0.25 .. 60.0
        clock.now = tick * 0.25
        passed += _drain(limiter, "arc")

    assert passed == 120
    with pytest.raises(RateLimited):
        limiter.check_call("arc")  # the 121st inside the minute, still at t = 60.0


def test_refused_calls_do_not_consume_tokens() -> None:
    clock = FakeClock()
    limiter = CallerLimiter(clock=clock)
    _drain(limiter, "arc")
    for _ in range(50):
        with pytest.raises(RateLimited):
            limiter.check_call("arc")

    clock.now = 0.5

    limiter.check_call("arc")  # one whole token refilled despite 50 refusals


def test_idle_bucket_caps_at_burst() -> None:
    clock = FakeClock()
    limiter = CallerLimiter(clock=clock)
    _drain(limiter, "arc")

    clock.now = 3600.0

    assert _drain(limiter, "arc") == 20


def test_rate_limit_is_per_caller() -> None:
    clock = FakeClock()
    limiter = CallerLimiter(clock=clock)
    _drain(limiter, "flooder")

    assert _drain(limiter, "neighbour") == 20


def test_rate_limited_is_an_exception_with_code() -> None:
    assert issubclass(RateLimited, Exception)
    assert RateLimited.code == "rate_limited"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"calls_per_minute": 0},
        {"calls_per_minute": -1},
        {"burst": 0},
        {"max_concurrent_solves_per_caller": 0},
    ],
)
def test_limiter_refuses_non_positive_configuration(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        CallerLimiter(**kwargs)


# --------------------------------------------------------------------------- solve slots


def test_third_concurrent_solve_for_one_caller_is_refused_while_another_caller_proceeds() -> None:
    limiter = CallerLimiter(max_concurrent_solves_per_caller=2)
    both_inside = threading.Barrier(3, timeout=5)
    release = threading.Event()
    errors: list[BaseException] = []

    def hold_slot() -> None:
        try:
            with limiter.solve_slot("arc"):
                both_inside.wait()
                release.wait(timeout=5)
        except BaseException as exc:  # surfaced to the main thread below
            errors.append(exc)

    workers = [threading.Thread(target=hold_slot) for _ in range(2)]
    for worker in workers:
        worker.start()
    try:
        both_inside.wait()  # both slots for "arc" are held right now

        with pytest.raises(RateLimited) as excinfo, limiter.solve_slot("arc"):
            pass
        assert excinfo.value.code == "rate_limited"
        assert excinfo.value.retry_after_seconds > 0

        with limiter.solve_slot("other-caller"):
            pass
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=5)

    assert errors == []
    with limiter.solve_slot("arc"):  # slots were released on exit
        pass


def test_solve_slot_is_released_when_the_body_raises() -> None:
    limiter = CallerLimiter(max_concurrent_solves_per_caller=1)

    with pytest.raises(RuntimeError), limiter.solve_slot("arc"):
        raise RuntimeError("solver crashed")

    with limiter.solve_slot("arc"):
        pass


def test_refused_solve_slot_does_not_leak_a_slot() -> None:
    limiter = CallerLimiter(max_concurrent_solves_per_caller=1)
    with limiter.solve_slot("arc"):
        for _ in range(5):
            with pytest.raises(RateLimited), limiter.solve_slot("arc"):
                pass

    with limiter.solve_slot("arc"):
        pass


# --------------------------------------------------------------------------- result size


class _Result(BaseModel):
    rows: list[str]


def test_ensure_result_size_returns_serialized_json_bytes_for_model() -> None:
    model = _Result(rows=["a", "b"])

    payload = ensure_result_size(model, 1_048_576)

    assert isinstance(payload, bytes)
    assert json.loads(payload) == model.model_dump(mode="json")


def test_ensure_result_size_boundary_is_inclusive() -> None:
    exact = b"x" * 1_048_576

    assert ensure_result_size(exact, 1_048_576) == exact
    with pytest.raises(ResultTooLarge) as excinfo:
        ensure_result_size(exact + b"x", 1_048_576)
    assert excinfo.value.code == "size_limit"


def test_ensure_result_size_refuses_large_model_without_echoing_content() -> None:
    canary = "CANARY-RESULT-VALUE-tf005"
    model = _Result(rows=[canary] * 50_000)

    with pytest.raises(ResultTooLarge) as excinfo:
        ensure_result_size(model, 1_048_576)

    assert canary not in str(excinfo.value)
    assert ResultTooLarge.code == "size_limit"


# --------------------------------------------------------------------------- body limits


async def _counting_app(scope: Scope, receive: Receive, send: Send) -> None:
    received = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        received += len(message.get("body", b""))
        if not message.get("more_body", False):
            break
    body = json.dumps({"received": received}).encode()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": body})


@contextmanager
def _client(*, max_request_bytes: int, max_mcp_request_bytes: int) -> Iterator[TestClient]:
    app = ServiceBoundaryMiddleware(
        _counting_app,
        registry=None,
        max_request_bytes=max_request_bytes,
        max_mcp_request_bytes=max_mcp_request_bytes,
    )
    yield TestClient(app)  # no lifespan: the raw counting app only speaks HTTP


def _assert_size_error(response: Any) -> None:
    assert response.status_code == 413
    assert response.json()["code"] == "size_limit"
    assert response.json()["correlation_id"] == response.headers["x-correlation-id"]


@pytest.mark.parametrize(
    ("path", "limit"),
    [
        ("/api/workspace/scenarios", 100),
        ("/api/workspace/erpfoo", 100),  # prefix confusion: not under /erp/
        ("/api/run", 100),
        ("/mcp", 1000),
        ("/api/workspace/erp/validate", 1000),
        ("/api/workspace/erp/problems", 1000),
    ],
)
def test_body_limit_is_chosen_by_route(path: str, limit: int) -> None:
    with _client(max_request_bytes=100, max_mcp_request_bytes=1000) as client:
        at_limit = client.post(path, content=b"x" * limit)
        over = client.post(path, content=b"x" * (limit + 1))

    assert at_limit.status_code == 200
    assert at_limit.json() == {"received": limit}
    _assert_size_error(over)


def test_schedules_route_keeps_its_64_mib_limit() -> None:
    assert SCHEDULE_BODY_LIMIT_BYTES == 64 * MIB
    with _client(max_request_bytes=100, max_mcp_request_bytes=1000) as client:
        response = client.post("/api/workspace/schedules", content=b"x" * 5000)

    assert response.status_code == 200


def test_mcp_body_of_16_mib_passes_and_16_mib_plus_one_is_413() -> None:
    with _client(max_request_bytes=5 * MIB, max_mcp_request_bytes=16 * MIB) as client:
        at_limit = client.post("/mcp", content=b"x" * (16 * MIB))
        over = client.post("/mcp", content=b"x" * (16 * MIB + 1))

    assert at_limit.status_code == 200
    _assert_size_error(over)


def test_default_rest_body_over_5_mib_is_413_even_when_mcp_allows_more() -> None:
    with _client(max_request_bytes=5 * MIB, max_mcp_request_bytes=16 * MIB) as client:
        over = client.post("/api/workspace/scenarios", content=b"x" * (5 * MIB + 1))

    _assert_size_error(over)
