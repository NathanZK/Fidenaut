# Proposed governance/execution boundary

> **Status:** architecture proposal for review. This document does not
> authorize an implementation, select an execution provider, or change the
> current workflow.
>
> The narrower, experiment-derived boundary and minimum executor semantics
> are proposed in
> [`external-executor-contract.md`](external-executor-contract.md).

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
Under a possible delegated boundary, the executor would run operations and
expose outcomes and artifacts under the protocol in
[`external-executor-contract.md`](external-executor-contract.md). Fidenaut
would remain blocked until it recovered and independently accepted adequate
evidence. Whether an executor may retry internally, and under what conditions,
is constrained by that contract's operation identity and side-effect rules;
Fidenaut never delegates workflow authorization to an executor.

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

An external execution layer, if selected, would run agents and their tools,
manage its own provider/runtime details, and expose context, operation,
outcome, and artifact behavior meeting the normative minimum contract in
[`external-executor-contract.md`](external-executor-contract.md). Its
internals—queues, process lifecycle, persistence mechanisms, retry
algorithms, and protocols—are not prescribed. It must not make workflow
decisions for Fidenaut. Cancellation is optional; executor forks are not a
required boundary operation. Fidenaut independently validates returned
evidence and decides whether any workflow transition is permitted.

## Normative external-executor contract

[`external-executor-contract.md`](external-executor-contract.md) defines the
minimum proposed protocol semantics, operation identity and recovery rules,
context requirements, artifact boundary, outcome model, exclusions, and
unresolved decisions. It is normative for this architecture proposal;
summaries elsewhere in this document do not add to or override it.

The current conceptual operation names are create context, resume context,
run operation, fetch outcome, and optional cancel operation. Replacement is a
Fidenaut-governed creation of a distinct context with lineage recorded by
Fidenaut; executor fork semantics are not required. The executor contract
does not prescribe internal implementation or guarantee that arbitrary
external side effects are transactional.

### IDs, roles, and lineage

Fidenaut assigns the operation identity and binds it to a workflow, role, and
context reference. The executor must preserve that operation identity and the
exact context meaning defined in the normative contract across resume and
outcome lookup. A context identifier is evidence/reference, not proof of
independent actor identity or isolation.
Fidenaut records which context was assigned to each role, which context
produced an artifact, and whether a replacement is a new identity. Any
executor context lineage is supplemental evidence; replacement lineage and
authorization remain Fidenaut-owned.

### Artifact transfer

The executor may return files, diffs, reports, or content-addressed references
only in a form that meets the artifact requirements in the normative
contract, including an explicit completeness assertion for repository
changes. Fidenaut obtains the actual bytes into its canonical artifact store
and recomputes their digest and length; executor-provided hashes are claims,
not authoritative values. Only bytes received and independently verified by
Fidenaut count as governed artifact evidence. An absent or incomplete
retrieval is not evidence of no changes.

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
| Operation status | Require a durable operation observation/outcome and reject unknown or malformed results | The executor's classification of provider vs. agent failure |
| Context relationship | Compare recorded IDs and lineage and reject collisions | That distinct IDs represent genuinely distinct state and no hidden shared history |
| Same-role resumption | Check that the bound ID is returned on resume | That the executor restored the exact same context state, subject to qualification evidence |
| Replacement lineage | Record the new ID, old binding, reason, and authorization | That the new context has the qualified isolation properties required for its role |

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
3. An unavailable-context replacement is a Fidenaut-authorized fresh context
   with a distinct identity and explicit Fidenaut-owned lineage to the
   original; an executor fork is not required.
4. Fidenaut rejects collisions or missing relationship evidence and does not
   advance on an unverifiable binding.

A context ID or role label does not prove independence. Contexts must meet the
deployment-specific isolation qualification evidence defined by the
normative contract, including identification of executor versus host
guarantees and conformance checks for relevant shared state. The current
implementation also treats its runtime evidence as execution evidence rather
than authentication; delegation changes who attests context facts, not the
governance rule Fidenaut applies to them.

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
| Governance plus external agent runtime | Governance, evidence, approvals, and an executor adapter | Potentially smaller runtime burden; depends on executor context and outcome semantics |
| Governance plus durable workflow engine plus agent runtime | Governance, a durable orchestration substrate, and an agent executor | May improve operation durability, but adds another platform and risks moving governance into substrate semantics |

The middle option remains under evaluation; it is not a selected direction.
A future adapter design can proceed only after the proposed contract's
unresolved policy decisions are resolved and an executor is shown to meet its
requirements. A durable workflow engine is not selected or excluded; its
relevance depends on whether an eventual execution layer can meet the
operation-recovery contract without moving governance authority out of
Fidenaut.

No technology is selected. Candidates from Issue #72 — including OpenHands,
Goose, Codex, Copilot, ACP, A2A, Temporal, DBOS, Restate, OPA, and OpenFGA —
remain candidates for deeper evaluation only. Policy engines are governance
primitives, not execution replacements.

## Unresolved design decisions and research

The contract deliberately leaves these governance/design questions open;
neither this document nor a prototype should assign arbitrary defaults:

- **Isolation qualification:** what deployment-specific evidence and
  conformance results are sufficient to qualify executor/host guarantees for
  role contexts?
- **Persistently unknown operation:** what explicit human/governed disposition,
  if any, can unblock work when the same operation remains `UNKNOWN`, and what
  evidence is required without claiming it failed or was canceled?
- **Terminal-failure retry policy:** which known terminal failures, if any,
  can authorize a distinct new operation at each role gate, subject to
  approval and revision limits?
- **Artifact base and retrieval:** what repository identity/base evidence and
  retrieval binding are required for repository changes and non-Git outputs?

Research must also establish whether a candidate can meet the contract's
context continuity, outcome recovery, failure/side-effect certainty, and
artifact requirements. The #72 OpenHands and Goose experiments are evidence
for the requirements, not a permanent selection or rejection of either
project. Provider-neutrality and any migration/supersession rule for existing
managed-runtime context bindings remain evaluation questions, not contract
semantics.

Until these questions are answered, the current managed runtime remains the
active implementation and the governance/execution split remains a proposed
architecture.
