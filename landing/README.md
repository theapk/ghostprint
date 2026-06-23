# theapk · ghostprint — landing page

Drag-drop web tool for the ghostprint CLI. Single-file HTML + stdlib-only
Python wrapper. No JS framework, no CDN, no Tailwind, no paid SaaS.

## What's in here

| File | Purpose |
|---|---|
| `index.html` | Single-file landing page. Drag-drop, fill order_id/printer/job_seq, click Tag STL, download. Vanilla JS, fetch() to `/api/tag`. Stripe-tier buttons call `/api/checkout?plan=...`. |
| `serve.py` | `ThreadingHTTPServer` + `BaseHTTPRequestHandler`. Serves `index.html`, exposes `POST /api/tag` that shells out to `tag-print.py` and returns tagged STL + gcode + manifest. Also exposes `GET /api/checkout?plan=<maker\|studio>` which calls Stripe Checkout API (stdlib urllib, no SDK) and returns the checkout URL. |
| `stripe_config.json` | Catalog of plans + price_ids + vault entry name. Vee fills in the `price_***` ids after creating the products in the Stripe dashboard. |

## Local dev

```bash
cd /Users/ian/theapk-ghostprint
source .venv/bin/activate
python3 landing/serve.py --host 127.0.0.1 --port 8080
# open http://127.0.0.1:8080
```

End-to-end test:
```bash
python3 -m pytest tests/test_landing_serve.py -v
# 3 passed in 0.5 s
```

## Deploy to LXC 105 (scrap-bin, no new infra)

LXC 105 (10.1.9.7) is the web LXC the worker uses. Drop the whole
`theapk-ghostprint/` repo at `/opt/theapk-ghostprint/` on the box
(nginx already serves other theapk.com subdomains from there):

```bash
# from your Mac, once Vee confirms the deploy path (vee-7):
rsync -avz --exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache' \
  /Users/ian/theapk-ghostprint/ root@lxc105:/opt/theapk-ghostprint/
ssh root@lxc105 'cd /opt/theapk-ghostprint && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt'
```

Then the API needs a process supervisor (s6, systemd, or a tmux session).
For v0.1, a tmux session is fine; v0.2 we wire it under s6.

### nginx vhost (one file)

`/etc/nginx/sites-available/ghostprint`:
```
server {
    listen 80;
    server_name ghostprint.theapk.com;
    client_max_body_size 60m;  # STL + gcode

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 120s;
    }
}
```

Then `certbot --nginx -d ghostprint.theapk.com` for HTTPS (already
provisioned for other theapk.com subdomains).

### Stripe (test mode — Vee fills in on return)

The page has a Subscribe button on the Maker + Studio tiers. The button
calls `GET /api/checkout?plan=<maker|studio>`. The server route calls
the Stripe Checkout API with stdlib `urllib.request` (no SDK). Returns
the checkout URL → JS does `window.location = url`.

**Until Vee fills in the Stripe bits, clicking Subscribe shows a clear
"Stripe not wired yet" message inline on the page (no crash, no
console error, the test suite verifies this).**

Vee's one-time setup, in order:

1. `https://dashboard.stripe.com/test/products` — create three products:
   - **Ghostprint Maker** — recurring, **$9 / month**, copy the
     `price_***` id.
   - **Ghostprint Studio** — recurring, **$49 / month**, copy the
     `price_***` id.
   - **Ghostprint CLI** — free, skip.
2. Open `landing/stripe_config.json` and paste each `price_***` id
   into the matching `plans.<name>.price_id` field (replacing the
   `REPLACE_WITH_price_xxx_FROM_STRIPE_DASHBOARD` placeholder).
3. `https://dashboard.stripe.com/test/apikeys` — copy the **secret
   key** (starts with `sk_test_`).
4. Save the secret to Vaultwarden under entry
   `ghostprint-stripe-test` (notes field, custom field, or whatever
   the vault convention is).
5. On LXC 105, write the env var so the systemd/tmux process picks
   it up:
   ```bash
   echo 'export STRIPE_SECRET_KEY=***' >> /opt/theapk-ghostprint/landing/.env
   ```
6. Restart the process. Test:
   `curl http://127.0.0.1:8080/health` → `"stripe_configured": true`.
   `curl 'http://127.0.0.1:8080/api/checkout?plan=maker'` → JSON with
   `checkout_url` starting with `https://checkout.stripe.com/`.

Worker cannot fill any of these in (vaultwarden CLI broken in this
environment; would need Vee to paste a key on return). Until then,
the v0.1 page renders cleanly and the Subscribe button shows the
helpful "Stripe not wired yet" message.

### Subdomain choice

Vee has not decided yet — see `BLOCKED` item vee-5 in
`~/.scratch/launch/theapk-launch/BOARD.md`. The HTML nav assumes
`ghostprint.theapk.com`; can be re-pointed with one find/replace in
`index.html` if Vee picks `theapk.com/ghostprint` instead.

## What this does NOT do (v0.1)

- No auth. Anyone can tag.
- **Stripe is wired (button + /api/checkout + Stripe Checkout API call)
  but the secret key is not configured.** Vee fills in three things on
  return: (1) price_id in `stripe_config.json`, (2) STRIPE_SECRET_KEY
  env var on LXC 105, (3) Stripe test product creation. Until then the
  Subscribe button shows a clear inline message instead of crashing.
  Paywall lives in the linked Stripe checkout (separate page).
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
