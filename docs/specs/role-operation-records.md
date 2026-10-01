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
- These primitives do not implement executor dispatch, evidence acceptance, R5 baseline/delta, retries, or provider lifecycle.
