"""
test_tag_cli.py — CLI-level regression tests for tag-print.py.

Drives tag-print.py via subprocess (the same way an operator would),
against a small STL built in tmp_path.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np

import trimesh
from stl import mesh as stl_mesh

REPO_ROOT = Path(__file__).resolve().parent.parent
TAG_PRINT = REPO_ROOT / "tag-print.py"


def _run_tag(*argv, cwd):
    """Run tag-print.py with the venv's python; return CompletedProcess."""
    return subprocess.run(
        [sys.executable, str(TAG_PRINT), *argv],
        capture_output=True, text=True, cwd=str(cwd),
    )


def _cube_stl(path: Path, size_mm: float = 20.0, max_edge: float = 3.5) -> int:
    """Small subdivided cube STL; returns the face count."""
    m = trimesh.creation.box(extents=[size_mm, size_mm, size_mm])
    m = m.subdivide_to_size(max_edge=max_edge)
    sm = stl_mesh.Mesh(np.zeros(len(m.faces), dtype=stl_mesh.Mesh.dtype))
    for i, face in enumerate(m.faces):
        for j in range(3):
            sm.vectors[i][j] = m.vertices[face[j]]
    sm.save(str(path))
    return len(m.faces)


def test_sliced_gcode_input_preserved(tmp_path):
    """tag-print must never overwrite its --sliced-gcode INPUT; the
    spliced G-code goes next to --out instead."""
    master = tmp_path / "master.stl"
    _cube_stl(master)
    sliced = tmp_path / "X.gcode"
    sliced.write_text("; original sliced gcode\nG28\nG1 X1 Y1 Z0.2\n")
    before = sliced.read_bytes()
    out_stl = tmp_path / "out.stl"
    r = _run_tag(
        str(master), "--out", str(out_stl), "--order-id", "ORDER-X",
        "--printer", "printer-a", "--job-seq", "7",
        "--sliced-gcode", str(sliced), cwd=tmp_path,
    )
    assert r.returncode == 0, f"tag-print failed: {r.stderr}"
    # 1. Input sliced G-code bytes UNCHANGED.
    assert sliced.read_bytes() == before, "--sliced-gcode input was clobbered"
    # 2. Emitted G-code lives beside --out and carries the watermark block.
    gcode_out = tmp_path / "out.gcode"
    assert gcode_out.exists(), f"missing {gcode_out}\nstdout:\n{r.stdout}"
    text = gcode_out.read_text()
    assert "; --- ghostprint begin ---" in text, "no watermark comment block"
    assert "; GP1:" in text, "no GP1 line in watermark block"
    # 3. The input content was spliced into (not replaced).
    assert "; original sliced gcode" in text
