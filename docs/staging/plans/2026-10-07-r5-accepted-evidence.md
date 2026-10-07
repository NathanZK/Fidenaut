# R5 baseline and accepted-evidence plan

goal: Implement issue #80's provider-independent R5 baseline, delta, and accepted-evidence verification in the existing governance operation boundary.
spec: `docs/staging/specs/2026-10-07-r5-accepted-evidence.md`

- [x] T1: Capturing deterministic R5 baselines at admission
  goal: Capture and persist the operation-bound same-host worktree identity, HEAD, raw status, framed tree digest, and minimal baseline manifest before reservation.
  files: `scripts/agent_workflow.py`, `scripts/tests/test_agent_workflow.py`
  acceptance: Focused temporary-Git-worktree tests assert identity, HEAD, digest framing, manifest fields, filemode detection, ignored-untracked exclusion, and fail-closed status/read errors; `python3 -m unittest discover -s scripts/tests -p test_agent_workflow.py`
  spec: `docs/staging/specs/2026-10-07-r5-accepted-evidence.md#contract`

- [x] T2: Computing and validating accepted worktree deltas
  goal: Recompute stable acceptance snapshots, derive deterministic path deltas against baseline and HEAD, and enforce scope and READ_ONLY rules.
  files: `scripts/agent_workflow.py`, `scripts/tests/test_agent_workflow.py`
  acceptance: Focused tests assert exact delta schema/order for add/modify/delete/mode/symlink, path scope rejection, reviewer mutation rejection, wrong worktree/HEAD rejection, and rename/delete-recreate/type-change behavior; `python3 -m unittest discover -s scripts/tests -p test_agent_workflow.py`
  spec: `docs/staging/specs/2026-10-07-r5-accepted-evidence.md#contract`

- [x] T3: Verifying operation-scoped evidence before acceptance
  goal: Re-read the required operation output, reject incomplete or unstable evidence, and attach Fidenaut-computed artifact and delta data to the existing governance-verified success transition.
  files: `scripts/agent_workflow.py`, `scripts/tests/test_agent_workflow.py`
  acceptance: Focused tests prove canonical op-scoped containment, symlink-escape/wrong-operation/wrong-role rejection, computed hash/length, changed bytes rejection, and that executor claims cannot authorize or override evidence; `python3 -m unittest discover -s scripts/tests -p test_agent_workflow.py`
  spec: `docs/staging/specs/2026-10-07-r5-accepted-evidence.md#acceptance-tests`

- [x] T4: Verifying failure fences and documenting R5 behavior
  goal: Prove persistence, concurrency, revision-fence, and provider-independent failure behavior, then update living operation/workflow documentation.
  files: `scripts/agent_workflow.py`, `scripts/tests/test_agent_workflow.py`, `docs/specs/role-operation-records.md`, `docs/engineering/agent-workflow.md`
  acceptance: Fault and concurrency tests preserve G9 and prior state on write failure, stale acceptance, and retry; then `python3 scripts/run_python_lint.py` and `python3 scripts/run_agent_workflow_tests.py` pass. Documentation records exact ignored-path and evidence-verification behavior.
  spec: `docs/staging/specs/2026-10-07-r5-accepted-evidence.md#failure-handling`
