#!/usr/bin/env python3
"""test_rlimit.py — prove the tag-print.py child process runs under
resource limits (RLIMIT_AS 1.5 GiB / RLIMIT_CPU 60 s).

We do not run tag-print.py here; we run a fresh interpreter that imports
landing.serve and executes _limits() itself (the same function passed as
preexec_fn to the child). If _limits() is real, importable, and safe to
call in a child process, getrlimit(RLIMIT_CPU) must read back 60 after it.

Pure stdlib. Runs a subprocess so limits never leak into the test process.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_CODE = (
    "import resource, tempfile\n"
    "import sys\n"
    "sys.path.insert(0, %r)\n"
    "from landing.serve import _limits\n"
    "_limits()\n"
    "lo, hi = resource.getrlimit(resource.RLIMIT_CPU)\n"
    "assert hi == 60, hi\n"
    "lo_as, hi_as = resource.getrlimit(resource.RLIMIT_AS)\n"
    "assert hi_as == 1_500_000_000, hi_as\n"
) % (str(ROOT),)


def test_tag_child_limits_runnable():
    """serve._limits() executes cleanly and leaves RLIMIT_CPU=60 /
    RLIMIT_AS=1.5GiB — the limits the tag-print.py child will inherit."""
    r = subprocess.run(
        [sys.executable, "-c", _CODE],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert r.returncode == 0, f"limits child failed:\n{r.stderr}"


def test_run_tag_passes_preexec_fn():
    """subprocess.run in _run_tag must actually install the limits
    (preexec_fn=_limits is the point of the task)."""
    import ast
    src = (ROOT / "landing" / "serve.py").read_text()
    tree = ast.parse(src)
    run_calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and getattr(getattr(n, "func", None), "attr", None) == "run"
    ]
    assert run_calls, "no subprocess.run call found in serve.py"
    kwargs = {k.arg for node in run_calls for k in node.keywords}
    assert "preexec_fn" in kwargs, (
        "subprocess.run(...) must pass preexec_fn=_limits"
    )
