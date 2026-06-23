# theapk · ghostprint

Invisible fingerprinting for 3D prints. Pin a printed part to a specific
order, printer, and timestamp — without changing the visual, dimensional,
or surface finish of the part.

Two independent layers, both round-trip verifiable:

- **L1** — G-code micro-watermark. Printer + job + timestamp encoded as
  sub-resolution Z-babysteps at print start. Survives any post-processor
  that doesn't strip `;` comments or rewrite the park block.
- **L2** — Geometric steganography. Per-vertex perturbation of ±0.005–0.020 mm
  driven by a BLAKE2b hash of the order_id. Below typical 0.04 mm nozzle
  X/Y resolution. Survives a no-slicer path; standard slicer re-mesh
  strips it (use L1 in that case, or hold the master STL).

A sidecar `manifest.json` pairs the tagged STL with the order_id, printer
SHA, and master STL SHA256, so a verifier with the master can always
recover provenance.

## Install

```bash
git clone https://dev.vivaed.com/theapk/ghostprint.git
cd ghostprint
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Requires Python 3.10+. No system deps; pure-Python, numpy + numpy-stl + trimesh.

## Quickstart

Tag a master STL with an order ID:

```bash
./tag-print.py path/to/master.stl \
  --order-id ORDER-2026-0001 \
  --printer bambucco-1 \
  --job-seq 42 \
  --out tagged.stl \
  --emit-gcode
