#!/usr/bin/env python3
"""
theapk · ghostprint — minimal HTTP wrapper around tag-print.py.

Single Python file, stdlib only. Run alongside tag-print.py +
verify-print.py + src/ + requirements.txt:

    python3 landing/serve.py --port 8080 --landing-dir landing/

Then `curl -F stl=@master.stl http://127.0.0.1:8080/api/tag
     -F order_id=ORDER-2026-0001 -F printer=bambucco-1 -F job_seq=42
     -o tagged.stl` (manifest lands next to it as <out>.manifest.json).

The landing page (index.html) does the same upload from the browser via
XMLHttpRequest. No JS framework — just fetch().

Why no Flask/FastAPI: scrap-bin. stdlib + cgi + subprocess is enough for
a v0.1 drag-drop page. Move to FastAPI in v0.2 when we add auth + Stripe.
"""
from __future__ import annotations

import argparse
import base64
import cgi
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # ghostprint repo root
LANDING_DIR = Path(__file__).resolve().parent
STRIPE_CONFIG = LANDING_DIR / "stripe_config.json"


def _load_stripe_config() -> dict:
    """Read stripe_config.json once per process. Cached. Falls back to a
    minimal config if the file is missing (e.g. partial deploy)."""
    if not STRIPE_CONFIG.is_file():
        return {"plans": {}, "currency": "usd",
                "success_url": "", "cancel_url": "",
                "env_var": "STRIPE_SECRET_KEY"}
    try:
        return json.loads(STRIPE_CONFIG.read_text())
    except (json.JSONDecodeError, OSError):
        return {"plans": {}, "currency": "usd",
                "success_url": "", "cancel_url": "",
                "env_var": "STRIPE_SECRET_KEY"}


STRIPE_CFG = _load_stripe_config()


def _run_tag(stl_path: Path, order_id: str, printer: str, job_seq: int,
             out_dir: Path) -> dict:
    """Invoke tag-print.py against stl_path. Returns manifest dict on
    success, raises on failure."""
    out_stl = out_dir / "tagged.stl"
    manifest = out_dir / "tagged.stl.manifest.json"
    cmd = [
        sys.executable,
        str(ROOT / "tag-print.py"),
        str(stl_path),
        "--order-id", order_id,
        "--printer", printer,
        "--job-seq", str(job_seq),
        "--out", str(out_stl),
        "--emit-gcode",
        "--manifest", str(manifest),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"tag-print.py failed: {proc.stderr.strip()}")
    return json.loads(manifest.read_text())


