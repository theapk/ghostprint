"""
test_core_regression.py — regression tests for audit-wave core fixes.

Covers:
  * Big-part L2 round-trip: STL stores float32, so on large meshes the
    quantization error exceeds the old 1e-5 verify tolerance even though
    the encoder only emits deltas on a 1e-4 grid.
  * (appended by later tasks) CLI-level regression coverage.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import trimesh

from stl import mesh as stl_mesh

REPO_ROOT = Path(__file__).resolve().parent.parent
VERIFY_PRINT = REPO_ROOT / "verify-print.py"
sys.path.insert(0, str(REPO_ROOT / "src"))
from ghostprint_core import (  # noqa: E402
    l2_perturb_vertices,
    l2_verify_match,
)


def _box_stl(size: float, subdiv: int) -> stl_mesh.Mesh:
    """Build a subdivided box mesh (float32 vectors, like an STL)."""
    m = trimesh.creation.box(extents=[size, size, size])
    m = m.subdivide_to_size(max_edge=size / subdiv)
    sm = stl_mesh.Mesh(np.zeros(len(m.faces), dtype=stl_mesh.Mesh.dtype))
    for i, face in enumerate(m.faces):
        for j in range(3):
            sm.vectors[i][j] = m.vertices[face[j]]
    return sm


def _big_part_round_trip(tmp_path: Path, size: float, subdiv: int) -> None:
    master = _box_stl(size, subdiv)
    master_path = tmp_path / f"master_{int(size)}.stl"
    master.save(str(master_path))
    tagged = l2_perturb_vertices(master, "ORDER-BIG", 0.012)
    p = tmp_path / f"tagged_{int(size)}.stl"
    tagged.save(str(p))
    suspect = stl_mesh.Mesh.from_file(str(p))
    assert l2_verify_match(suspect, master, "ORDER-BIG", 0.012), (
        f"L2 verify failed on {int(size)}mm part (amplitude=0.012)"
    )


def test_big_part_round_trip_600mm(tmp_path):
    _big_part_round_trip(tmp_path, 600.0, 20)


def test_big_part_round_trip_1000mm(tmp_path):
    _big_part_round_trip(tmp_path, 1000.0, 20)


# --- CLI-level tests (verify-print.py) --------------------------------------

def _run_verify(*argv, cwd):
    """Run verify-print.py with the venv's python; return CompletedProcess."""
    return subprocess.run(
        [sys.executable, str(VERIFY_PRINT), *argv],
        capture_output=True, text=True, cwd=str(cwd),
    )


def test_verify_manifest_amplitude_exit0(tmp_path):
    """Tag at amplitude 0.006, then verify with --manifest and no
    --order-id / --amplitude: manifest supplies both, exits 0."""
    master = tmp_path / "master.stl"
    _box_stl(30.0, 6).save(str(master))
    tagged = tmp_path / "tagged.stl"
    r = subprocess.run(
        [sys.executable,
         str(REPO_ROOT / "tag-print.py"),
         str(master), "--out", str(tagged), "--order-id", "ORDER-MAN-1",
         "--printer", "bambu-werk", "--job-seq", "5",
         "--amplitude", "0.006"],
        capture_output=True, text=True, cwd=str(tmp_path),
    )
    assert r.returncode == 0, f"tag-print failed: {r.stderr}"
    manifest = tmp_path / "tagged.stl.manifest.json"
    assert manifest.exists()
    assert '"l2_amplitude_mm": 0.006' in manifest.read_text()

    r2 = _run_verify("decode-stl", str(tagged), "--master", str(master),
                     "--manifest", str(manifest), cwd=tmp_path)
    assert r2.returncode == 0, (
        f"manifest-driven verify failed: stdout={r2.stdout} stderr={r2.stderr}"
    )
    assert "✅ L2 match: order_id=ORDER-MAN-1" in r2.stdout
    assert "printer_id=" in r2.stdout


def test_verify_shape_mismatch_clean_error(tmp_path):
    """Suspect/master with different topology -> exit 1, one-line FAIL,
    no traceback."""
    master_small = tmp_path / "small.stl"
    _box_stl(20.0, 4).save(str(master_small))
    master_other = tmp_path / "other.stl"   # different topology (more faces)
    _box_stl(20.0, 6).save(str(master_other))
    r = _run_verify("decode-stl", str(master_other), "--master",
                    str(master_small), cwd=tmp_path)
    assert r.returncode == 1, f"stdout={r.stdout} stderr={r.stderr}"
    assert "FAIL:" in r.stderr
    assert "Traceback" not in r.stderr
    assert "Traceback" not in r.stdout


def test_verify_suspect_alias_rejected(tmp_path):
    """The unusable --suspect alias was removed: argparse must reject it."""
    master = tmp_path / "m.stl"
    _box_stl(20.0, 4).save(str(master))
    r = _run_verify("decode-stl", str(master), "--master", str(master),
                    "--suspect", str(master), cwd=tmp_path)
    assert r.returncode == 2, f"stdout={r.stdout} stderr={r.stderr}"
    assert "unrecognized arguments" in r.stderr
