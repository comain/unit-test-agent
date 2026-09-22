# Binary Mutmut Meta Crash

## Problem as reported

Report `0a2f342d872c489190c9660370a60243` (`w_ais_gaia`, branch
`TASK-40998-20260908`) failed with:

```
UnicodeDecodeError: 'utf-8' codec can't decode byte 0xba in position 1
```

in `read_mutmut_meta`. Enforcement produced no UTA evidence, so one-click repair
could not select safe target files (`repair_task_create_failed`).

## Affected data

- App: `w_ais_gaia` / `git@git.example.com:aistore/gaia.git`
- Workspace: `/opt/app/uta-data/workspaces/0a2f342d872c489190c9660370a60243/gaia`
- Binary TensorFlow checkpoints copied into the mutmut tree:
  - `mtcnn/data/MTCNN_model/ONet_landmark/ONet-16.meta`
  - `mtcnn/data/MTCNN_model/PNet_landmark/PNet-18.meta`
  - `mtcnn/data/MTCNN_model/RNet_landmark/RNet-14.meta`
- `ONet-16.meta` starts `0a ba 45 12 ...` (protobuf), matching byte `0xba` at
  position 1.

## Root cause

`read_mutmut_meta` used `mutants_dir.rglob("*.meta")` and decoded every match as
UTF-8 JSON. Mutmut 3 writes `<source.py>.meta`. Mutmut also copies the whole
repo into `mutants/`, so TensorFlow `.meta` checkpoints were parsed as JSON.
`UnicodeDecodeError` was not in the skip list (`OSError`, `JSONDecodeError`).

Failure boundary: mutation metadata scoring after a successful generate pass.
Causal change: none in UTA; the crash is latent whenever a Python repo contains
non-mutmut `*.meta` files.

## Fix

- Read only mutmut sidecars (`*.py.meta`).
- Treat `UnicodeError` like other unreadable meta files.
- Ignore non-object JSON payloads.

## Verification

- RED: `tests/test_mutation.py` reproduced the production prefix and crash.
- GREEN: that test plus related mutation tests passed.
- Local Python enforcement vs `origin/main`: changed-line coverage 5/5 (100%).
  Mutation reported 3 survivors because mutmut's path-based module name
  (`tools.python-enforcement.uta_py_enforce.mutation`) does not match the
  `uta_py_enforce.mutation` import the test uses; mutmut logged
  "could not find any test case for any mutant". That is a pre-existing
  nested-package trampoline mismatch, not this crash.
