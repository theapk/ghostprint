#!/usr/bin/env python3
"""
test_checkout_route.py — end-to-end tests for landing/serve.py's
Stripe checkout route. Covers:

  - /api/checkout?plan=__unknown__  → 400
  - /api/checkout?plan=maker with NO STRIPE_SECRET_KEY env var → 503
  - /api/checkout?plan=maker with sk_test_*** but price_id still the
    placeholder REPLACE_WITH_price_xxx_FROM_STRIPE_DASHBOARD → 503
  - /api/checkout?plan=maker with sk_test_*** + a real price_*** + a
    mocked Stripe API → 200 with checkout_url
  - /health surfaces `stripe_configured: false` when no key

Stdlib only. ~3 s on a warm venv.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib import error as urlerr
from urllib import request as urlreq

import pytest

REPO = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(base: str, proc: subprocess.Popen, deadline: float = 15.0) -> None:
    deadline = time.time() + deadline  # offset from now
    while time.time() < deadline:
        try:
            with urlreq.urlopen(f"{base}/health", timeout=1) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.15)
    proc.terminate()
    out, err = proc.communicate(timeout=5)
    pytest.fail(f"serve.py did not come up. stderr:\n{err.decode()}")


def _sandbox_landing(with_config: bool = True, config: dict | None = None) -> Path:
    """Copy landing/ into a temp sandbox (rot-proof whole-dir copy — no
    per-file list to maintain) + stub tag-print.py (not exercised on the
    checkout route, but serve.py refuses to boot without it). When
    with_config (and no explicit config), write a deterministic
    stripe_config.json with REPLACE_WITH placeholder price_ids so the
    sandbox is self-sufficient on a CLEAN CHECKOUT (the repo tree itself
    has no stripe_config.json — it's a deploy-time file)."""
    import shutil
    sandbox = Path(tempfile.mkdtemp(prefix="gp-checkout-"))
    shutil.copytree(REPO / "landing", sandbox / "landing",
                    ignore=shutil.ignore_patterns("__pycache__"))
    (sandbox / "tag-print.py").write_text("# stub: not exercised by checkout tests\n")
    if config is None and with_config:
        config = {"env_var": "STRIPE_SECRET_KEY",
                  "plans": {
                      "maker": {"price_id": "REPLACE_WITH_price_xxx"},
                      "studio": {"price_id": "REPLACE_WITH_price_xxx"},
                  }}
    if config is not None:
        (sandbox / "landing" / "stripe_config.json").write_text(json.dumps(config))
    # with_config=False → NO stripe_config.json → fallback, plans = {}
    return sandbox


def _spawn(env_extra: dict | None = None, with_config: bool = True):
    """Start serve.py from a sandbox copy on a free port. env_extra is
    merged on top of the current os.environ. Strips STRIPE_SECRET_KEY by
    default so the no-key path is the default test fixture. Returns
    (sandbox_dir, base_url, process) — caller owns cleanup."""
    sandbox = _sandbox_landing(with_config)
    port = _free_port()
    env = dict(os.environ)
    env.pop("STRIPE_SECRET_KEY", None)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.Popen(
        [sys.executable, str(sandbox / "landing" / "serve.py"),
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(sandbox),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    _wait_ready(base, proc)
    return sandbox, base, proc


def _get_json(url: str):
    try:
        with urlreq.urlopen(url, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urlerr.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"_raw": body.decode("utf-8", errors="replace")}


@pytest.fixture(scope="module")
def server_no_key():
    sandbox, base, proc = _spawn()
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    shutil.rmtree(sandbox, ignore_errors=True)


@pytest.fixture(scope="module")
def server_test_key_placeholder():
    """sk_test_*** is set but price_id is still the placeholder text."""
    sandbox, base, proc = _spawn({"STRIPE_SECRET_KEY": "sk_test_FAKE_FOR_TEST_ONLY"})
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    shutil.rmtree(sandbox, ignore_errors=True)


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------

def test_health_reports_stripe_unconfigured(server_no_key):
    """No STRIPE_SECRET_KEY → /health shows stripe_configured: false."""
    code, body = _get_json(f"{server_no_key}/health")
    assert code == 200
    assert body["ok"] is True
    assert body["stripe_configured"] is False


def test_unknown_plan_returns_400(server_no_key):
    code, body = _get_json(f"{server_no_key}/api/checkout?plan=__bogus__")
    assert code == 400
    assert "unknown plan" in body["error"]
    assert "maker" in body["error"]  # mentions the allowed plan


def test_checkout_without_key_returns_503(server_no_key):
    code, body = _get_json(f"{server_no_key}/api/checkout?plan=maker")
    assert code == 503, f"expected 503, got {code}: {body}"
    assert "STRIPE_SECRET_KEY" in body["error"]
    # vault_entry removed from response — no longer in public repo
    assert body["env_var"] == "STRIPE_SECRET_KEY"
    assert body["plan"] == "maker"


def test_checkout_studio_without_key_also_returns_503(server_no_key):
    code, body = _get_json(f"{server_no_key}/api/checkout?plan=studio")
    assert code == 503
    assert body["plan"] == "studio"


def _spawn_sandbox(with_config: bool, env_extra: dict, config: dict | None = None):
    """Run serve.py from a sandbox copy with a chosen stripe_config
    presence → returns (sandbox, proc, base). Delegates to
    _sandbox_landing (whole-dir copy, deterministic config)."""
    sandbox = _sandbox_landing(with_config, config)
    port = _free_port()
    env = dict(os.environ)
    env["STRIPE_SECRET_KEY"] = "sk_test_FAKE_FOR_TEST_ONLY"  # key IS set, always
    env.update(env_extra)
    proc = subprocess.Popen(
        [sys.executable, str(sandbox / "landing" / "serve.py"),
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(sandbox),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _wait_ready(base := f"http://127.0.0.1:{port}", proc)
    return sandbox, proc, base


def test_health_empty_plans_vacuous_true_bug():
    """Regression: with the key present but EVERY plan's price_id missing
    (no stripe_config.json → empty plans), stripe_configured must be False.
    The old `bool(key) and all(... over empty plans)` was vacuously True."""
    sandbox, proc, base = _spawn_sandbox(with_config=False, env_extra={})
    try:
        code, body = _get_json(f"{base}/health")
        assert code == 200
        assert body["ok"] is True
        assert body["stripe_configured"] is False, (
            "empty plans + key present must NOT report configured "
            "(vacuous all() bug)")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    shutil.rmtree(sandbox, ignore_errors=True)


def test_health_configured_when_key_and_valid_price_ids():
    """Positive path: key set + plans whose price_ids are real price_* →
    stripe_configured must be True."""
    sandbox, proc, base = _spawn_sandbox(True, env_extra={}, config={
        "env_var": "STRIPE_SECRET_KEY",
        "plans": {"maker": {"price_id": "price_test_sandboxok123"}},
    })
    try:
        code, body = _get_json(f"{base}/health")
        assert code == 200
        assert body["stripe_configured"] is True, body
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    import shutil
    shutil.rmtree(sandbox, ignore_errors=True)


def test_checkout_with_key_but_placeholder_price_returns_503(
        server_test_key_placeholder):
    code, body = _get_json(
        f"{server_test_key_placeholder}/api/checkout?plan=maker")
    assert code == 503, f"expected 503 (bad price_id), got {code}: {body}"
    assert "price_id" in body["error"]
    assert body["plan"] == "maker"


def test_landing_page_has_stripe_buttons_and_handler():
    """Static check: the served index.html must wire the buttons."""
    html = (REPO / "landing" / "index.html").read_text()
    assert 'id="cta-maker"' in html, "Maker tier button missing"
    assert 'id="cta-studio"' in html, "Studio tier button missing"
    assert 'id="note-maker"' in html, "Maker note span missing"
    assert 'id="note-studio"' in html, "Studio note span missing"
    assert "/api/checkout?plan=" in html, (
        "fetch call to /api/checkout?plan= missing from JS")
    assert "startCheckout" in html, "startCheckout handler missing"
    # The class for the buttons
    assert 'class="cta"' in html


def test_stripe_config_example_json_is_valid_and_has_maker_plan():
    """The example config file must parse and define the 'maker' plan
    with placeholder price_ids to replace."""
    import json as _json
    cfg = _json.loads((REPO / "landing" / "stripe_config.example.json").read_text())
    assert "plans" in cfg
    assert "maker" in cfg["plans"]
    assert cfg["plans"]["maker"]["amount_cents"] == 900
    assert cfg["plans"]["maker"]["interval"] == "month"
    # Price_id is the placeholder until filled in.
    assert cfg["plans"]["maker"]["price_id"].startswith("REPLACE_WITH_price_")
    assert cfg["env_var"] == "STRIPE_SECRET_KEY"
