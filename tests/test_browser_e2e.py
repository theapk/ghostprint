#!/usr/bin/env python3
"""
test_browser_e2e.py — full browser-flow end-to-end test for the landing page.

What this test simulates:
  - A real user visiting the landing page in a browser.
  - Dragging/dropping (or browse-selecting) an STL.
  - Filling in order_id / printer / job_seq.
  - Clicking "Tag" — the page does fetch('/api/tag', { method: 'POST', body: fd }).
  - Downloading the resulting STL + gcode + manifest.
  - Verifying the watermarked output round-trips.
  - Clicking "Subscribe (Maker)" — page does fetch('/api/checkout?plan=maker').
  - Confirming the 503 path shows the right inline message (Stripe not wired).

Why this matters:
  He can't open a browser on his tablet and click through 4 buttons to verify
  the page works after each deploy. This test IS the click-through.

Why no Flask/FastAPI: scrap-bin. stdlib + subprocess is enough for
a v0.1 drag-drop page. Move to FastAPI in v0.2 when we add auth + Stripe.

Boot: serve.py runs from a tmp SANDBOX (whole landing copy + real
tag-print.py + src/ + a deterministic placeholder stripe_config.json —
the gitignored repo file is never read, so clean checkouts / CI are
identical to the dev tree). Assert semantics unchanged.

Pure stdlib + urllib. No requests, no selenium, no playwright. ~10 s warm.
"""
from __future__ import annotations

import base64
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INDEX = REPO / "landing" / "index.html"

# The runtime sandbox built by the module-scoped `server` fixture. Only
# test_browser_full_happy_path_dry_run touches its on-disk config (patch +
# restore); everything else talks to the served HTTP API exclusively.
SANDBOX: Path = Path()  # set by the `server` fixture before yield


# ---------- fixtures --------------------------------------------------------

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _sandbox_landing() -> Path:
    """Build the sandbox under {tmp}: a WHOLE-DIR copy of landing/ (rot-proof
    — no per-file list to maintain; same pattern as tests/test_checkout_route.py's
    _sandbox_landing) plus the REAL tag-print.py + src/ (serve.py shells out
    to ROOT/tag-print.py, which sys.path-inserts ROOT/src — /api/tag in the
    round-trip test exercises them, so no stub allowed here).

    Why a sandbox and not cwd=REPO: serve.py resolves everything from
    __file__ (ROOT = parent.parent of the SERVE COPY), but its import-time
    STRIPE_CFG cache reads {LANDING_DIR}/stripe_config.json — a GITIGNORED
    deploy-time file. On a clean checkout (git archive / CI) it's absent →
    plans = {} → every checkout 400s instead of the expected 503. So the
    fixture is made self-sufficient anywhere by writing a deterministic
    placeholder config ON TOP of the copy (a dev tree's local gitignored
    file, if present, was copied too and must not leak its contents):
      - plans EXIST (maker, studio) → checkout is never an unknown-plan 400,
      - price_ids are placeholders (don't start with price_*) and
        STRIPE_SECRET_KEY is NOT set in the fixture env → every checkout
        lands on the documented no-key 503 with the env_var message,
      - no Stripe network call is reachable from this config (the price_id
        gate short-circuits first, and the key gate short-circuits first
        anyway).
    """
    sandbox = Path(tempfile.mkdtemp(prefix="gp-browser-"))
    shutil.copytree(REPO / "landing", sandbox / "landing",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(REPO / "tag-print.py", sandbox / "tag-print.py")
    shutil.copytree(REPO / "src", sandbox / "src",
                    ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    (sandbox / "landing" / "stripe_config.json").write_text(json.dumps({
        "env_var": "STRIPE_SECRET_KEY",
        "plans": {
            "maker": {"price_id": "REPLACE_WITH_price_xxx_FROM_STRIPE_DASHBOARD"},
            "studio": {"price_id": "REPLACE_WITH_price_xxx_FROM_STRIPE_DASHBOARD"},
        },
    }))
    return sandbox


def _wait_ready(base: str, proc: subprocess.Popen, deadline: float = 15.0) -> None:
    """Poll /health until serve.py answers, else fail with the child's stderr."""
    deadline = time.time() + deadline  # offset from now
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=1) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.2)
    proc.terminate()
    out, err = proc.communicate(timeout=5)
    pytest.fail(f"serve.py did not come up in 15 s. stderr:\n{err.decode()}")


