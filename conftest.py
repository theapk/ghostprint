# Ensure the repo root is importable so tests' absolute imports
# (e.g. `from tests.test_landing_serve import ...`) resolve under ANY
# pytest invocation — console-script `pytest` (no cwd on sys.path) and
# `python -m pytest` alike.
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
