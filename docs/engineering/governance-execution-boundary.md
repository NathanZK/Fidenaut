# Proposed governance/execution boundary

> **Status:** architecture proposal for review. This document does not
> authorize an implementation, select an execution provider, or change the
> current workflow.

## Purpose

Fidenaut currently combines a governed workflow controller with a managed
agent-execution runtime. The proposed direction is to keep Fidenaut
authoritative for governance and delegate agent execution to an external
execution provider.

This is a boundary proposal, not a replacement decision. Issue #72 identified
projects for deeper evaluation, but no particular executor, protocol, durable
workflow engine, policy engine, or provider has been selected.

## Why consider the boundary

Fidenaut's governance guarantees concern state transitions, evidence,
provenance, scope, separation of producer and reviewer work, and human
approval. Its current runtime also owns role worker processes, Unix-socket IPC,
process-group lifecycle, history journals, receipts, response-loss recovery,
context replacement, and operation handling.

The Issue #61 run illustrates the distinction. The planner produced the
canonical plan, and the reviewer was invoked in a separate execution context.
The reviewer operation completed with exit code 1 because a Copilot model
catalog request timed out. Fidenaut correctly refused to advance and preserved
the evidence because no valid reviewer evidence existed. However, because
Fidenaut currently owns execution and recovery, a provider failure becomes a
Fidenaut runtime problem: the completed nonzero operation has no governed retry
path.

This is an architectural observation, not authorization to add retry behavior.
Under the proposed boundary, provider-level retries and recovery would belong
to the executor. Fidenaut would remain blocked until the executor supplied
evidence that Fidenaut independently accepted.

## Fidenaut governance/control plane

Fidenaut remains authoritative for:

- the workflow state machine and fail-closed transitions;
- role requirements and role-to-execution-context binding records;
- producer/reviewer separation requirements and context lineage records;
- canonical artifact storage, hashing, and integrity verification;
- approved scope and path validation;
- evidence validation and revalidation at each gate;
- human approval and explicit approval binding;
- revision authorization and revision limits;
- validation of repository state, source changes, ancestry, and topology;
- deciding whether an executor outcome is sufficient to unlock a transition.

The existing workflow's state, journals, Git checks, artifact identity, review
gates, and human acknowledgments remain governance concerns. Human
acknowledgment remains a workflow gate; an executor wait or executor status is
not approval.

## External execution layer

The execution provider is responsible for:

- creating, resuming, and forking execution contexts;
- running agents and their tools;
- model and provider interaction;
- process, sandbox, and runtime lifecycle;
- provider-level retries and timeout handling;
- execution recovery and response persistence;
- cancellation;
- internal execution history and persistence needed for those operations.

Fidenaut should not need to understand provider-specific failure mechanisms,
process groups, sockets, or model-catalog behavior. It may classify an outcome
as acceptable or unacceptable for a governed transition, but it should not
have to implement the provider's retry protocol.

## Conceptual executor contract

The names below are illustrative. The required semantics matter more than a
particular API or wire protocol.

| Operation | Fidenaut needs to know | Executor remains responsible for |
| --- | --- | --- |
| Create context | A newly created context identifier, its role/scope metadata, and any declared parent/lineage reference | Allocating isolated context state and making the new context usable |
| Resume context | Whether the requested identifier resumed, is unavailable, or was replaced; never a silent substitute | Restoring the same context's state and reporting availability |
| Fork or replace context | A distinct new identifier and explicit parent/replacement lineage | Copying or rebuilding whatever state is permitted, without disguising the child as the original |
| Run operation | Stable operation identifier, context identifier, outcome status, returned artifacts/results, and executor event reference where available | Running the agent, provider calls, timeouts, retries, and internal operation durability |
| Fetch outcome | The same operation's durable outcome, or an explicit unknown/unavailable result; no accidental re-execution | Persisting and looking up outcomes idempotently after response loss |
| Cancel | Whether cancellation was accepted, completed, or remains unknown | Stopping the operation and handling races with completion |

The contract must support at least these outcome distinctions:

- succeeded with returned result/artifact data;
- failed because of agent work;
- failed because of a transient or permanent provider/runtime problem;
- cancelled;
- unknown or unavailable.

An unknown outcome must make Fidenaut fail closed. Fidenaut may authorize a
new operation after a classified transient failure, subject to its own
governed limits, but it must not silently reinterpret an unknown operation as
safe to repeat.

### IDs, roles, and lineage

Fidenaut supplies or records a governed operation identity and binds the
operation to a workflow, role, and context reference. The executor supplies
execution and context identifiers and must preserve their meaning across
resume, fork, and outcome lookup. A context identifier is a reference, not
proof of independent actor identity.

The executor may expose richer event history or lineage, but Fidenaut must
record enough information to determine which context was assigned to which
role, which context produced an artifact, and whether a replacement is a new
identity. A fork or replacement must never be presented as a continuation of
the original context merely because it inherited state.

### Artifact transfer

The executor can return files, diffs, reports, or content-addressed references.
Fidenaut copies or retrieves the bytes into its canonical artifact store and
recomputes their digest and length. Only bytes received and re-hashed by
Fidenaut count as governed artifact evidence.