class Handler(BaseHTTPRequestHandler):
    server_version = "ghostprint/0.1"

    # ---- static landing page --------------------------------------------
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            path = LANDING_DIR / "index.html"
            if not path.is_file():
                self.send_error(404, "landing page missing")
                return
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path == "/health":
            self._json(200, {"ok": True, "service": "ghostprint-landing",
                              "version": "0.1",
                              "stripe_configured":
                                bool(os.environ.get(STRIPE_CFG.get("env_var", "STRIPE_SECRET_KEY")))
                                and all(
                                    STRIPE_CFG.get("plans", {}).get(p, {}).get("price_id", "").startswith("price_")
                                    for p in STRIPE_CFG.get("plans", {})
                                )})
            return
        if self.path.startswith("/api/checkout"):
            self._handle_checkout()
            return
        # Serve any other static file from the landing dir if it exists
        # (images, css, future assets). Refuses path traversal.
        from urllib.parse import unquote
        rel = unquote(self.path.lstrip("/"))
        if rel and not rel.startswith(("..", "/")):
            asset = (LANDING_DIR / rel).resolve()
            if LANDING_DIR.resolve() in asset.parents and asset.is_file():
                import mimetypes
                mime = mimetypes.guess_type(str(asset))[0] or "application/octet-stream"
                data = asset.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(data)))
                # Long cache for static assets; index.html stays no-cache via the / branch above
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_error(404, "asset not found")
            return
        # Convenience redirect: any other GET → landing.
        self.send_response(302)
        self.send_header("Location", "/")
        self.end_headers()

    # ---- tag API --------------------------------------------------------
    def do_POST(self):
        if self.path != "/api/tag":
            self.send_error(404, "use POST /api/tag")
            return
        try:
            order_id, printer, job_seq, stl_bytes, stl_name = self._read_upload()
        except ValueError as e:
            self._json(400, {"error": str(e)})
            return

        # Sanity: STL files start with `solid` (ASCII) or 80-byte header (binary).
        if not (stl_bytes[:5].lower() == b"solid" or len(stl_bytes) > 84):
            self._json(400, {"error": "file does not look like an STL"})
            return

        work = Path(tempfile.mkdtemp(prefix="gp-tag-"))
        try:
            in_stl = work / (stl_name or "input.stl")
            in_stl.write_bytes(stl_bytes)
            manifest = _run_tag(in_stl, order_id, printer, job_seq, work)
            tagged = (work / "tagged.stl").read_bytes()
            gcode = (work / "tagged.gcode")
            gcode_bytes = gcode.read_bytes() if gcode.is_file() else b""

            # Response: a multipart-ish JSON for the SPA, plus raw files.
            self._json(200, {
                "manifest": manifest,
                "tagged_stl_b64": _b64(tagged),
                "tagged_gcode_b64": _b64(gcode_bytes) if gcode_bytes else None,
                "size_stl": len(tagged),
                "size_gcode": len(gcode_bytes),
                "download_name_stl": "tagged.stl",
                "download_name_gcode": "tagged.gcode",
                "download_name_manifest": "tagged.stl.manifest.json",
                "request_id": uuid.uuid4().hex,
            })
        except Exception as e:
            self._json(500, {"error": f"tag failed: {e}"})
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # ---- helpers --------------------------------------------------------
    def _read_upload(self):
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            raise ValueError("Content-Type must be multipart/form-data")
        # FieldStorage reads from rfile using environ. We build a minimal
        # one. cgi.FieldStorage is deprecated in 3.13 but still works on
        # 3.10/3.11/3.12. Fallback path: read raw bytes + simple parse.
        try:
            fs = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={"REQUEST_METHOD": "POST",
                          "CONTENT_TYPE": ctype},
            )
            order_id = fs.getfirst("order_id", "").strip()
            printer = fs.getfirst("printer", "").strip()
            job_seq_s = fs.getfirst("job_seq", "0").strip()
            stl_field = fs["stl"] if "stl" in fs else None
            if not order_id or not printer or stl_field is None:
                raise ValueError("missing order_id, printer, or stl file")
            try:
                job_seq = int(job_seq_s)
            except ValueError:
                raise ValueError("job_seq must be an integer")
            stl_bytes = stl_field.file.read()
            stl_name = stl_field.filename or "input.stl"
            return order_id, printer, job_seq, stl_bytes, stl_name
        except (ValueError, KeyError):
            raise

    def _json(self, code: int, obj: dict):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---- Stripe checkout (v0.1: stdlib-only, test mode only) -------------
    def _handle_checkout(self):
        """`GET /api/checkout?plan=<maker|studio>` returns a JSON body with
        `checkout_url`. Page does `window.location = url`. We do not redirect
        server-side because the page wants to handle the 503 case inline.

        Returns:
            200: {"checkout_url": "https://checkout.stripe.com/...", "plan": "maker"}
            400: {"error": "..."}                       — bad query
            503: {"error": "...", "vault_entry": "..."}  — key not configured
        """
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        plan = (qs.get("plan") or [""])[0].strip().lower()
        if plan not in STRIPE_CFG.get("plans", {}):
            self._json(400, {
                "error": f"unknown plan '{plan}'. allowed: {sorted(STRIPE_CFG.get('plans', {}).keys())}",
            })
            return

        env_var = STRIPE_CFG.get("env_var", "STRIPE_SECRET_KEY")
        secret = os.environ.get(env_var, "").strip()
        # Accept both sk_test_ (test mode) and sk_live_ (live mode). rk_*
        # (restricted agent keys) are NOT acceptable here because they cannot
        # create Checkout Sessions — only the full secret key can.
        if not secret or not (secret.startswith("sk_test_") or secret.startswith("sk_live_")):
            self._json(503, {
                "error": (f"Stripe not configured: set {env_var} env var with a "
                          f"sk_test_*** or sk_live_*** key."),
                "env_var": env_var,
                "plan": plan,
            })
            return

        plan_cfg = STRIPE_CFG["plans"][plan]
        price_id = plan_cfg.get("price_id", "").strip()
        if not price_id.startswith("price_"):
            self._json(503, {
                "error": (f"price_id for plan '{plan}' not set in "
                          f"landing/stripe_config.json (got '{price_id[:16]}...'). "
                          f"Create the product in the Stripe dashboard "
                          f"and paste the price_*** ID."),
                "plan": plan,
            })
            return

        # Build Checkout Session via Stripe API. POST as form-encoded.
        success = STRIPE_CFG.get("success_url", "") or "https://ghostprint.theapk.com/?checkout=success"
        cancel = STRIPE_CFG.get("cancel_url", "") or "https://ghostprint.theapk.com/?checkout=cancel"
        success = success.replace("{CHECKOUT_SESSION_ID}", "{CHECKOUT_SESSION_ID}")
        body = urllib.parse.urlencode({
            "mode": "subscription",
            "line_items[0][price]": price_id,
            "line_items[0][quantity]": "1",
            "success_url": success,
            "cancel_url": cancel,
            "allow_promotion_codes": "true",
            "billing_address_collection": "auto",
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.stripe.com/v1/checkout/sessions",
            data=body,
            headers={
                "Authorization": f"Bearer {secret}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                resp = json.loads(r.read())
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")
            self._json(502, {
                "error": f"Stripe rejected the checkout request: HTTP {e.code}",
                "stripe_error": err_body[:500],
                "plan": plan,
            })
            return
        except urllib.error.URLError as e:
            self._json(502, {
                "error": f"could not reach Stripe: {e.reason}",
                "plan": plan,
            })
            return

        url = resp.get("url", "")
        session_id = resp.get("id", "")
        if not url or not url.startswith("https://checkout.stripe.com/"):
            self._json(502, {
                "error": "Stripe response did not contain a checkout URL",
                "stripe_response": {k: resp.get(k) for k in ("id", "url", "status")},
                "plan": plan,
            })
            return

        self._json(200, {
            "checkout_url": url,
            "session_id": session_id,
            "plan": plan,
        })

    def log_message(self, fmt, *args):
        sys.stderr.write("[gp] " + (fmt % args) + "\n")


def _b64(b: bytes) -> str:
    import base64
    return base64.b64encode(b).decode("ascii")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()
    if not (ROOT / "tag-print.py").is_file():
        sys.exit(f"tag-print.py not found at {ROOT}/tag-print.py")
    print(f"[gp] serving on http://{args.host}:{args.port}", flush=True)
    print(f"[gp] landing: {LANDING_DIR / 'index.html'}", flush=True)
    print(f"[gp] tag-print: {ROOT / 'tag-print.py'}", flush=True)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.serve_forever()


if __name__ == "__main__":
    main()
