"""ghostprint — two-layer invisible watermark for 3D-printed parts (v0.1.1).

Layer 1 (L1): G-code micro-watermark (printer_id + job_seq + timestamp).
Layer 2 (L2): geometric steganography (order_id -> vertex perturbations).

The implementation lives in :mod:`ghostprint.core`; the tag/verify CLIs
live in tag-print.py / verify-print.py (repo clone layout), and
:mod:`ghostprint.cli` exposes the ``ghostprint`` console script.

Version matches ``theapk-ghostprint`` in pyproject.toml (0.1.1).
"""

from .core import *  # noqa: F401,F403
from .core import (  # noqa: F401
    GhostprintManifest,
    L1_PREAMBLE,
    L1_SCHEME_VERSION,
    L2_AMPLITUDE_MAX_MM,
    L2_AMPLITUDE_MIN_MM,
    file_sha256,
    l1_babysteps_to_payload,
    l1_decode_payload,
    l1_encode_payload,
    l1_extract_from_gcode,
    l1_payload_to_babysteps,
    l1_rewrite_gcode,
    l2_order_hash,
    l2_perturb_vertices,
    l2_recover_deltas,
    l2_recover_order,
    l2_verify_match,
)

__version__ = "0.1.1"