Repository changes remain subject to Fidenaut's independent scope, path, Git,
and validation checks. An executor-authored report describing a change is not
itself proof that the repository contains that change.

## Trust boundary

The executor is not authoritative over Fidenaut's workflow. Fidenaut treats
executor responses as claims and inputs to independent checks.

| Fact | Fidenaut can independently verify | Requires trust in or attestation from executor |
| --- | --- | --- |
| Artifact bytes and digest | Re-read bytes, hash them, and store them canonically | That the executor selected the intended source before transfer |
| Source changes and scope | Compute and inspect Git changes against approved paths | That the executor's internal workspace was the one it reports |
| Validation result | Run required validation or inspect independently captured evidence | That an executor-reported test ran in the claimed environment |
| Operation status | Require a durable outcome and reject unknown/malformed results | The executor's classification of provider vs. agent failure |
| Context relationship | Compare recorded IDs and lineage and reject collisions | That distinct IDs represent genuinely distinct state and no hidden shared history |
| Same-role resumption | Check that the bound ID is returned on resume | That the executor restored the same context state |
| Replacement lineage | Record the new ID and parent relationship | That the executor did not silently reuse the old context's state |

This is not an authentication or cryptographic actor-identity design. The
workflow decides whether the available context relationship satisfies its
governance rule; it does not claim that a role label or executor ID proves who
controls a context.

## Role separation under delegation

The expected governance rules are:

1. Planner, reviewer, test implementer, and implementer bindings must resolve
   to distinct execution contexts where the workflow requires separation.
2. A same-role revision resumes the appropriate producer context rather than
   silently switching contexts.
3. An unavailable-context replacement or authorized fork receives a distinct
   execution identity and records explicit lineage to the original.
4. Fidenaut rejects collisions or missing relationship evidence and does not
   advance on an unverifiable binding.

A self-reported context ID does not automatically prove independence. Whether
fresh executor contexts have isolated prompts, histories, filesystems, and
permissions is an executor trust-boundary question that requires prototype
validation. The current implementation also treats its runtime evidence as
execution evidence rather than authentication; delegation changes who attests
the context facts, not the governance rule Fidenaut applies to them.

## Current implementation versus proposed target

Today, the managed runtime in
[`scripts/workflow_supervisor.py`](../../scripts/workflow_supervisor.py)
provides:

- long-lived role worker processes;
- Unix-socket request/response IPC;
- per-context history journals and runtime receipts;
- operation identity and response-loss recovery;
- context resume and explicit replacement/recovery;
- bounded operation lifecycle and process-group supervision.

[`scripts/agent_workflow.py`](../../scripts/agent_workflow.py) binds those
runtime instances to workflow roles, checks distinctness, persists pending
operations, revalidates receipts, and verifies returned artifacts and changed
paths. This machinery is part of the current prototype and is not deprecated,
removed, or refactored by this document.

The proposed architecture asks whether those execution responsibilities can be
delegated behind the contract above. It does not presume that every current
check can be deleted: Fidenaut's artifact, scope, evidence, approval, and
fail-closed checks remain even if an executor supplies stronger journals.

## Options and current direction

| Architecture | Fidenaut owns | Main trade-off |
| --- | --- | --- |
| Fidenaut-owned execution | Governance and all agent process/runtime machinery | Maximum local control, but provider/runtime failures and lifecycle complexity remain in Fidenaut |
| Governance plus external agent runtime | Governance, evidence, approvals, and an executor adapter | Smallest proposed boundary; depends on executor context and outcome semantics |
| Governance plus durable workflow engine plus agent runtime | Governance, a durable orchestration substrate, and an agent executor | May improve operation durability, but adds another platform and risks moving governance into substrate semantics |

The current direction is the middle option, with a thin adapter contract and
the executor treated as untrusted. A durable workflow engine is not part of
the initial decision; it becomes relevant only if prototype evidence shows
that an agent runtime cannot supply adequate operation durability.

No technology is selected. Candidates from Issue #72 — including OpenHands,
Goose, Codex, Copilot, ACP, A2A, Temporal, DBOS, Restate, OPA, and OpenFGA —
remain candidates for deeper evaluation only. Policy engines are governance
primitives, not execution replacements.

## Unresolved questions for a prototype

- Can a selected executor demonstrate genuinely isolated fresh contexts for
  planner and reviewer, including history, prompt, filesystem, and permissions?
- Can it resume the original producer context and represent a replacement as a
  distinct, traceable identity?
- Can Fidenaut retrieve artifacts and repository changes without sharing its
  worktree or trusting executor-authored descriptions?
- Is outcome retrieval idempotent after a lost response, including a completed
  nonzero provider failure?
- Can the executor expose failure classes and bounded retry behavior without
  requiring Fidenaut to understand provider-specific mechanisms?
- Which executor facts can be independently checked, and which must remain
  explicit trust assumptions?
- Can one provider-neutral adapter support more than one execution technology?
- What migration or supersession rule is needed for existing runs whose
  context bindings refer to the current managed runtime?

Until these questions are answered, the current managed runtime remains the
active implementation and the governance/execution split remains a proposed
architecture.