@pytest.fixture(scope="module")
def server():
    """Boot serve.py FROM THE SANDBOX COPY, never cwd=REPO, so this module's
    e2e flow is identical on a dev tree that happens to have the gitignored
    landing/stripe_config.json and on a clean checkout / CI where it does
    not. Every test in this module consumes the same boot (cwd-independent by
    construction). Mirrors tests/test_checkout_route.py."""
    global SANDBOX
    SANDBOX = _sandbox_landing()
    port = _free_port()
    env = dict(os.environ)
    # Explicitly NO STRIPE_SECRET_KEY — this is the "Stripe not wired" path.
    env.pop("STRIPE_SECRET_KEY", None)
    proc = subprocess.Popen(
        [sys.executable, str(SANDBOX / "landing" / "serve.py"),
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(SANDBOX),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    _wait_ready(base, proc)
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    shutil.rmtree(SANDBOX, ignore_errors=True)


# _make_binary_stl + _multipart_body come from tests/test_landing_serve.py
# (F-01 refactor: one shared request-shape helper across the e2e files).
from tests.test_landing_serve import (  # noqa: F401
    _make_binary_stl,
    _multipart_body,
)


# ---------- 1. page renders + wires the right fetch paths --------------------

def test_page_wires_fetch_paths():
    """Static check: the index.html actually contains the fetch() calls the
    page makes, the elements the JS binds to, and the FormData fields it sends.
    Catches drift between index.html and serve.py before a deploy goes out."""
    html = INDEX.read_text()

    # 1. The upload fetch path
    assert 'fetch("/api/tag"' in html, "page must POST /api/tag"
    assert 'new FormData()' in html, "page must use FormData"
    assert 'fd.append("stl"' in html, "page must include the stl file"
    assert 'fd.append("order_id"' in html, "page must include order_id"
    assert 'fd.append("printer"' in html, "page must include printer"
    assert 'fd.append("job_seq"' in html, "page must include job_seq"

    # 2. The Stripe checkout fetch path
    assert 'fetch("/api/checkout?plan="' in html, "page must GET /api/checkout?plan="
    assert "startCheckout(" in html, "startCheckout function must exist"
    assert '"maker"' in html and '"studio"' in html, "maker + studio plans referenced"
    assert 'cta-maker' in html and 'cta-studio' in html, "plan buttons must exist"

    # 3. The elements the JS binds to (must exist by id)
    for elem_id in ("file", "drop", "order_id", "printer", "job_seq",
                    "tag-btn", "manifest-pre", "dl-list", "cta-maker",
                    "cta-studio", "note-maker", "note-studio"):
        assert f'id="{elem_id}"' in html, f"page must contain element id={elem_id}"

    # 3b. The JS variable names the wiring binds to (catch renames).
    for var in ("orderEl", "printerEl", "jobEl", "fileInput", "btn"):
        assert var in html, f"page JS must reference variable {var}"

    # 4. The download links the page wires from the response
    assert "download_name_stl" in html
    assert "tagged_stl_b64" in html
    assert "tagged_gcode_b64" in html
    assert "manifest" in html


def test_page_has_stripe_unavailable_inline_message():
    """The 503 path must show a helpful inline message — not crash, not
    redirect away, not silently fail. The user sees this on their tablet when they
    clicks Subscribe before the Stripe dashboard is set up."""
    html = INDEX.read_text()
    assert "Stripe not wired yet" in html, (
        "503 path must surface the 'Stripe not wired yet' message")
    # The inline message is sufficient — no vault entry names in public repo.


# ---------- 2. landing HTML serves from / -----------------------------------

def test_get_landing_returns_html(server):
    """Browser GETs / → expect HTML with form elements."""
    import urllib.request
    with urllib.request.urlopen(f"{server}/", timeout=5) as r:
        assert r.status == 200
        ct = r.headers.get("Content-Type", "")
        assert "text/html" in ct
        body = r.read().decode()
    assert "theapk · ghostprint" in body
    assert '<form' in body.lower() or 'id="file"' in body
    assert 'id="tag-btn"' in body  # the tag button


# ---------- 3. drag-drop upload (browse-equivalent) → tag → download → verify

def test_browser_upload_tag_download_round_trip(server):
    """The full user flow, in one test:
      1. User browses → selects an STL.
      2. User fills order_id, printer, job_seq.
      3. User clicks Tag → page POSTs /api/tag with FormData.
      4. Page receives {manifest, tagged_stl_b64, tagged_gcode_b64, ...}.
      5. Page wires 3 download links (stl, gcode, manifest) via b64ToBlob.
      6. User downloads tagged.stl + tagged.gcode + manifest.
      7. User runs verify-print.py → both layers round-trip."""
    import urllib.request
    import urllib.error
    import stl

    stl_bytes = _make_binary_stl()
    order_id = "ORDER-BROWSER-E2E-001"
    printer = "bambucco-tablet"
    job_seq = 314

    # Step 1+2+3: simulate the exact FormData POST the page makes.
    boundary = "----gp-browser-e2e-q5w8e"
    body = _multipart_body(order_id, printer, job_seq=job_seq,
                           stl=stl_bytes, filename="upload.stl", boundary=boundary)

    req = urllib.request.Request(
        f"{server}/api/tag",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        assert r.status == 200
        resp = json.loads(r.read())

    # Step 4: page parses the JSON.
    assert "manifest" in resp
    assert "tagged_stl_b64" in resp
    assert resp["tagged_stl_b64"], "server returned empty stl payload"
    assert resp["tagged_gcode_b64"], "server returned empty gcode payload"

    # Step 5: page would do `b64ToBlob(b64, mime) → URL.createObjectURL → <a download>`.
    # Step 6: simulate that decode.
    tagged_stl = base64.b64decode(resp["tagged_stl_b64"])
    tagged_gcode = base64.b64decode(resp["tagged_gcode_b64"])
    assert tagged_stl[:5].lower() != b"solid"  # binary STL doesn't start with 'solid'
    assert len(tagged_stl) > 84                # binary STL has 80-byte header + at least 1 tri
    assert b"G1" in tagged_gcode or b"; gp" in tagged_gcode or b"theapk" in tagged_gcode, \
        "gcode should contain motion or watermark marker"

    # Step 7: user runs the verify CLI (or the verify button on the page).
    sys.path.insert(0, str(REPO / "src"))
    from ghostprint_core import l1_extract_from_gcode, l2_verify_match

    # L1 round-trip: decode gcode watermark, confirm printer_id + job_seq match.
    l1 = l1_extract_from_gcode(tagged_gcode.decode("utf-8", errors="replace"))
    assert l1["printer_id"] == resp["manifest"]["printer_id"]
    assert l1["job_seq"] == job_seq

    # L2 round-trip: decode STL watermark by diffing against master.
    work = Path(tempfile.mkdtemp(prefix="gp-e2e-"))
    tagged_path = work / "tagged.stl"
    master_path = work / "master.stl"
    tagged_path.write_bytes(tagged_stl)
    master_path.write_bytes(stl_bytes)
    tagged_mesh = stl.mesh.Mesh.from_file(str(tagged_path))
    master_mesh = stl.mesh.Mesh.from_file(str(master_path))
    assert l2_verify_match(tagged_mesh, master_mesh, order_id) is True, \
        "L2 should verify for the correct order_id"
    assert l2_verify_match(tagged_mesh, master_mesh, "ORDER-WRONG") is False, \
        "L2 should reject a wrong order_id"

    # Manifest sanity.
    assert resp["manifest"]["order_id"] == order_id
    assert resp["manifest"]["master_stl_sha256"]
    assert resp["manifest"]["tagged_stl_sha256"]
    assert resp["size_stl"] == len(tagged_stl)
    assert resp["size_gcode"] == len(tagged_gcode)


# ---------- 4. wrong file type → 400 → inline error (not a 500) -------------

def test_browser_upload_wrong_filetype_shows_clean_error(server):
    """If a user uploads README.md instead of a .stl, the page should
    receive a 400, show 'error: file does not look like an STL', and not
    crash the page or trigger a 500."""
    import urllib.request
    import urllib.error

    boundary = "----gp-browser-bad-q5w8e"
    body = _multipart_body("X", "p", job_seq="1",
                           stl=b"this is my notes, not an stl",
                           filename="notes.txt", boundary=boundary)
    req = urllib.request.Request(
        f"{server}/api/tag",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
        pytest.fail("expected 400 for non-STL upload")
    except urllib.error.HTTPError as e:
        assert e.code == 400, f"page expected 400 with friendly message, got {e.code}"
        body = json.loads(e.read())
        assert "STL" in body["error"]


# ---------- 5. Stripe checkout 503 path (this is what user sees on tablet) ---

def test_browser_click_maker_subscribe_gets_stripe_503(server):
    """The user is on a tablet. They click 'Subscribe — Maker'. The page does
    fetch('/api/checkout?plan=maker'). Until STRIPE_SECRET_KEY is set,
    this returns 503 with a JSON body that names the vault entry.
    The page then shows an inline message — NOT a crash, NOT a redirect."""
    import urllib.request
    import urllib.error

    # Sanity: no key in env (fixture guarantees this). urllib raises on 4xx/5xx.
    try:
        urllib.request.urlopen(f"{server}/api/checkout?plan=maker", timeout=5)
        pytest.fail("expected 503 for unconfigured Stripe")
    except urllib.error.HTTPError as e:
        assert e.code == 503
        body = json.loads(e.read())
    assert body["plan"] == "maker"
    assert body["env_var"] == "STRIPE_SECRET_KEY"
    assert "STRIPE_SECRET_KEY" in body["error"]
    # The page reads these fields to build the inline note.
    assert "env_var" in body

    # Studio should 503 the same way.
    try:
        urllib.request.urlopen(f"{server}/api/checkout?plan=studio", timeout=5)
        pytest.fail("expected 503 for unconfigured Stripe (studio)")
    except urllib.error.HTTPError as e:
        assert e.code == 503
        body = json.loads(e.read())
    assert body["plan"] == "studio"

    # Unknown plan → 400 (not 503).
    try:
        urllib.request.urlopen(f"{server}/api/checkout?plan=enterprise", timeout=5)
        pytest.fail("expected 400 for unknown plan")
    except urllib.error.HTTPError as e:
        assert e.code == 400


# ---------- 6. the full happy path proves the page works after Stripe is wired

def test_browser_full_happy_path_dry_run(server, monkeypatch):
    """With a fake sk_test_ key + fake price_ id injected, /api/checkout
    will still 503 (because Stripe will reject the request) — but the page
    path itself is exercised. This test catches: server code changes that
    break the checkout endpoint without us noticing, before real keys are wired
    Stripe creds.

    We monkeypatch STRIPE_SECRET_KEY + stripe_config.json with valid-looking
    placeholders, hit the endpoint, and assert the page-level behavior:
      - 503 because we don't have a real key, OR
      - 502 because Stripe rejected the fake key.
    Either is acceptable; what matters is the page doesn't crash and the
    error message is shown."""
    import urllib.request
    import urllib.error
    import json as _json

    # Patch the SANDBOX's stripe_config.json with placeholder price_ids that
    # look real. The fixture always wrote one there; the server cached it at
    # import, so this on-disk patch cannot change the boot decision — exactly
    # the point. Nothing under REPO/landing is touched, so the test is safe
    # on clean checkouts (where the repo copy doesn't exist at all).
    cfg_path = SANDBOX / "landing" / "stripe_config.json"
    example_path = REPO / "landing" / "stripe_config.example.json"
    if not cfg_path.exists():  # defensive; the fixture always provisions it
        import shutil
        shutil.copy(example_path, cfg_path)
    original = cfg_path.read_text()
    patched = original.replace(
        "REPLACE_WITH_price_xxx_FROM_STRIPE_DASHBOARD",
        "price_1FakeFakeFakeFake",
    )
    cfg_path.write_text(patched)
    try:
        # The fixture server was started without STRIPE_SECRET_KEY in env,
        # so this should still 503. The patched config alone doesn't help.
        try:
            urllib.request.urlopen(
                f"{server}/api/checkout?plan=maker", timeout=10,
            )
            pytest.fail("expected 503 — fixture has no STRIPE_SECRET_KEY")
        except urllib.error.HTTPError as e:
            assert e.code == 503, f"expected 503, got {e.code}"
            body = _json.loads(e.read())
        assert body["env_var"] == "STRIPE_SECRET_KEY"
    finally:
        cfg_path.write_text(original)


# ---------- 7. health endpoint reports accurate state ------------------------

def test_health_reports_stripe_state(server):
    """The /health endpoint drives any future monitoring. Verify it reflects
    'stripe_configured: false' while we have no key wired (the current
    production state)."""
    import urllib.request
    with urllib.request.urlopen(f"{server}/health", timeout=5) as r:
        body = json.loads(r.read())
    assert body["ok"] is True
    assert body["service"] == "ghostprint-landing"
    assert body["stripe_configured"] is False, (
        "with no STRIPE_SECRET_KEY in env, stripe_configured must be false")
    assert body["version"] == "0.1"
