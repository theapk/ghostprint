#!/usr/bin/env python3
"""test_limiter.py — unit tests for the sliding-window per-IP rate limiter.

Pure unit tests on the class (no HTTP loop; an 11-request e2e loop would
just slow the suite — the do_POST wiring is covered by the serve import
identity check plus code review). stdlib only.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from landing.limiter import RATE, SlidingWindow  # noqa: E402


def test_tenth_allowed_eleventh_rejected():
    """max_events=10 / 60s: allows 10, then blocks the 11th."""
    sw = SlidingWindow(max_events=10, window_s=60.0)
    results = [sw.allow("1.2.3.4") for _ in range(11)]
    assert results == [True] * 10 + [False]
    # A *different* key is unaffected (per-IP isolation).
    assert sw.allow("5.6.7.8") is True


def test_window_expiry_restores_allow():
    """A tiny 0.05 s window expires: after the block, waiting out the
    window re-allows."""
    sw = SlidingWindow(max_events=2, window_s=0.05)
    assert sw.allow("k") is True
    assert sw.allow("k") is True
    assert sw.allow("k") is False          # 2/2 in-window
    assert sw.allow("k") is False          # still full
    time.sleep(0.06)                        # window slides past the first hits
    assert sw.allow("k") is True


def test_serve_wires_the_shared_rate_singleton():
    """serve.py must import the SAME RATE instance the limiter module
    owns (one shared state — no accidental duplicate window)."""
    import landing.serve as serve_mod
    assert serve_mod.RATE is RATE
    assert isinstance(RATE, SlidingWindow)
    assert RATE._mx == 10 and RATE._win == 60.0


def test_key_cap_sweep_bounded_memory():
    """Distinct-key growth is bounded: beyond max_keys, expired windows
    are purged and then the oldest slots are forcibly evicted. Allow()
    must never crash and the tracked-key count must stay under the cap."""
    sw = SlidingWindow(max_events=2, window_s=0.05, max_keys=3)
    for i in range(6):  # 6 distinct keys > cap 3, all allowed
        assert sw.allow(f"10.0.0.{i}") is True
    with sw._lk:
        assert 0 < len(sw._hits) <= 3
    # eviction never wedges state: the limiter still admits new traffic
    assert sw.allow("10.0.0.99") is True

    # expired keys are swept preferentially: churn past the cap, then
    # after one window the old keys are gone; state stays bounded.
    sw2 = SlidingWindow(max_events=1, window_s=0.05, max_keys=2)
    assert sw2.allow("a") and sw2.allow("b")
    assert sw2.allow("c") is True          # 3rd key pushes past cap
    time.sleep(0.06)                        # windows expire
    assert sw2.allow("d") is True
    with sw2._lk:
        assert len(sw2._hits) <= 2