```

This writes:
- `tagged.stl` — the watermarked STL (L2)
- `tagged.gcode` — a watermarked G-code wrapping the STL (L1, fallback path)
- `tagged.stl.manifest.json` — the sidecar

Verify a suspect part against the master:

```bash
./verify-print.py decode-stl suspect.stl --master master.stl --order-id ORDER-2026-0001
# → "order_id match: ORDER-2026-0001  ✓"  (exit 0)
# → "order_id MISMATCH: ... ✗"             (exit 1)
```

Decode a G-code watermark:

```bash
./verify-print.py decode-gcode tagged.gcode
# → { "printer_id": 7116758, "job_seq": 42, "ts_unix": ..., "source": "comment" }
```

End-to-end self-test (generates a test cube, tags, decodes both layers, asserts round-trip):

```bash
./verify-print.py self-test
# → "overall_pass": true
```

## CLI reference

### `tag-print.py INPUT.stl [options]`

| Flag | Default | Purpose |
|---|---|---|
| `--out PATH` | required | output tagged STL path |
| `--order-id ID` | required | per-order identifier (any string) |
| `--printer NAME` | required | printer name; hashed to a stable 24-bit ID |
| `--job-seq N` | unix ts | per-printer job sequence number |
| `--amplitude MM` | 0.012 | L2 perturbation envelope, 0.005–0.020 |
| `--no-l1` | off | skip the G-code watermark layer |
| `--no-l2` | off | skip the geometric steganography layer |
| `--sliced-gcode FILE` | none | splice the L1 watermark into an existing sliced G-code |
| `--emit-gcode` | off | always emit a watermarked G-code (useful without a slicer) |
| `--manifest PATH` | `<out>.manifest.json` | manifest output path |
| `--ts UNIX` | now | override timestamp (for reproducible tests) |

### `verify-print.py <subcommand>`

- `decode-gcode FILE` — recover L1 (printer + job + ts) from a G-code file.
- `decode-stl SUSPECT --master MASTER [--order-id ID | --candidates a,b,c]` —
  recover L2 by diffing the suspect STL against the master. With `--order-id`,
  asserts an exact match. With `--candidates`, picks the best match above
  cosine score 0.99.
- `self-test` — generate a test cube, tag, decode both layers, assert round-trip.

## How it works

### L1 — G-code micro-watermark

A 12-byte payload (printer_id 3B | job_seq 3B | unix_ts 4B | scheme 2B) is
spliced into the start of the G-code file, bracketed by `; --- ghostprint
begin/end ---` comments. The payload is duplicated in two forms:

1. A redundant `; GP1: GP1:<base32>` comment — recoverable by regex even if
   the babystep block is stripped.
2. A sequence of sub-resolution `G1 ... Z<z>` moves (babysteps) where each
   bit of the payload flips the Z offset by ±0.010 mm around a 0.20 mm
   base height. A 0.04 mm layer band absorbs the perturbation invisibly.

The 16-byte raw payload (12 data + 4 CRC32) is CRC-checked on decode.

### L2 — geometric steganography

The order_id is hashed with domain-separated BLAKE2b (`master_seed + \x00 + order_id`,
32 bytes). For each (face, vertex) pair in the master STL, one byte of the
hash drives a deterministic per-vertex displacement: a tangent-to-face
axis (the world axis with smallest dot to the face normal) scaled to
`±amplitude * (0.5..1.0)` with sign from the byte's high bit. Shared
vertices on triangle boundaries are de-duped by 1 µm coordinate
rounding, so the perturbation is coherent across the mesh.

Magnitude stays inside `[0.005, 0.020]` mm — below typical 0.04 mm X/Y
nozzle resolution. The final part is dimensionally and visually
indistinguishable from the master.

**Survival caveat:** standard slicers (PrusaSlicer, OrcaSlicer, Bambu Studio)
re-mesh on import, which destroys L2. Use L1 alone for the slicer path,
or hold the master STL to enable L2 verification at any time.

### Manifest

A sidecar JSON pins everything needed for offline verification:

```json
{
  "order_id": "ORDER-2026-0001",
  "printer_id": 7116758,
  "job_seq": 42,
  "ts_unix": 1781942549,
  "l1_gcode_watermark": true,
  "l2_geom_stego": true,
  "l2_amplitude_mm": 0.012,
  "master_stl_sha256": "...",
  "tagged_stl_sha256": "...",
  "scheme_version": 1
}
```

## Use cases

- **Anti-counterfeit.** A customer claims your print is a counterfeit. You
  diff their STL against your master, recover the order_id, and prove which
  order (and which printer, which job) made the part.
- **Chain-of-custody.** Track which licensed manufacturer / printer /
  batch produced a regulated part (medical, aerospace, defense).
- **Per-order traceability.** A studio sells one-of-a-kind prints. The
  customer receives a tagged STL + manifest, and the studio can prove
  the exact print run their piece came from.

## Why this wins

Existing 3D-print watermarking is either:
- **Visible** (QR codes, etched serial numbers) — easy to copy around.
- **Destructive** (color shifts, material additives) — changes the part.
- **Slicer-coupled** (post-processor scripts that require a specific
  slicer) — fragile across the ecosystem.

`theapk · ghostprint` is invisible, deterministic, slicer-independent at
the encoder, and round-trip verifiable. The CLI is MIT-licensed and
runs on any laptop; the web tool (in development) gives non-technical
makers a drag-drop UX.

## Roadmap
## Web tool (v0.1, beta)

A drag-drop landing page lives in `landing/`. Single HTML + stdlib-only
Python wrapper — no framework, no CDN. See [`landing/README.md`](landing/README.md)
for the deploy recipe (LXC 105, no new infra).

```bash
cd /path/to/ghostprint
python3 landing/serve.py --port 8080
# open http://127.0.0.1:8080
```

## Roadmap

- v0.1 (now) — CLI, MIT, full round-trip verified. Landing page beta.
- v0.2 — L2 stego on G-code toolpath (survives slicer re-mesh). Web
  upload + pay → tagged download. Per-user master STL storage.
- v0.3 — Slicer plugin (PrusaSlicer / OrcaSlicer post-processor).
- v0.4 — Bambu / Klipper native integration.

## License

MIT. See `LICENSE`.

## Brand

`theapk · ghostprint` is a product line of theapk llc. v0.1 ships
under MIT; the hosted service (v0.2+) is a paid tier of theapk.com.
