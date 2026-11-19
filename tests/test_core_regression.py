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
