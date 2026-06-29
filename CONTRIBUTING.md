# Contributing to theapk · ghostprint

Thanks for your interest. Ghostprint is an open-source tool under theapk llc;
the hosted/managed tiers (web tool, paid API) live separately.

## Quick rules

- Contributions welcome. The project uses the PolyForm Noncommercial License — contributions are accepted under the same terms.
- Bug fixes: open an issue or PR on GitHub (`github.com/theapk/ghostprint`).
- New fingerprinting layers, codec changes, or anything touching the watermark
  math: **open an issue first.** We want to discuss the threat model before
  merging. Bad watermark = worse than no watermark.
- Keep changes small and focused. One PR = one thing.
- Add or update tests in `tests/` for any behavior change. The round-trip
  property (tag → decode → verify) must not regress.

## Local dev

```bash
git clone https://github.com/theapk/ghostprint.git
cd ghostprint
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest tests/
```

## Reporting security issues

If you find a way to defeat the watermark (forge an L1 stamp, recover the
master STL from a tagged STL with sub-noise precision, etc.), please email
security@theapk.com rather than opening a public issue. We'll work with you
on coordinated disclosure.

## Style

- Python 3.10+. No type stubs required, but type hints are appreciated.
- Pure Python where possible. The CLI tools (`tag-print.py`, `verify-print.py`)
  are the public surface; keep their args stable.
- One commit per logical change. Imperative-mood subject lines.
- If you're adding a dependency, justify it in the PR — pure-Python + numpy
  is the floor.

## Theapk product line

Ghostprint is `theapk · ghostprint`. Sub-products (verify, tag, api) may live
in this repo or in sibling repos on the same GitHub org. When in doubt, ask
in the issue before starting a large change.

Thanks.
