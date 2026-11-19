# Security Policy — theapk · ghostprint

## Reporting a vulnerability

Email **support@theapk.com**. Do not open a public issue with exploit details.

Please include: affected component (repo path or hosted service), version/commit,
a reproduction or PoC, and the impact you see.

## Response expectations

- **Acknowledgement:** within 48 hours of your report.
- **Fix:** within 30 days for critical issues; remaining issues get a fix or a
  published mitigation on a timeline we'll state in the initial reply.
- We'll keep you in the loop and credit you if you want.

## Scope

- **This repository** (`github.com/theapk/ghostprint`): `tag-print.py`,
  `verify-print.py`, the `src/ghostprint` CLI, and the local web tooling in
  `landing/`.
- **Hosted version** (`ghostprint.theapk.com`): the drag-drop web tool and the paid
  API.

## Watermark-defeat research

Ghostprint is forensics, not DRM: it can't stop copying. Attempts to strip or
forge the watermark are an expected part of the threat model — finding one is
research, not a repository bug. If you find a way to defeat the watermark
(forge an L1 stamp, recover the master STL from a tagged STL with sub-noise
precision, etc.), please email support@theapk.com rather than opening a public
issue. We'll work with you on coordinated disclosure.
