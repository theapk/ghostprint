#!/usr/bin/env python3
"""
verify-print.py — decode an invisible 3D-print fingerprint.

Usage:
  verify-print.py decode-gcode FILE.gcode
      → prints printer_id, job_seq, timestamp, and provenance (which
        decoder path: 'comment' = primary, 'babysteps' = fallback).

  verify-print.py decode-stl SUSPECT.stl --master MASTER.stl
                       [--order-id ORDER-12345 | --candidates id1,id2,...]
                       [--manifest MANIFEST.json] [--amplitude A]
      → if --order-id given, asserts the suspect matches that order
        exactly (within encoder rounding, 1e-4 mm). Exits 0 on match,
        1 on mismatch.
        if --candidates given, returns the best match (cosine score
        threshold 0.99) or None.
        if neither, prints the per-vertex delta statistics so an
        operator can eyeball the perturbation envelope.
        A sidecar manifest (default auto-detect: <suspect>.manifest.json)
        supplies order_id and l2_amplitude_mm (plus printer_id for the
        success line) when --order-id is not given; --amplitude
        explicitly overrides the manifest amplitude when both are present.

  verify-print.py self-test
      → generates a fresh test cube STL, tags it with a known order_id,
        decodes both layers, asserts round-trip is exact. Exits 0 on
        PASS, non-zero on FAIL.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import List, Optional

# Allow `python verify-print.py` to find the sibling src/ package.
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from ghostprint_core import (  # noqa: E402
    L2_AMPLITUDE_MAX_MM,
    L2_AMPLITUDE_MIN_MM,
    l1_extract_from_gcode,
    l2_order_hash,
    l2_recover_deltas,
    l2_recover_order,
    l2_verify_match,
)


def _gen_test_cube_stl(path: str, size_mm: float = 20.0) -> int:
    """Generate a small ASCII test cube STL with 12 triangles. Returns
    the face count. Uses trimesh so no third-party mesh lib is needed."""
    import trimesh
    import numpy as np
    h = size_mm / 2.0
    box = trimesh.creation.box(extents=[size_mm, size_mm, size_mm])
    # Subdivide slightly so we have enough unique vertices to spread
    # the order hash across.
    subdivided = box.subdivide_to_size(max_edge=2.0)
    # Re-derive a clean STL mesh via stl.Mesh for the encoder path
    # (the encoder imports stl.mesh, not trimesh).
    from stl import mesh as stl_mesh
    m = stl_mesh.Mesh(np.zeros(len(subdivided.faces), dtype=stl_mesh.Mesh.dtype))
    for i, face in enumerate(subdivided.faces):
        for j in range(3):
            m.vectors[i][j] = subdivided.vertices[face[j]]
    m.save(path)
    return len(subdivided.faces)


def cmd_decode_gcode(args) -> int:
    if not os.path.exists(args.gcode):
        print(f"error: file not found: {args.gcode}", file=sys.stderr)
        return 2
    with open(args.gcode, "r", encoding="utf-8", errors="replace") as f:
        gcode = f.read()
    try:
        info = l1_extract_from_gcode(gcode)
    except ValueError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    print(json.dumps(info, indent=2))
    return 0


def cmd_decode_stl(args) -> int:
    if not os.path.exists(args.suspect) or not os.path.exists(args.master):
        print("error: suspect STL (positional) and --master must exist", file=sys.stderr)
        return 2

    # --- Manifest resolution ------------------------------------------------
    # The sidecar manifest pairs this suspect STL with the order_id and
    # amplitude used at tag time, so a verifier can skip re-deriving them.
    if args.manifest and not os.path.exists(args.manifest):
        print(f"error: manifest not found: {args.manifest}", file=sys.stderr)
        return 2
    manifest_path = args.manifest or (args.suspect + ".manifest.json")
    manifest: Optional[dict] = None
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"warning: could not read manifest {manifest_path}: {e}",
                  file=sys.stderr)
            manifest = None

    manifest_amplitude = None
    if manifest is not None and manifest.get("l2_amplitude_mm") is not None:
        manifest_amplitude = float(manifest["l2_amplitude_mm"])
    # --amplitude explicitly overrides the manifest amplitude when both
    # are present; otherwise use the manifest value, else the 0.012 default.
    amplitude = (args.amplitude if args.amplitude is not None
                 else (manifest_amplitude if manifest_amplitude is not None
                       else 0.012))

    order_id = args.order_id
    if order_id is None and manifest is not None:
        order_id = manifest.get("order_id") or None

    candidates: Optional[List[str]] = None
    if args.candidates:
        candidates = [c.strip() for c in args.candidates.split(",") if c.strip()]

    from stl import mesh as stl_mesh
    suspect = stl_mesh.Mesh.from_file(args.suspect)
    master = stl_mesh.Mesh.from_file(args.master)
    try:
        deltas = l2_recover_deltas(suspect, master)
        magnitudes = (deltas ** 2).sum(axis=1) ** 0.5
        nz = magnitudes[magnitudes > 1e-9]
        print(json.dumps({
            "n_vertices": int(deltas.shape[0]),
            "n_perturbed": int(nz.size),
            "max_delta_mm": float(magnitudes.max()) if magnitudes.size else 0.0,
            "mean_delta_mm": float(magnitudes.mean()) if magnitudes.size else 0.0,
            "amplitude_envelope_mm": [float(L2_AMPLITUDE_MIN_MM), float(L2_AMPLITUDE_MAX_MM)],
        }, indent=2))
        if order_id:
            ok = l2_verify_match(suspect, master, order_id, amplitude=amplitude)
            if ok:
                line = f"✅ L2 match: order_id={order_id}"
                if manifest is not None and manifest.get("printer_id") is not None:
                    line += f" printer_id={manifest['printer_id']}"
                print(line)
                return 0
            print(f"order_id MISMATCH: {order_id}  ✗", file=sys.stderr)
            return 1
        if candidates:
            winner = l2_recover_order(suspect, master, amplitude=amplitude,
                                       candidates=candidates)
            if winner is None:
                print("no candidate above score threshold 0.99", file=sys.stderr)
                return 1
            print(f"best candidate: {winner}")
            return 0
    except ValueError as e:
        # Shape mismatch (suspect topology differs from the master) and any
        # other diff error: clean one-line failure, no traceback.
        print(f"FAIL: {e}", file=sys.stderr)
        return 1
    return 0


def cmd_self_test(args) -> int:
    """End-to-end round-trip: generate cube → tag → decode both layers."""
    import numpy as np
    with tempfile.TemporaryDirectory(prefix="ghostprint_selftest_") as tmp:
        master_stl = os.path.join(tmp, "master.stl")
        tagged_stl = os.path.join(tmp, "tagged.stl")
        manifest = os.path.join(tmp, "manifest.json")
        gcode_out = os.path.join(tmp, "watermarked.gcode")
        n_faces = _gen_test_cube_stl(master_stl)
        order_id = "ORDER-SELFTEST-001"
        printer_name = "self-test-printer"
        # Encode L2
        from stl import mesh as stl_mesh
        m = stl_mesh.Mesh.from_file(master_stl)
        from ghostprint_core import l2_perturb_vertices
        tagged = l2_perturb_vertices(m, order_id, amplitude=0.012)
        tagged.save(tagged_stl)
        # Encode L1
        import hashlib, time as _time
        printer_id = int.from_bytes(
            hashlib.blake2b(b"printer:" + printer_name.encode(), digest_size=4).digest(),
            "big") & 0xFFFFFF
        job_seq = 12345
        ts_unix = int(_time.time())
        from ghostprint_core import l1_rewrite_gcode
        gcode_text = l1_rewrite_gcode("", printer_id, job_seq, ts_unix)
        with open(gcode_out, "w") as f:
            f.write(gcode_text)
        # Decode L1
        info = l1_extract_from_gcode(gcode_text)
        l1_ok = (
            info["printer_id"] == printer_id
            and info["job_seq"] == job_seq
            and info["ts_unix"] == ts_unix
        )
        # Decode L2
        l2_ok = l2_verify_match(tagged, m, order_id, amplitude=0.012)
        deltas = l2_recover_deltas(tagged, m)
        max_delta = float(np.linalg.norm(deltas, axis=1).max())
        # Report
        result = {
            "n_faces": n_faces,
            "l1": {
                "extracted": info,
                "expected": {"printer_id": printer_id, "job_seq": job_seq, "ts_unix": ts_unix},
                "pass": l1_ok,
            },
            "l2": {
                "order_id": order_id,
                "max_delta_mm": max_delta,
                "pass": l2_ok,
            },
            "overall_pass": l1_ok and l2_ok,
        }
        print(json.dumps(result, indent=2))
        return 0 if result["overall_pass"] else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="verify-print.py — invisible 3D-print fingerprint decoder")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("decode-gcode", help="recover L1 (printer+job+ts) from a G-code file")
    p1.add_argument("gcode", help="path to .gcode file")
    p1.set_defaults(func=cmd_decode_gcode)

    p2 = sub.add_parser("decode-stl", help="recover L2 (order_id) by diffing STL against master")
    p2.add_argument("suspect", help="path to the suspect STL to check")
    p2.add_argument("--master", required=True, help="path to master STL")
    p2.add_argument("--order-id", default=None, help="assert this order_id is encoded in the suspect")
    p2.add_argument("--candidates", default=None, help="comma-separated candidate order_ids to test")
    p2.add_argument("--manifest", default=None,
                    help="sidecar manifest JSON from tag-print "
                         "(default: auto-detect <suspect>.manifest.json)")
    p2.add_argument("--amplitude", type=float, default=None,
                    help="L2 amplitude in mm; when given it overrides the "
                         "manifest value (default: manifest, else 0.012)")
    p2.set_defaults(func=cmd_decode_stl)

    p3 = sub.add_parser("self-test", help="generate test STL, tag, decode, assert round-trip")
    p3.set_defaults(func=cmd_self_test)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
