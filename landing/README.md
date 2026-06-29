# theapk · ghostprint — landing page

Drag-drop web tool for the ghostprint CLI. Single-file HTML + stdlib-only
Python wrapper. No JS framework, no CDN, no Tailwind, no paid SaaS.

## What's in here

| File | Purpose |
|---|---|
| `index.html` | Single-file landing page. Drag-drop, fill order_id/printer/job_seq, click Tag STL, download. Vanilla JS, fetch() to `/api/tag`. Stripe-tier buttons call `/api/checkout?plan=...`. |
| `serve.py` | `ThreadingHTTPServer` + `BaseHTTPRequestHandler`. Serves `index.html`, exposes `POST /api/tag` that shells out to `tag-print.py` and returns tagged STL + gcode + manifest. Also exposes `GET /api/checkout?plan=<maker\|studio>` which calls Stripe Checkout API (stdlib urllib, no SDK) and returns the checkout URL. |
| `stripe_config.json` | Catalog of plans + price_ids. Create your own products in the Stripe dashboard and fill in the `price_***` ids. See template below. |

## Local dev

```bash
git clone https://github.com/theapk/ghostprint.git
cd ghostprint
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 landing/serve.py --host 127.0.0.1 --port 8080
# open http://127.0.0.1:8080
```

End-to-end test:
```bash
python3 -m pytest tests/test_landing_serve.py -v
# 3 passed in 0.5 s
```

## Stripe setup (optional)

The page has a Subscribe button on the Maker + Studio tiers. The button
calls `GET /api/checkout?plan=<maker|studio>`. The server route calls
the Stripe Checkout API with stdlib `urllib.request` (no SDK). Returns
the checkout URL → JS does `window.location = url`.

**Without a Stripe key configured, clicking Subscribe shows a clear
"Stripe not wired yet" message inline on the page (no crash, no
console error, the test suite verifies this).**

To enable Stripe:

1. Create products in your Stripe dashboard (test mode first).
2. Create a `stripe_config.json` in `landing/` with your plan catalog
   (price IDs, amounts, labels). Use the template below.
3. Set the `STRIPE_SECRET_KEY` environment variable.
4. Restart the server. Test:
   `curl http://127.0.0.1:8080/health` → `"stripe_configured": true`.

### `stripe_config.json` template

```json
{
  "currency": "usd",
  "success_url": "http://127.0.0.1:8080/?checkout=success&session_id={CHECKOUT_SESSION_ID}",
  "cancel_url": "http://127.0.0.1:8080/?checkout=cancel",
  "plans": {
    "maker": {
      "display_name": "Ghostprint Maker",
      "amount_cents": 900,
      "interval": "month",
      "price_id": "price_REPLACE_WITH_YOURS",
      "checkout_label": "Subscribe — $9 / mo",
      "features": [
        "Drag-drop web tool",
        "Hold master STLs in your account",
        "Verify suspect parts in the browser",
        "Up to 500 tags / month"
      ]
    }
  }
}
```

## What this does NOT do (v0.1)

- No auth. Anyone can tag.
- Stripe is wired (button + /api/checkout + Stripe Checkout API call)
  but the secret key is not configured by default. Fill in your own.
- No master STL storage. v0.2 has your account holding master STLs
  so the verifier can diff against them. v0.1 ships the CLI for full
  provenance; the web is "tag + emit + manifest" only.
- No batch upload. One STL per request.

## Why this stack

- **stdlib only on the server** — no Flask, no FastAPI. The CLI is the
  product; the wrapper is glue. When v0.2 needs auth + Stripe + per-user
  master STLs, we move to FastAPI in a single file.
- **single HTML file** — no bundler, no CDN, no Tailwind. Opens from
  `file://` if you really need to. 14 KB on the wire (gzipped).
- **no JS framework** — fetch + DOM. Total inline JS is ~4 KB.