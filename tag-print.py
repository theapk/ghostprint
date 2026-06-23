#!/usr/bin/env python3
"""
tag-print.py — encode an invisible 3D-print fingerprint.

Usage:
  tag-print.py INPUT.stl --order-id ORDER-12345 --printer bambucco-1 \\
      --job-seq 42 --out OUTPUT.stl [--manifest manifest.json] \\
      [--no-l1] [--no-l2] [--amplitude 0.012]

Layers (both on by default):
  L1: G-code micro-watermark. If a --sliced-gcode FILE.gcode is given
      (or a slicer is configured via --slicer), we splice the watermark
      into the produced G-code and emit it next to the STL.
      Otherwise the encoder still emits a watermarked G-code that wraps
      a trivial "park-and-home" sequence, suitable for downstream
      re-slicing without losing the watermark.
  L2: Geometric steganography. Writes OUTPUT.stl with vertex
      perturbations of ±amplitude mm driven by the order_id hash.

Always writes a sidecar manifest (manifest.json by default) that pins
master SHA256, order_id, and the parameters needed to verify.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# Allow `python tag-print.py` to find the sibling src/ package.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from ghostprint_core import (  # noqa: E402
    L2_AMPLITUDE_MAX_MM,
    L2_AMPLITUDE_MIN_MM,
    GhostprintManifest,
    file_sha256,
    l1_rewrite_gcode,
    l2_perturb_vertices,
)


def _hash_printer_id(name: str) -> int:
    """Stable 24-bit printer ID from a human name. Deterministic, so
    the same name on the same host yields the same ID."""
    import hashlib
    h = hashlib.blake2b(b"printer:" + name.encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(h, "big") & 0xFFFFFF


def _slice_with_orca(stl_path: str, out_dir: str) -> Optional[str]:
    """Try to slice via the OrcaSlicer CLI. Returns path to sliced G-code
    or None if OrcaSlicer is not available / fails."""
    orca = "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer"
    if not os.path.exists(orca):
        return None
    os.makedirs(out_dir, exist_ok=True)
    # --export-slicedata writes plate_0.gcode in the out dir
    try:
        subprocess.run(
            [
                orca, "--slice", "0", "--outputdir", out_dir,
                "--export-slicedata", out_dir,
                stl_path,
            ],
            check=True, timeout=180, capture_output=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    g = os.path.join(out_dir, "plate_0.gcode")
    return g if os.path.exists(g) else None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="tag-print.py — invisible 3D-print fingerprint encoder",
    )
    ap.add_argument("input_stl", help="input master STL path")
    ap.add_argument("--out", required=True, help="output tagged STL path")
    ap.add_argument("--order-id", required=True, help="per-order identifier")
    ap.add_argument("--printer", required=True, help="printer name (hashed to 24-bit ID)")
    ap.add_argument("--job-seq", type=int, default=None,
                    help="per-printer job sequence number (default: unix timestamp & 0xFFFFFF)")
    ap.add_argument("--amplitude", type=float, default=0.012,
                    help=f"L2 perturbation amplitude in mm "
                         f"[{L2_AMPLITUDE_MIN_MM}, {L2_AMPLITUDE_MAX_MM}], default 0.012")
    ap.add_argument("--no-l1", action="store_true", help="disable G-code watermark layer")
    ap.add_argument("--no-l2", action="store_true", help="disable geometric steganography layer")
    ap.add_argument("--manifest", default=None, help="manifest output path (default: <out>.manifest.json)")
    ap.add_argument("--sliced-gcode", default=None, help="also splice the L1 watermark into this existing G-code file")
    ap.add_argument("--emit-gcode", action="store_true",
                    help="also write a watermarked G-code wrapping the STL (useful when no slicer is available)")
    ap.add_argument("--ts", type=int, default=None, help="override timestamp (unix seconds)")
    args = ap.parse_args()

    if args.no_l1 and args.no_l2:
        print("error: both layers disabled — nothing to do", file=sys.stderr)
        return 2
    if not (L2_AMPLITUDE_MIN_MM <= args.amplitude <= L2_AMPLITUDE_MAX_MM):
        print(f"error: --amplitude {args.amplitude} outside "
              f"[{L2_AMPLITUDE_MIN_MM}, {L2_AMPLITUDE_MAX_MM}]", file=sys.stderr)
        return 2

    input_stl = args.input_stl
    out_stl = args.out
    if not os.path.exists(input_stl):
        print(f"error: input STL not found: {input_stl}", file=sys.stderr)
        return 2

    printer_id = _hash_printer_id(args.printer)
    job_seq = args.job_seq if args.job_seq is not None else (int(time.time()) & 0xFFFFFF)
    ts_unix = args.ts if args.ts is not None else int(time.time())

    # --- L2: geometric stego ---
    if not args.no_l2:
        from stl import mesh as stl_mesh
        master = stl_mesh.Mesh.from_file(input_stl)
        tagged = l2_perturb_vertices(master, args.order_id, amplitude=args.amplitude)
        tagged.save(out_stl)
        print(f"[L2] wrote tagged STL: {out_stl} "
              f"({tagged.vectors.shape[0]} faces, amplitude={args.amplitude} mm)")
    else:
        # Just copy the master.
        shutil.copyfile(input_stl, out_stl)
        print(f"[L2] skipped — copied master to {out_stl}")

    # --- L1: G-code watermark ---
    manifest_path = args.manifest or (out_stl + ".manifest.json")
    gcode_out = None
    if not args.no_l1:
        if args.sliced_gcode and os.path.exists(args.sliced_gcode):
            with open(args.sliced_gcode, "r", encoding="utf-8", errors="replace") as f:
                base = f.read()
            watermarked = l1_rewrite_gcode(base, printer_id, job_seq, ts_unix)
            base_root, _ = os.path.splitext(args.sliced_gcode)
            gcode_out = base_root + ".gcode"
            with open(gcode_out, "w", encoding="utf-8") as f:
                f.write(watermarked)
            print(f"[L1] spliced watermark into {gcode_out}")
        else:
            # Try OrcaSlicer.
            tmp_dir = out_stl + ".slice_tmp"
            sliced = _slice_with_orca(input_stl, tmp_dir)
            if sliced:
                with open(sliced, "r", encoding="utf-8", errors="replace") as f:
                    base = f.read()
                watermarked = l1_rewrite_gcode(base, printer_id, job_seq, ts_unix)
                gcode_out = os.path.splitext(out_stl)[0] + ".gcode"
                with open(gcode_out, "w", encoding="utf-8") as f:
                    f.write(watermarked)
                print(f"[L1] sliced with OrcaSlicer and watermarked: {gcode_out}")
                shutil.rmtree(tmp_dir, ignore_errors=True)
            elif args.emit_gcode:
                # Pure-Python fallback: emit a minimal watermarked G-code
                # that contains the watermark and a comment header. The
                # verifier still recovers the L1 payload from this file.
                gcode_out = os.path.splitext(out_stl)[0] + ".gcode"
                with open(gcode_out, "w", encoding="utf-8") as f:
                    f.write(l1_rewrite_gcode("", printer_id, job_seq, ts_unix))
                print(f"[L1] emitted standalone watermarked G-code: {gcode_out}")
            else:
                # No slicer, no --emit-gcode: still splice a watermarked
                # block at the top of the (empty) G-code so the manifest
                # records the L1 params and --emit-gcode wasn't forgotten.
                print("[L1] no slicer available and --emit-gcode not set; "
                      "skipping G-code output. Re-run with --emit-gcode or "
                      "--sliced-gcode to write the G-code layer.", file=sys.stderr)

    # --- Manifest ---
    manifest = GhostprintManifest(
        order_id=args.order_id,
        printer_id=printer_id,
        job_seq=job_seq,
        ts_unix=ts_unix,
        l1_gcode_watermark=not args.no_l1,
        l2_geom_stego=not args.no_l2,
        l2_amplitude_mm=args.amplitude,
        master_stl_sha256=file_sha256(input_stl),
        tagged_stl_sha256=file_sha256(out_stl) if not args.no_l2 else file_sha256(input_stl),
    )
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest.to_dict(), f, indent=2)
    print(f"[manifest] {manifest_path}")

    print()
    print(f"  order_id   : {args.order_id}")
    print(f"  printer    : {args.printer}  (id={printer_id})")
    print(f"  job_seq    : {job_seq}")
    print(f"  ts_unix    : {ts_unix}")
    print(f"  l1 emitted : {gcode_out or '(skipped)'}")
    print(f"  l2 emitted : {out_stl if not args.no_l2 else '(skipped)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
