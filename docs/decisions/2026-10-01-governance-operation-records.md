# Governance-owned role-operation records

## Context

Fidenaut needs a governance boundary for role operations that remains independent of executor protocols and provider lifecycle.

Operation state must be written atomically with the owning workflow run, and concurrent admission must preserve the one-open-operation invariant.

## Choice

Store immutable run mode and role-operation records in the owning `state.json`.

Use the existing per-run lock and compare-and-swap revision for admission, acceptance, and stale-writer protection. CAS rejects stale in-flight snapshots that would overwrite a newer persisted revision; it does not detect an out-of-band restoration of an older valid `state.json`.

Require a later operation after terminal failure to link the prior record to an existing revision-request or revision-authorization event.

## Ruled out

- A separate operation ledger, because coordinating it atomically with workflow state would add a second persistence boundary.
- A new anti-rollback watermark or revision journal, because the existing state compare-and-swap is the selected mechanism. External state-file rollback detection is consequently not guaranteed.
- Executor, provider, context-lifecycle, retry, and evidence-acceptance behavior, because these belong to separate boundaries.
