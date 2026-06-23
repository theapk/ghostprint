"""
test_round_trip.py — pytest harness for ghostprint round-trip.

Generates 5 random test STLs (varying geometry: cube, sphere, cylinder,
two random convex hulls), tags each with a unique order_id, decodes
both layers, and asserts:

  L1: gcode -> printer_id, job_seq, ts_unix all match exactly.
  L2: tagged STL - master STL -> per-vertex deltas match the predicted
      per-vertex perturbation for that order_id (atol=1e-5 mm).

Also exercises the candidate-mode L2 recovery (with one planted order
plus 99 random decoys) to confirm the cosine-similarity ranking
selects the right order.
"""

from __future__ import annotations

import os
import random
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pytest
import trimesh

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from ghostprint_core import (  # noqa: E402
    L2_AMPLITUDE_MAX_MM,
    L2_AMPLITUDE_MIN_MM,
    l1_extract_from_gcode,
    l1_rewrite_gcode,
    l2_order_hash,
    l2_perturb_vertices,
    l2_recover_deltas,
    l2_recover_order,
    l2_verify_match,
)
from stl import mesh as stl_mesh


def _make_stl(path: str, kind: str, seed: int) -> int:
    """Generate a small test STL. Returns the face count."""
    rng = np.random.default_rng(seed)
    if kind == "cube":
        m = trimesh.creation.box(extents=[20 + rng.uniform(-3, 3)] * 3)
    elif kind == "sphere":
        m = trimesh.creation.icosphere(subdivisions=2)
        m.apply_translation(rng.uniform(-2, 2, 3))
    elif kind == "cylinder":
        m = trimesh.creation.cylinder(radius=8, height=20, sections=24)
    elif kind == "hull":
        # Use trimesh's convex_hull if scipy is available; otherwise fall
        # back to a translated box (still gives a varied shape).
        pts = rng.uniform(-10, 10, (12, 3))
        try:
            m = trimesh.convex.convex_hull(pts)
        except Exception:
            m = trimesh.creation.box(extents=[15, 12, 18])
            m.apply_translation(rng.uniform(-2, 2, 3))
    elif kind == "torus":
        m = trimesh.creation.torus(major_radius=10, minor_radius=3)
    else:
        raise ValueError(kind)
    # Subdivide lightly so we have more unique vertices than just corners.
    m = m.subdivide_to_size(max_edge=3.5)
    sm = stl_mesh.Mesh(np.zeros(len(m.faces), dtype=stl_mesh.Mesh.dtype))
    for i, face in enumerate(m.faces):
        for j in range(3):
            sm.vectors[i][j] = m.vertices[face[j]]
    sm.save(path)
    return len(m.faces)


def _round_trip_one(tmp: Path, kind: str, seed: int) -> dict:
    order_id = f"ORDER-{kind.upper()}-{seed:06d}"
    printer_name = f"printer-{kind}"
    job_seq = seed & 0xFFFFFF
    ts = int(time.time()) - seed  # deterministic per test
    master_stl = tmp / f"master_{kind}_{seed}.stl"
    tagged_stl = tmp / f"tagged_{kind}_{seed}.stl"
    n = _make_stl(str(master_stl), kind, seed)

    # L2 encode.
    master = stl_mesh.Mesh.from_file(str(master_stl))
    amp = 0.012
    tagged = l2_perturb_vertices(master, order_id, amplitude=amp)
    tagged.save(str(tagged_stl))

    # L2 verify (exact match).
    assert l2_verify_match(tagged, master, order_id, amplitude=amp), \
        f"L2 verify failed for {kind}/{seed}"

    # L2 deltas: max amplitude bound.
    deltas = l2_recover_deltas(tagged, master)
    mags = np.linalg.norm(deltas, axis=1)
    assert mags.max() <= amp * 1.001, f"delta exceeds amplitude: max={mags.max()}"
    assert mags.min() >= L2_AMPLITUDE_MIN_MM * 0.5 - 1e-5, \
        f"delta below safe range: min={mags.min()}"
    n_perturbed = (mags > 1e-6).sum()
    assert n_perturbed > 0, "no vertices were perturbed"

    # L1 encode (use the standalone pure-Python path).
    import hashlib
    printer_id = int.from_bytes(
        hashlib.blake2b(b"printer:" + printer_name.encode(), digest_size=4).digest(),
        "big") & 0xFFFFFF
    gcode = l1_rewrite_gcode("", printer_id, job_seq, ts)
    info = l1_extract_from_gcode(gcode)
    assert info["printer_id"] == printer_id, f"L1 printer_id mismatch"
    assert info["job_seq"] == job_seq, f"L1 job_seq mismatch"
    assert info["ts_unix"] == ts, f"L1 ts_unix mismatch"

    # L1 fallback: strip the comment, decode from Z moves.
    import re
    stripped = re.sub(r"; GP1:.*\n", "", gcode)
    info2 = l1_extract_from_gcode(stripped)
    assert info2["source"] == "babysteps", f"expected babysteps fallback, got {info2['source']}"
    assert (info2["printer_id"], info2["job_seq"], info2["ts_unix"]) == \
           (printer_id, job_seq, ts), "babysteps fallback round-trip failed"

    # L2 candidate mode: plant the true order_id among 99 decoys.
    decoys = [f"DECOY-{i:04d}" for i in range(99)]
    candidates = decoys + [order_id]
    random.Random(seed).shuffle(candidates)
    winner = l2_recover_order(tagged, master, amplitude=amp, candidates=candidates)
    assert winner == order_id, f"candidate recovery picked {winner!r} instead of {order_id!r}"

    return {
        "kind": kind, "seed": seed, "n_faces": n,
        "order_id": order_id, "printer_id": printer_id,
        "job_seq": job_seq, "ts_unix": ts,
        "max_delta_mm": float(mags.max()),
        "n_perturbed": int(n_perturbed),
    }


def test_round_trip_5_random_stls(tmp_path):
    """Generate 5 random STLs, tag, decode, assert exact round-trip."""
    cases = [
        ("cube", 1),
        ("sphere", 2),
        ("cylinder", 3),
        ("hull", 4),
        ("torus", 5),
    ]
    results = []
    for kind, seed in cases:
        r = _round_trip_one(tmp_path, kind, seed)
        results.append(r)
        print(f"  [PASS] {kind:8s} seed={seed} n_faces={r['n_faces']:4d} "
              f"max_delta={r['max_delta_mm']*1000:.2f}um n_perturbed={r['n_perturbed']}")
    # Sanity: 5 different order_ids recovered.
    assert len({r["order_id"] for r in results}) == 5
