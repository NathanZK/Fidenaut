# Governance-owned role-operation records

## Run identity and mode

- New runs store a Fidenaut-generated 32-character hexadecimal `run_id`.
- New runs pin `role_execution_mode` as `managed-v0` or `executor-v1`.
- `workflow.role_execution` selects the mode and defaults to `managed-v0`.
- `role_execution_contexts: true` enables managed execution and conflicts with explicit `executor-v1`.
- Legacy runs are managed only when role bindings contain unambiguous `instance_id` values.
- Ambiguous legacy state and configuration/run mode mismatches fail closed.
- Selecting `executor-v1` does not implement an executor or provider integration.

## Operation identity

- Records use format `fidenaut-role-op-v1` and belong to one persisted run.
- Fidenaut mints globally unique `op_id` values across canonical and superseded run states.
- Each record stores run, mode, gate, role, request identity, fingerprint, revision, attempt, status, and optional result or failure.
- Request identity includes all execution-affecting request values, role binding, purpose, canonical inputs, and gate revision.
- Canonical JSON sorts object keys, uses ASCII, and omits insignificant whitespace.
- Identity accepts JSON nulls, booleans, integers, finite numbers, strings, arrays, and string-keyed objects only.
- Duplicate JSON keys, non-finite numbers, malformed records, and conflicting fingerprints fail closed.
- `request_fp` is SHA-256 over canonical request identity; observations and delivery attempts are excluded.
- Delivery attempt starts at one and remains immutable in this protocol.

## Admission and authorization

- Admission acquires the artifact-root operation-ID lock, then the per-run lock, then the state-write lock.
- Admission validates persisted run identity, configured and pinned mode, gate, role, revision, binding, and request identity.
- Admission persists `reserved` before any delivery boundary and enforces at most one open operation per run.
- Exact repeated admission returns the existing record without writing state.
- Changed meaning, duplicate fingerprints under different IDs, global ID reuse, stale revisions, and open-operation conflicts fail closed.
- A later operation for the same role and purpose requires a prior terminal operation and a linked governance event.
- A failed operation cannot be bypassed by changing its purpose; subsequent work for that role must link the latest failure to a governance event.
- The authorization reference binds the prior operation, gate, and state revision to the exact persisted event.
- Authorization events are single-use for role-operation chaining.
- Revision runs pin a new run identity and the configured execution mode, as ordinary runs do.
- An `unknown` operation cannot be superseded.

## State transitions and persistence

| Current status | Allowed next status |
| --- | --- |
| `reserved` | `dispatched`, `unknown` |
| `dispatched` | `unknown`, `succeeded`, `failed` |
| `unknown` | `unknown`, `dispatched`, `succeeded`, `failed` |
| `succeeded` | Exact replay only; read-only |
| `failed` | Exact replay only; read-only |

- Only an internal typed governance verification result can transition a record.
- Raw executor/provider observations and caller-supplied status strings cannot advance governance state.
- Terminal results and request identities remain immutable.
- `unknown` can be reconciled only for the same operation ID and request identity.
- Transition acceptance reacquires the run lock and revalidates persisted run, mode, gate, operation, and revision.
- State writes use atomic replacement and compare-and-swap against the currently persisted revision. Concurrent stale writers fail closed without partial state changes.
- CAS prevents a stale in-flight snapshot from overwriting a newer persisted revision. It does not detect an out-of-band restoration of an older valid `state.json`; this limitation is intentional because the protocol has no rollback watermark or revision journal.
- A reserved operation can be replayed idempotently after process restart before delivery; a dispatched operation can be accepted after restart only after persisted identity and revision checks pass.

## R5 baseline, delta, and accepted evidence

- R5 inspects only the operation-bound, Fidenaut-created Git worktree on the same host. It does not create worktrees or support arbitrary external or cross-host worktrees.
- Admission stores the canonical worktree root, worktree Git directory, common Git directory, `HEAD`, a framed snapshot digest, and a sorted path manifest in the G-record baseline. Git status is captured with `-c core.filemode=true status --porcelain=v2 -z --untracked-files=all`; the exact status bytes are included in snapshot verification.
- Ignored untracked paths are excluded from the baseline and accepted delta because the pinned status command omits them. A tracked file remains included when changed even if it matches an ignore rule. An ignored untracked path alone is not an accepted change.
- Acceptance revalidates worktree identity and `HEAD`, captures a stable final snapshot, and computes a deterministic path delta against admission state and the unchanged HEAD tree. Delta paths are checked against the run's approved scope and role-specific test/implementation path split. Renames are represented as deletion plus addition; reviewers configured `READ_ONLY` require an empty delta.
- The G-record's Fidenaut-derived `output_path` is `ops/<op_id>/<kind>` relative to the run root. Successful `result` stores `{executor_result, accepted_artifact, accepted_delta}`; executor claims are informational. `accepted_artifact` records the operation-relative path, Fidenaut-computed lowercase SHA-256, and byte length. The output is read twice and must remain a regular, stable file in the operation-scoped directory.
- Invalid or unstable Git snapshots, changed identity/HEAD/revision, out-of-scope changes, missing or unreadable output, path escape, role mismatch, or differing output reads fail closed before success is persisted. Persistence failures leave prior state intact; retries recapture and reverify evidence. Existing locks, G9, CAS revision checks, and terminal replay rules remain in force.
- Pre-R5 records without a baseline remain readable as persisted history. An open pre-R5 operation cannot transition to `succeeded`, because its admission baseline cannot be reconstructed after the fact.
- These primitives do not implement executor dispatch, external provider lifecycle, or worktree creation.
