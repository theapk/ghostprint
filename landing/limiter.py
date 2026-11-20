#!/usr/bin/env python3
"""theapk · ghostprint — minimal per-IP rate limiter for landing/serve.py.

Sliding-window counter keyed by client IP: 10 POST attempts per 60 s
window (rejected 400s count too — the point is costing attempts, not
accepting them), thread-safe, stdlib only. Keying is a serve.py decision:
this class is key-agnostic (any hashable string). serve.py keys on the
raw socket peer, EXCEPT when that peer is the Caddy edge (EDGE_IP,
Tailscale) — there it keys on the X-Real-IP the edge set at the front
door. X-Forwarded-For is never consulted (client-supplied, trivially
spoofable); a non-edge client cannot choose its key.

Memory is bounded: tracked keys are hard-capped (scanner floods of
distinct IPs get swept first for expired windows, then evicted oldest).
Behind Caddy the container peer is the edge's IP — serve.py therefore
keys edge traffic on X-Real-IP for real per-client fairness. Rate
limiting at the edge stays the outer tier; this limiter is the
in-process backstop.
"""
from __future__ import annotations

import threading
import time


class SlidingWindow:
    def __init__(self, max_events=10, window_s=60.0, max_keys=100_000):
        self._mx, self._win, self._hits, self._lk = (
            max_events, window_s, {}, threading.Lock())
        self._max_keys = max_keys  # bound on distinct tracked peers

    def _sweep_locked(self, now):
        """No-op until the key cap is reached; then purge expired windows
        first, then forcibly evict keys (bounded worst case: an evicted
        live client loses its count and gets a fresh window)."""
        if len(self._hits) < self._max_keys:
            return
        for k in [k for k, ts in self._hits.items()
                  if not ts or now - ts[-1] >= self._win]:
            del self._hits[k]
        while len(self._hits) >= self._max_keys and self._hits:
            self._hits.pop(next(iter(self._hits)))

    def allow(self, key):
        now = time.monotonic()
        with self._lk:
            self._sweep_locked(now)
            hits = [t for t in self._hits.setdefault(key, []) if now - t < self._win]
            if len(hits) >= self._mx:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True


# Shared instance served to do_POST: 10 POST attempts per 60 s per raw peer IP.
RATE = SlidingWindow(10, 60.0)
