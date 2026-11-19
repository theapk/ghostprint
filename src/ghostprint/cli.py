"""ghostprint — console-script CLI for the two-layer 3D-print watermark.

Verbs (wrapping the repo's tag-print.py / verify-print.py):
    ghostprint tag <tag-print.py arguments>
    ghostprint verify <suspect.stl> <master.stl> [--order-id ...]
                      (also accepts decode-stl --master form)
    ghostprint decode-gcode <FILE.gcode>
    ghostprint self-test

v0.1.1 limitation: the CLI loads the sibling tag-print.py / verify-print.py
from a repo clone (documented install path: `pip install -e .` from the
git checkout). If the sibling scripts are missing (installed from a
wheel), main() fails cleanly with exit code 2.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import List, Optional

_SRC_DIR = Path(__file__).resolve().parents[1]  # <repo>/src (package lives in src/)
_REPO_ROOT = _SRC_DIR.parent                    # <repo>/ (sibling scripts live here)
_TAG_PRINT = _REPO_ROOT / "tag-print.py"
_VERIFY_PRINT = _REPO_ROOT / "verify-print.py"

_VERIFY_SUBVERBS = ("decode-gcode", "decode-stl", "self-test")


def _load_sibling(module_name: str, path: Path):
    """Load a sibling repo script by absolute path (same house pattern as
    landing/serve.py). Returns None when the script is missing, so the
    installed-from-wheel case can fail cleanly in main()."""
    if not path.exists():
        return None
    src = str(_SRC_DIR)
    if src not in sys.path:
        sys.path.insert(0, src)
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _verify_argv(rest: List[str]) -> List[str]:
    """Map `ghostprint verify ...` onto verify-print.py's argv.

    Two-positional form (suspect master) is translated — decode-stl spells
    the master as --master. Everything else passes through unchanged, and
    a leading verify-print.py subverb (decode-gcode / decode-stl /
    self-test) is routed as-is.
    """
    if rest and rest[0] in _VERIFY_SUBVERBS:
        return rest
    if rest and rest[0].startswith("-"):
        # Flags-first ordering (--amplitude X / --manifest m.json BEFORE the
        # positionals) can't be translated positionally: pass through and let
        # decode-stl's argparse produce its clean required-argument error.
        return ["decode-stl"] + rest
    if len(rest) >= 2 and not rest[1].startswith("-"):
        # decode-stl spells the master as --master.
        return ["decode-stl", rest[0], "--master", rest[1]] + rest[2:]
    return ["decode-stl"] + rest


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="ghostprint",
        description="ghostprint — two-layer invisible watermark for 3D-printed parts",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    tag = sub.add_parser("tag", help="tag an STL: invisible fingerprint encoder (tag-print.py arguments)")
    tag.add_argument("tag_args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    verify = sub.add_parser("verify", help="verify a tagged STL or G-code (verify-print.py arguments)")
    verify.add_argument("verify_args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    dg = sub.add_parser("decode-gcode", help="recover the L1 (printer+job+ts) payload from a G-code file")
    dg.add_argument("decode_gcode_args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    sub.add_parser("self-test", help="generate a cube, tag, decode both layers, assert round-trip")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)

    tag_mod = _load_sibling("gp_tag_print", _TAG_PRINT)
    verify_mod = _load_sibling("gp_verify_print", _VERIFY_PRINT)
    if tag_mod is None or verify_mod is None:
        print("ghostprint CLI verbs require a repo clone for now — reinstall from git",
              file=sys.stderr)
        return 2

    if args.cmd == "tag":
        return tag_mod.main(args.tag_args)
    if args.cmd == "verify":
        return verify_mod.main(_verify_argv(args.verify_args))
    if args.cmd == "decode-gcode":
        return verify_mod.main(["decode-gcode"] + args.decode_gcode_args)
    if args.cmd == "self-test":
        return verify_mod.main(["self-test"])
    ap.error(f"unknown command: {args.cmd}")  # unreachable: subparsers are required


if __name__ == "__main__":
    sys.exit(main())
