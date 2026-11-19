"""
test_cli.py — subprocess-level tests for the `ghostprint` console CLI.

Run pattern: drive ghostprint.cli:main through a fresh interpreter via
`python -c` (works everywhere the repo (or an installed theapk-ghostprint)
is importable — no reliance on the console-script entry point existing in
the running interpreter's venv).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import trimesh
from stl import mesh as stl_mesh

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))


def _cli_run(*argv, cwd=REPO_ROOT):
    """Run `ghostprint <argv...>` via python -c (no entry-point dependency)."""
    code = (
        "import sys; sys.path.insert(0, r'{}'); "
        "from ghostprint.cli import main; "
        "sys.argv = ['ghostprint'] + sys.argv[1:]; sys.exit(main())"
    ).format(SRC_DIR)
    return _subprocess_run(code, *argv, cwd=cwd)


def _subprocess_run(code, *argv, cwd):
    import subprocess
    return subprocess.run(
        [sys.executable, "-c", code, *argv],
        capture_output=True, text=True, cwd=str(cwd),
    )


def _cube_stl(path: Path, size_mm: float = 20.0, max_edge: float = 3.5) -> None:
    m = trimesh.creation.box(extents=[size_mm, size_mm, size_mm])
    m = m.subdivide_to_size(max_edge=max_edge)
    sm = stl_mesh.Mesh(np.zeros(len(m.faces), dtype=stl_mesh.Mesh.dtype))
    for i, face in enumerate(m.faces):
        for j in range(3):
            sm.vectors[i][j] = m.vertices[face[j]]
    sm.save(str(path))


def test_cli_self_test_exit_zero():
    r = _cli_run("self-test")
    assert r.returncode == 0, f"stderr={r.stderr}"
    assert '"overall_pass": true' in r.stdout


def test_cli_tag_verify_round_trip(tmp_path):
    master = tmp_path / "master.stl"
    _cube_stl(master)
    tagged = tmp_path / "tagged.stl"
    tag = _cli_run("tag", str(master), "--out", str(tagged),
                   "--order-id", "ORDER-CLI-1", "--printer", "ci-runner",
                   "--job-seq", "7", cwd=tmp_path)
    assert tag.returncode == 0, f"tag stderr={tag.stderr}"
    assert tagged.exists()
    verify = _cli_run("verify", str(master), str(tagged),
                      "--order-id", "ORDER-CLI-1", cwd=tmp_path)
    assert verify.returncode == 0, f"verify stderr={verify.stderr}"
    assert "✅ L2 match" in verify.stdout


def test_cli_verify_master_tagged_sidecar_only(tmp_path):
    """README promise order with NO --order-id: the sidecar manifest is
    auto-discovered next to the TAGGED path (decode-stl's suspect
    positional, i.e. rest[1] of the two-positional sugar). The order_id
    must come from the manifest and the L2 match must still print. Moving
    the sidecar next to the MASTER must NOT produce a match (discovery
    keys on the tagged path, not the master)."""
    master = tmp_path / "master.stl"
    _cube_stl(master)
    tagged = tmp_path / "tagged.stl"
    tag = _cli_run("tag", str(master), "--out", str(tagged),
                   "--order-id", "ORDER-SC-1", "--printer", "ci-runner",
                   "--job-seq", "3", cwd=tmp_path)
    assert tag.returncode == 0, f"tag stderr={tag.stderr}"
    sidecar = tagged.with_suffix(".stl.manifest.json")  # tag-print default
    assert sidecar.exists()

    verify = _cli_run("verify", str(master), str(tagged), cwd=tmp_path)
    assert verify.returncode == 0, f"verify stderr={verify.stderr}"
    assert "✅ L2 match: order_id=ORDER-SC-1" in verify.stdout

    # Negative control: re-key the sidecar to the master path — discovery
    # must not find it (it keys on the TAGGED path at this HEAD).
    sidecar.rename(master.parent / "master.stl.manifest.json")
    verify2 = _cli_run("verify", str(master), str(tagged), cwd=tmp_path)
    assert verify2.returncode == 0, f"verify2 stderr={verify2.stderr}"
    assert "L2 match" not in verify2.stdout


def test_cli_verify_flags_first_passthrough_rc2(tmp_path):
    """Flags-first REMAINDER quirk: `verify --manifest m tagged master`
    passes through to decode-stl's argparse (one positional, --master
    required) → clean argparse error, rc 2, no Traceback."""
    r = _cli_run("verify", "--manifest", "m", "tagged", "master", cwd=tmp_path)
    assert r.returncode == 2, f"stdout={r.stdout} stderr={r.stderr}"
    assert "Traceback" not in r.stderr
    assert r.stderr.strip()  # argparse usage/error text present


def test_cli_verify_single_positional_flags_passthrough(tmp_path):
    """Single-positional + flags: `verify tagged --order-id X` skips the
    two-positional branch (rest[1] is a flag) and routes decode-stl
    TAGGED --order-id X — suspect=tagged, sidecar discovery keyed on
    tagged, L2 match prints (no master swap)."""
    master = tmp_path / "master.stl"
    _cube_stl(master)
    tagged = tmp_path / "tagged.stl"
    tag = _cli_run("tag", str(master), "--out", str(tagged),
                   "--order-id", "ORDER-SP-1", "--printer", "ci-runner",
                   "--job-seq", "5", cwd=tmp_path)
    assert tag.returncode == 0, f"tag stderr={tag.stderr}"
    verify = _cli_run("verify", str(tagged), "--order-id", "ORDER-SP-1",
                      cwd=tmp_path)
    assert verify.returncode == 0, f"verify stderr={verify.stderr}"
    assert "✅ L2 match: order_id=ORDER-SP-1" in verify.stdout


def test_cli_core_shim_reexports_same_objects():
    """ghostprint_core shim must re-export the SAME objects as
    ghostprint.core (no duplicated definitions)."""
    import importlib
    core_shim = importlib.import_module("ghostprint_core")
    from ghostprint import core
    assert core_shim.l2_perturb_vertices is core.l2_perturb_vertices
    assert core_shim.l1_rewrite_gcode is core.l1_rewrite_gcode
    assert core_shim.GhostprintManifest is core.GhostprintManifest
    assert core_shim.L2_AMPLITUDE_MIN_MM == core.L2_AMPLITUDE_MIN_MM


def test_cli_fail_clean_when_sibling_scripts_missing(monkeypatch):
    """Installed-from-wheel case: sibling tag-print.py absent → main()
    must print the one-line clone error and exit 2."""
    from ghostprint import cli
    monkeypatch.setattr(cli, "_TAG_PRINT", Path("/nonexistent/tag-print.py"))
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        rc = cli.main(["self-test"])
    assert rc == 2
    assert "repo clone" in buf.getvalue()
