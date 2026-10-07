# Provider qualification evaluator

## Context

Issue #83 requires executable qualification assessment, while #82 leaves safe
provider-context creation unavailable and human governance values unresolved.
An evaluator can validate a supplied evidence bundle without producing real
U1/U3 evidence or activating provider adoption.

## Choice

Keep evaluation separate from provider workflow execution. Require caller-
supplied, role-scoped trust keys and signed, qualification-bound policy and
evidence. Return only `qualified` or `adoption unavailable`; qualification
does not itself enable adoption. Synthetic fixtures may exercise evaluator
behavior but are not qualification evidence.

## Ruled out

- Selecting human authority, verifier, deployment boundary, or U3 criteria.
- Creating provider contexts, invoking providers, or wiring provider workflows.
- Treating adapter/fake-runner tests or synthetic evidence as real U1/U3
  qualification.
- Enabling real-provider adoption through evaluator results.
