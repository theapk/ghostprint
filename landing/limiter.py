#!/usr/bin/env python3
"""theapk · ghostprint — minimal per-IP rate limiter for landing/serve.py.

Sliding-window counter keyed by client IP: 10 accepted POSTs per 60 s
window, thread-safe, stdlib only. Keys are the raw socket peer address —
never X-Forwarded-For / X-Real-IP (client-supplied, trivially spoofable;
the whole point is to cost an attacker something, so the key must be
something the attacker does not choose).
"""
from __future__ import annotations

import threading
import time


class SlidingWindow:
    def __init__(self, max_events=10, window_s=60.0):
        self._mx, self._win, self._hits, self._lk = max_events, window_s, {}, threading.Lock()

    def allow(self, key):
        now = time.monotonic()
        with self._lk:
            hits = [t for t in self._hits.setdefault(key, []) if now - t < self._win]
            if len(hits) >= self._mx:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True


# Shared instance served to do_POST: 10 accepted POSTs per minute per IP.
RATE = SlidingWindow(10, 60.0)
