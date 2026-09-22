# Python Root Manifest Runtime Mismatch

## Incident

Repair session `c37351284a7d449e83b1251ad7ad0b02` generated and pushed a
target-specific test, then failed before coverage because UTA installed the
repository-root `requirements.txt` into its Python 3.11 dependency overlay.
The root manifest pins a Python 3.6-era environment; the changed nested target
documents Python 3.10 and does not depend on most of that manifest.

The first incompatible package was `absl-py==0.9.0`, whose legacy setup script
rejects Python 3.11. Retrying with disabled build isolation does not help.

## Required Behavior

1. Prefer the complete nearest manifest so pip remains the primary resolver.
2. If that manifest cannot build for the selected verifier runtime, discard
   the partial overlay and deterministically install only manifest
   distributions imported by the target and selected tests.
3. Remove legacy version pins in this compatibility fallback so pip can choose
   wheels for the selected runtime.
4. Record the failed whole-manifest attempt and the compatibility fallback in
   command evidence.
5. Never cache a partial or failed overlay.
6. Standalone enforcement and UTA repair verification must use the same
   dependency-overlay implementation.
