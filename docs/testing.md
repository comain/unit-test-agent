# Testing

The default suite is hermetic. Heavier lanes are marked and opt-in.

```bash
python3 -m pytest -m "not integration"
```

**E2E test modes**:

- Default subsystem and hermetic E2E: `python3 -m pytest`. This includes `tests/e2e/test_scripted_pipeline_e2e.py`, which exercises Java and Python parse/context -> generate -> materialize -> verify -> repair task plumbing with scripted agent/verifier doubles. It does not require live OpenCode, Maven/PIT, mutmut, network, or real corporate repos.
- Existing staged/real-repo checks: marker **`e2e_git_home`** and env-gated tests such as `tests/e2e/test_phase9_staged_verification.py` and `tests/test_cross_repo_smoke.py`. Run explicitly with `pytest -m e2e_git_home` or the documented test path. These can skip when local repos, JDK/Maven artifacts, or Python runtimes are unavailable.
- Fresh-clone real E2E: marker **`real_e2e`**, enabled only with `UTA_E2E_MODE=real` or `UTA_E2E_MODE=nightly`, for example `UTA_E2E_MODE=real python3 -m pytest -m real_e2e tests/e2e/`. This mode fresh-clones/copies the configured Java WMS repo and the UTA repo itself for the Python 3 lane, never mutates source workspaces directly, and writes `.uta_reports/real-e2e/real-e2e-results.json`. Python 2 remains covered by staged legacy-runtime checks, not by the current `real_e2e` lane set. Node2 scheduling for this mode is intentionally postponed.

Live-agent smoke checks are provider/auth diagnostics, not UTA correctness gates. Keep them separate from default hermetic E2E and from the `real_e2e` report contract.

**E2E / pytest repo paths** (defaults assume sample under `~/wms/`): `UTA_E2E_REPO`, `UTA_E2E_MODULE`; secondary block: `UTA_E2E_OUTBOUND_REPO`, `UTA_E2E_OUTBOUND_MODULE`. Python real E2E defaults to this UTA repo and writes a clone-only probe target/test, `uta_e2e_probe.py::discounted_total` and `tests/test_uta_e2e_probe.py`, so coverage/mutation evidence is non-empty without mutating the source repo. The dev extra pins patch-capable mutmut 2.x plus `whatthepatch` for stable local line-diff mutation. Override it with `UTA_E2E_PY3_REPO`, `UTA_E2E_PY3_TARGET`, and `UTA_E2E_PY3_TEST_PATHS` for another Python repo. Cross-org smoke (`tests/test_cross_repo_smoke.py`, marker **`e2e_git_home`**): `UTA_E2E_PLATFORM_REPO`, `UTA_E2E_PLATFORM_MODULE`, `UTA_E2E_TMS_REPO`, `UTA_E2E_TMS_MODULE` (defaults `~/platform/sample-baseinfo-product`, `~/tms/tms-order-core`). Run with `pytest -m e2e_git_home`; omit from default CI with `pytest -m "not e2e_git_home"`. Baseline compile may **skip** when sibling artifacts are not in the local `.m2` (e.g. off-VPN). E2E runs now **preserve** `.uta_cache`, `.uta_summary.md`, and related artifacts by default for postmortem inspection; set `UTA_E2E_KEEP_ARTIFACTS=false` to restore the old cleanup behavior.
