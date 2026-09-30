# External-executor contract

> **Status: normative design proposal only.** This document does not authorize
> implementation, select an executor, or change the current managed runtime.
> Fidenaut remains the governance authority; any executor is subordinate to
> Fidenaut's workflow decisions and is treated as an untrusted source of
> claims and evidence.
>
> This proposal is derived from the OpenHands and Goose experiments recorded
> in [#72](https://github.com/NathanZK/Fidenaut/issues/72) and
> [#74](https://github.com/NathanZK/Fidenaut/issues/74), together with the
> existing role-context and operation-recovery requirements in #60 and #63.
> For executor protocol semantics, this document is normative. The broader
> architecture discussion in
> [`governance-execution-boundary.md`](governance-execution-boundary.md)
> defers to this contract wherever it discusses executor operations or
> outcomes.

## Objective and boundary

Fidenaut owns governed workflow state and transitions, role assignment,
producer/reviewer separation rules, approved inputs and scope, canonical
artifacts, evidence acceptance, validation, revision limits, human approval,
and the decision to accept or reject an execution result.

An external executor owns agent and tool execution, model/provider interaction,
runtime and process lifecycle, execution-context persistence, and the
mechanism by which it reports and recovers operation outcomes. The contract
must let Fidenaut recover the *same* accepted operation after response loss;
it must not make Fidenaut rebuild process supervision, IPC, a runtime journal,
or provider-specific retry logic.

The executor does not advance Fidenaut workflow state, authorize retries,
approve artifacts, or satisfy a human gate. Its responses are inputs to
Fidenaut's own checks, not workflow authority.

## Minimum boundary operations

Only four operations are mandatory: create a fresh context, resume that exact
context, submit an identified operation, and retrieve that operation's
outcome. Context replacement uses fresh creation plus Fidenaut-recorded
lineage; it does not require an executor fork operation. Cancellation is an
optional runtime control, not a workflow outcome or approval.

| Operation | Minimum semantics |
| --- | --- |
| **Create context** | Create a fresh, runtime-resolvable context and return its opaque executor context ID. Fidenaut binds that ID to its workflow execution and role. Creation must not silently return an existing context. A Fidenaut-assigned creation key must make recovery of a lost creation response idempotent: repeating it returns the same creation result, not another context. |
| **Resume context** | Resume the exact supplied context ID or report it unavailable. Never substitute a new context while claiming it is the original. Same-role re-entry uses this operation, preserving #60's producer/reviewer lifecycle. |
| **Submit operation** | Submit a Fidenaut-assigned operation ID, context ID, immutable request fingerprint, and the role work/instructions and canonical input references. Repeated submission of the same operation ID and fingerprint must resolve to the same accepted operation; it must not start a second execution. Reuse of that ID with a different fingerprint or context is a conflict. |
| **Get outcome** | Retrieve the state/result for the same operation ID without executing it. Lookup is repeatable and remains available after the initiating connection is lost and across executor restarts within the declared durability scope; that scope must meet the governed recovery and audit lifetime below. If the executor cannot establish the state, return `UNKNOWN`, not a success-shaped response or an assumed failure. |
| **Request cancellation (optional)** | If supported, request cancellation for a specific operation ID. The request is idempotent. It reports a request/acknowledgement separately from the final operation outcome; only a terminal `CANCELLED` outcome establishes cancellation. |

### Request and context identity

`operation_id` is the identity and idempotency key for one governed operation
submission. It is not accompanied by a second submission request ID. A
different operation is a distinct governed operation with its own ID.

Other state-changing actions use a stable idempotency key when the caller
needs to recover that action after a lost response. Context creation must use
such a key; cancellation, if offered, uses a stable key bound to its operation
ID. Repeating an action with the same key and same meaning must return the
same result; reusing the key with different content is a conflict. In
particular, replaying a lost context-creation response must not silently
create multiple contexts. A create request includes Fidenaut's
workflow-execution and role association and, only where relevant, the parent
context reference and governed replacement reason. Those fields are binding
metadata, not executor authority to assign roles or approve the request.
`resume context` and `get outcome` address the exact context or operation ID
and are repeatable reads.

### Identity and immutable request

Before dispatch, Fidenaut creates and durably records an operation ID. That ID
is the idempotency key for one logical operation; an executor-generated run ID
may be returned for diagnostics but cannot replace it. The immutable request
identity covers every execution-affecting input whose change could make the
submitted operation materially different. It includes, where applicable, the
governed workflow/run identity, role, bound context, operation ID, canonical
inputs and digests, instructions, repository identity and base revision,
effective tool permissions/capabilities, executor configuration/profile/
version, and requested output/evidence purpose. This list is illustrative,
not a provider-specific schema: an execution-affecting value must either be
bound into the request identity or fixed by the governed context for the
operation's lifetime. The contract prescribes neither serialization nor
storage.

Fidenaut retries delivery only with the same operation ID and identical
fingerprint. The executor must either report that this submission attempt was
accepted for the identified operation or return the definitive
`NOT_ACCEPTED` submission disposition; it must not start a second execution
for an already accepted operation with that key. A different operation ID
means a new operation and requires Fidenaut authorization. The operation ID
is a correlation and deduplication key, not proof of actor identity,
isolation, or successful execution. The executor must never return an
outcome for a materially different request under the same operation ID.

`NOT_ACCEPTED` is a submission disposition, not an operation outcome. It
means that this specific submission attempt was not accepted for execution,
and the executor establishes that it cannot later execute as a result of
that attempt. It is not an assertion that the governed operation is
permanently terminal: a later submission using the same operation ID and
identical fingerprint may be accepted according to the idempotent submission
semantics above. A timeout, missing acknowledgement, absent record, or
unavailable lookup is not `NOT_ACCEPTED`; absent definitive evidence, the
observed operation status is `UNKNOWN`.

## Operation lifecycle and outcome model

The submit response first reports a submission disposition: accepted for the
identified operation, or `NOT_ACCEPTED` for that specific attempt. After an
accepted submission, the operation is observed as `ACCEPTED`, `RUNNING`, or a
terminal outcome. A later accepted submission following `NOT_ACCEPTED` is an
accepted submission of the same governed operation, not a transition from a
terminal operation outcome back to execution.

`ACCEPTED` means the executor has durably admitted the operation; `RUNNING`
means execution has begun. An operation may be observed as `ACCEPTED` and
then `RUNNING`, or may reach a terminal outcome without a separately observed
`RUNNING` state. Once terminal, its outcome is immutable. `UNKNOWN` is an
uncertain lookup observation, not a transition that overwrites an established
operation state or outcome.

Operation lookup/outcome uses these boundary observations and outcomes:

| Observation/outcome | Meaning | Fidenaut action |
| --- | --- | --- |
| `ACCEPTED` / `RUNNING` | The identified operation is durably accepted or executing. | Query the same operation. Do not dispatch a new operation for the same work. |
| `CANCEL_REQUESTED` | An optional cancellation request was accepted, but the operation has not yet reached a confirmed terminal state. | Continue querying the same operation; do not assume it stopped or dispatch replacement work. |
| `SUCCESS` | The identified operation reached a terminal successful outcome and its result/artifact manifest, including an explicit completeness assertion for repository changes, is available. | Reject absent or incomplete artifact retrieval; independently validate identity, manifest binding, bytes, scope, and required workflow evidence before accepting any work. |
| `AGENT_FAILURE` | The executor established that agent/tool work ended unsuccessfully and can account for consequential execution and side effects. | Preserve the failure evidence. Do not infer that resubmission is safe; only Fidenaut governance may authorize a distinct new operation. |
| `PROVIDER_FAILURE` | The executor established a terminal provider/model failure and can account for consequential execution and side effects. | Preserve the outcome. It does not make retry safe; only Fidenaut governance may authorize a distinct new operation. |
| `EXECUTOR_FAILURE` | The executor established a terminal failure of its own runtime and can account for consequential execution and side effects. | Preserve the outcome. It does not make retry safe; only Fidenaut governance may authorize a distinct new operation. If terminality or effects are uncertain, use `UNKNOWN`. |
| `CANCELLED` | The executor established that this operation stopped due to cancellation. | Do not accept partial work as successful. Resolve the workflow through its governed path. |
| `UNKNOWN` | The current observation cannot establish acceptance, completion, relevant side effects, or final outcome. It is not an immutable terminal outcome. | Fail closed. Query/reconcile the same operation only; do not treat it as failed, canceled, or safe to rerun. |

**Terminal failure and side-effect rule:** `AGENT_FAILURE`,
`PROVIDER_FAILURE`, `EXECUTOR_FAILURE`, or `CANCELLED` is terminal only when
the executor has sufficient evidence to make that outcome claim without
unresolved uncertainty about execution that could materially affect
repository/workspace state, external systems, credentials/secrets or other
externally visible state, durable agent/tool side effects, or any other effect
relevant to Fidenaut's governed operation. If the executor cannot establish
whether relevant consequential effects occurred, the current observation
remains `UNKNOWN`, even if a provider or runtime reported an error. A
definitive terminal result may include known partial effects/artifacts; those
remain subject to Fidenaut's independent artifact and repository checks.

`PROVIDER_FAILURE` and `EXECUTOR_FAILURE` do not imply safe retry. Executor or
provider retryability hints are advisory and never authorize a new Fidenaut
operation. Only Fidenaut governance determines whether and when a distinct
new operation may be authorized. A request timeout, lost response, server
restart, provider disconnect, or cancellation request is not itself a
terminal outcome. A timeout may be classified as `PROVIDER_FAILURE` only
when the executor can establish the terminal outcome and account for
consequential effects; otherwise it is `UNKNOWN`. Separate transient and
permanent provider classes are not required: neither class proves a repeat is
safe.

For cancellation, a response that merely acknowledges receipt changes the
operation to `CANCEL_REQUESTED`, not `CANCELLED`. The executor may instead
report that the operation already completed; Fidenaut then handles that
actual terminal result. If the cancellation race cannot be resolved, the
operation remains `UNKNOWN` and Fidenaut cannot conclude that execution has
stopped.

### Lost acknowledgement and retry rules

When the initiating response is lost, Fidenaut looks up the recorded operation
ID. It accepts the executor's result only if it is bound to the same
fingerprint, context, and operation. If the result is `ACCEPTED` or `RUNNING`,
Fidenaut continues querying that operation. A definitive `NOT_ACCEPTED`
submission disposition permits redelivery of the identical request with the
same ID under the idempotent submission semantics; it is not a terminal
operation outcome. If lookup currently returns `UNKNOWN`, Fidenaut may query
the same operation again; later authoritative evidence may resolve that
observation, but must refer to the same operation identity and must not imply
or trigger a second dispatch. While lookup is `UNKNOWN`, Fidenaut queries or
reconciles that operation and does not redeliver it. A caller may redeliver
the same operation ID and identical fingerprint after a definitive
`NOT_ACCEPTED` disposition only under the idempotent submission semantics:
an already accepted operation is returned, and that later submission may be
accepted without allowing a late or concurrent duplicate execution. This
same-key delivery recovery is not a new operation. Until resolved, Fidenaut
remains blocked: it
must not dispatch a different operation, replace the context, or claim the
old work failed merely to make progress. If it cannot be resolved, `UNKNOWN`
continues to block a replacement operation unless a separate future
governance policy explicitly addresses persistent `UNKNOWN`.

If lookup establishes a terminal outcome, Fidenaut processes that outcome
without rerunning the operation.

A safe *new* operation is distinct from recovery of an old one:

- **Same operation recovery:** reuse the original operation ID and fingerprint
  to retrieve the existing result; this must not execute work again.
- **New operation authorization:** Fidenaut issues a new ID only after the
  prior operation is known terminal and existing workflow rules authorize
  another attempt. The new record links to the prior operation and records the
  reason/authorization. The old outcome remains immutable.

For `PROVIDER_FAILURE`, `EXECUTOR_FAILURE`, `AGENT_FAILURE`, or
`CANCELLED`, no automatic repeat is implied by the category. Fidenaut decides
whether and when its workflow permits a new operation under governed policy;
the exact terminal-failure retry policy is intentionally unresolved here. If
the executor cannot prove that a failure or cancellation was terminal and
account for consequential effects, the state is `UNKNOWN`, and only
same-operation reconciliation is allowed.

This is an at-most-once *operation dispatch* contract, not a claim that
arbitrary external side effects can be made transactional or exactly once.
The executor must not replay an operation after an ambiguous failure in a way
that can repeat non-idempotent tool effects. If it cannot establish whether
such effects occurred, it must report `UNKNOWN`; the contract does not require
a particular database, queue, journal, or process supervisor.

The operation ID, request identity, terminal outcome, and evidence needed to
recover or audit that outcome must remain available for the retention period
required by the applicable Fidenaut governance/evidence policy for the
governed run, workflow, or evidence. That retention must support lost-
acknowledgement recovery, `UNKNOWN` reconciliation, and governed
retry/replacement decisions. If Fidenaut has no canonical policy defining
that lifetime, the obligation is inherited from the applicable governance
and evidence policy; defining that policy is outside this executor protocol.
No fixed duration or storage mechanism is prescribed.

## Context identity, role assignment, and isolation

These are separate facts:

1. **Context identity:** an opaque executor reference that resolves to a
   runtime execution context. Equality/inequality of ID strings alone does
   not prove that histories, tools, or underlying state are separate.
2. **Role assignment:** Fidenaut's binding from a governed workflow execution
   and role to a context ID. Role labels and executor metadata do not assign
   roles by themselves.
3. **Lineage:** Fidenaut records why and under what authorized recovery path a
   context was created or replaced. Lineage does not prove isolation or
   authorship.
4. **Isolation:** a property of the executor/host execution boundary, not of
   IDs, prompts, or role names.

For the #60 role model, planner, reviewer, test implementer, and implementer
must resolve to distinct underlying execution instances for the governed
workflow execution. Same-role revision work resumes its original context.
The reviewer receives only the canonical artifacts/evidence authorized for
review; role-context separation does not create a second canonical artifact
store or remove permitted access to shared workflow evidence.

The executor/host must prevent one role context from using conversation
history, workspace paths, tools, process state, environment, credentials, or
network-visible shared state belonging to another role except through
Fidenaut-authorized shared inputs and services. Network access need not be
globally disabled, but any shared service or mutable resource must respect the
same role boundary. The execution boundary must declare which isolation
mechanism and scope it provides. Goose's client-mediated terminal escaped the
test workspaces, and OpenHands local terminal tools read peer workspace files
and shared server environment; separate session IDs/workspace names did not
prevent those accesses. Goose's client did deny cross-workspace filesystem
requests, but that was the experimental client's path allowlist, not a Goose
guarantee.

To qualify a context for role separation, Fidenaut needs deployment-specific
evidence identifying the isolation boundary and its scope, plus a conformance
test showing that independently created contexts cannot read or mutate each
other's history, workspace, tool-visible state, processes, environment,
credentials, or relevant network-visible mutable state. The test must identify
which properties are enforced by the executor and which are supplied by its
host; shared services and explicitly shared inputs must be enumerated. This
is qualification evidence, not a universal sandbox standard or a cryptographic
proof. Fidenaut must not treat a context ID, role label, or executor assertion
alone as isolation evidence.

`resume context` means continuing the same underlying execution context,
including its context-owned execution history/state, workspace/filesystem
state relevant to execution, effective tools and permissions,
environment/configuration, and credentials/secrets available to that context.
It does not mean creating a replacement and reusing an identifier. A material
change to any governance-relevant property in that set must not be silently
represented as an exact resume. If the executor cannot establish continuity
of those properties, it must report the context as unavailable/lost rather
than present a materially different context as the original. This is a
semantic guarantee and does not prescribe how the executor persists or
restores context state. Fidenaut records the binding and treats
executor-reported identity/continuity as evidence subject to qualification,
not as proof of actor identity.

Fidenaut can independently check that it assigned distinct IDs, routes work
to the recorded role binding, resumes the expected ID, uses authorized
canonical inputs, and rejects a reported collision. It cannot independently
inspect hidden executor memory or prove that opaque IDs map to separate
underlying histories, processes, credentials, or network namespaces. Those
properties remain executor/host guarantees that Fidenaut must explicitly
trust and qualify through contract evidence and deployment-specific
conformance checks. This proposal does not add authenticated or cryptographic
actor identity, nor claim that distinct contexts prove independently
controlled actors.

### Replacement and lineage

An executor fork is not a minimum contract operation. OpenHands local fork
copied the conversation and reused its workspace; Docker fork was explicitly
blocked in the tested source revision. Goose ACP fork created a new session
and copied history/workspace, but the experiment's persisted session row did
not record its parent. Fork behavior therefore cannot be treated as a fresh,
isolated reviewer context or as sufficient lineage evidence.

When the existing governed replacement path authorizes replacing an
unavailable role context, Fidenaut creates a fresh context and records the
new binding, old binding, role, workflow execution, reason, and authorization.
The new ID is never represented as a resume of the old ID. Whether permitted
state is reconstructed is an explicit Fidenaut recovery decision; it is not
an implicit executor fork.

## Artifact and repository boundary

The executor returns an artifact manifest associated with the operation ID
and its immutable input/base identity. The manifest must explicitly establish
whether it is a complete account of the operation's repository changes for
that exact repository and base revision. The completeness assertion and
manifest boundary must let Fidenaut distinguish a complete result with no
repository changes from a complete result containing changes, and from an
incomplete or unavailable retrieval. An empty manifest means "complete and no
changes" only when completeness is explicitly established; no returned
artifacts alone is not evidence of a successful no-change result. The
transport may be files, a patch, a content-addressed reference, or another
format; no one format is selected here. The boundary must preserve the
information needed to reconstruct and inspect:

- file additions and modifications, with actual bytes and path;
- deletions, represented explicitly rather than omitted;
- executable mode changes and symlink type/target;
- a patch/diff when supplied, treated as a transfer representation rather
  than proof of repository state;
- reports and other evidence as actual retrievable bytes;
- repository identity and base revision, plus enough resulting tree/change
  information to reproduce the proposed state.

The transfer must unambiguously associate completeness, each path, and the
result with the exact operation, repository identity, and base revision;
encode paths relative to the governed repository without ambiguous
normalization; and represent additions, replacements, deletions, file type,
mode, and symlink target without loss. It must provide all bytes required to
reconstruct the proposed result, not merely changed-path names or a narrative
diff. An incomplete or unavailable transfer is not an empty manifest and
cannot be accepted as a successful no-change result. For non-Git inputs or
reports, it must identify the exact input/base bytes and return retrievable
output bytes with operation binding. Executor-reported hashes may be retained
as claims, but Fidenaut re-obtains the authoritative bytes, computes its own
hashes, and verifies the reconstructed result; a reported hash is never
accepted as a substitute.

Fidenaut distinguishes three layers:

| Layer | Meaning | Governance treatment |
| --- | --- | --- |
| **Executor claim** | Narrative, status, reported changed paths, reported hashes, or a statement that work/tests succeeded. | Untrusted description; not accepted as artifact or repository proof. |
| **Executor-provided bytes** | Bytes or patch actually retrieved from the executor/client boundary, associated with the operation and base, with an explicit completeness assertion for the repository-change manifest. | Input to Fidenaut's verification; preserve the received bytes, completeness assertion, and metadata. The assertion is executor-attested. |
| **Fidenaut-verified result** | Fidenaut independently hashes and stores the received bytes, validates the manifest's operation/repository/base binding and completeness declaration, reconstructs/applies changes in its verification checkout, checks the base, diff, paths, modes, symlinks and repository state, and runs required validation. | Only this result can become canonical governed artifact/evidence or candidate repository state. Completeness of the executor's account remains an executor/host claim that Fidenaut must qualify and decide whether to rely on; missing or incomplete declarations cannot be treated as no changes. |

The OpenHands experiment retrieved a base-bound git-delta archive that
preserved additions, edits, deletions, executable mode, and symlink changes;
independent application and verification succeeded, although its archive
route required careful workspace/path binding. Goose's ACP filesystem write
request exposed actual bytes that the harness copied into a separate checkout
and independently hashed, but it did not provide an artifact package/export
API or demonstrate deletions and filesystem metadata. These results support
byte retrieval with an adapter, not trust in executor descriptions.

Fidenaut continues to own canonical artifact identity and provenance, scope
checks, Git ancestry/topology checks, and governed validation. Executor
workspaces are execution state, not alternate canonical artifact stores.

## Trust boundary

| Property | Executor provides | Fidenaut independently verifies |
| --- | --- | --- |
| Context identity | Opaque ID and a stable resume mapping; assertion that each maps to the claimed underlying context. | The ID selected/bound by Fidenaut, non-collision of recorded IDs, and correct routing of each request. It cannot inspect hidden state to prove the mapping. |
| Operation identity | Durable lookup by Fidenaut's operation ID, immutable request binding, and duplicate-dispatch prevention. | Its own operation ID, fingerprint, pending intent, and that returned IDs/fingerprint match. It cannot prove hidden executor work did not occur absent trustworthy operation records. |
| Lineage | Reports executor-known parentage where available. | Records create/replace decisions and authorization in Fidenaut's own lineage. It does not rely on fork-reported ancestry as proof of independent state. |
| Execution outcome | A normalized terminal state and diagnostics, or `UNKNOWN`; executor attestation of what happened. | That the outcome corresponds to the pending operation/context/request. Fidenaut cannot independently reconstruct provider/runtime events that occurred inside the executor. |
| Artifacts | Retrievable bytes/patch and an explicit completeness assertion for the repository-change manifest, bound to the operation and exact repository/base. | Actual received bytes, digest/length, canonical storage, reconstruction, and whether the asserted manifest binding matches the request. Completeness of the executor's account remains an attested property to qualify. |
| Repository changes | Proposed base and repository delta/tree reference. | Base identity, actual diff/tree, path/scope, ancestry, topology, and required validation in a Fidenaut-controlled checkout. |
| Role assignment | Executes the context ID presented for a request; role metadata may be echoed. | Fidenaut's role-to-context binding, allowed handoff, canonical inputs, and approval state. The executor role label is not authority. |
| Isolation | Guarantees/configuration for history, workspace/tools, process, environment/credentials, and shared network resources. | Declared configuration, context-ID relationships, and deployment conformance probes where feasible. Fidenaut cannot prove absence of hidden shared state from opaque IDs alone. |
| Provider/runtime status | Normalized provider, agent, executor, cancellation, and uncertain outcomes with diagnostics. | That a claimed result is tied to the requested operation and that ambiguous results fail closed. The underlying cause classification remains executor-attested. |

No row introduces authentication or cryptographic actor identity. In
particular, `SUCCESS` is an executor-attested execution result; it does not
make the produced artifact valid until Fidenaut verifies the artifact and
workflow conditions independently.

## Compatibility with existing evidence

Statuses below use `satisfied`, `partially satisfied`, `not satisfied`, and
`unknown` against the proposed contract. They summarize only the experiments
linked above, not general product capability.

| Requirement | OpenHands | Goose |
| --- | --- | --- |
| Create and resume a context | **Satisfied** — conversation create/load/resume and persistence were exercised in local mode. | **Satisfied** — ACP `session/new`/`session/load` exercised; transcripts persisted. |
| Fresh role-context isolation | **Not satisfied** in tested local mode: cross-context workspace reads and shared server environment observed. Docker isolation is **unknown** because the pinned image could not be downloaded. | **Not satisfied** in tested ACP/client setup: terminal read, modify, and delete crossed workspaces; another context observed a peer process and shared local network endpoint. The ACP filesystem read tool's denial came from the test client's allowlist. |
| Durable operation identity and idempotent recovery | **Not satisfied** — conversation/event lookup recovered the one-run result, but no per-run operation ID/idempotency key was exposed; duplicate retry was not exercised. | **Not satisfied** — active-run UUID was temporary; no fetch-by-operation result path was demonstrated. Repeating the prompt after disconnect changed the side-effect count from one to two. |
| Terminal failure classification, including unknown | **Partially satisfied** — structured provider/agent errors and retry hints existed, but runtime crash after restart was indistinguishable from paused/cancelled state. Retryability did not prove side-effect safety. | **Partially satisfied** — tool failure and cancellation were observable, but provider failure was a human-readable message, timeout appeared as completed, and runtime death yielded connection closure plus an incomplete session. |
| Cancellation | **Partially satisfied** — interrupt returned and paused the conversation, but paused state was also observed after runtime crash. | **Partially satisfied** — ACP cancellation returned `stopReason=cancelled`; uncertain runtime shutdown was not a classified terminal result. |
| Context lineage for replacement/fork | **Partially satisfied** — local fork reported a parent, but copied history/workspace; Docker fork returned HTTP 501. | **Not satisfied** for durable lineage — fork returned a new ID and copied state, but the experiment's persisted child row had no parent ID. |
| Actual artifact transfer and independent verification | **Partially satisfied** — the patch/archive carried tested repository change types and was independently applied/verified; path-to-conversation binding remains adapter work. | **Partially satisfied** — actual filesystem request bytes were captured and re-hashed in a separate checkout; deletion/mode/symlink and general export semantics were not established. |
| Provider-neutral operation contract | **Unknown** — the experiment used a local OpenAI-compatible mock only. | **Unknown** — the experiment used one OpenAI-compatible mock only. |

Neither experiment establishes that these systems, as configured, satisfy
the full minimum contract. This table identifies adapter/executor
requirements; it does not rank or select an executor.

## Deliberately excluded

The minimum contract does **not** require:

- executor-side role policy or approval; these are Fidenaut governance;
- an executor fork API; Fidenaut can create a fresh replacement and record
  lineage itself;
- per-provider retry algorithms or treating a `retryable` hint as permission;
- a particular queue, database, journal format, event bus, IPC mechanism,
  process supervisor, worker manager, or runtime API;
- a new workflow engine or policy/authorization service;
- cryptographic actor identity or proof of independent human control;
- a particular artifact wire format or an executor-owned canonical store;
- a mandatory cancellation capability where the executor can safely expose
  and retain operation state without it.

These exclusions keep operation recovery, context execution, and artifact
transfer at the boundary without recreating Fidenaut's current managed
runtime. They do not waive the operation identity, outcome, isolation, or
artifact guarantees above.

## Open design decisions before adapter authorization

1. **Isolation assurance:** what deployment evidence/conformance test is
   sufficient for Fidenaut to rely on an executor's guarantees for context
   history, tool/workspace access, processes, credentials, and network state?
   The experiments show that session IDs and separate workspace names alone
   are not sufficient; Fidenaut cannot inspect a remote executor's hidden
   state independently.
2. **Unknown-operation disposition:** which explicit human/governed path, if
   any, may supersede an operation that remains `UNKNOWN`, and what evidence
   is required before new work can proceed without claiming the old operation
   failed or was canceled? Until decided, the safe behavior is to remain
   blocked and reconcile the same operation.
3. **Terminal failure policy:** which known `PROVIDER_FAILURE` or
   `EXECUTOR_FAILURE` outcomes may authorize a new operation at each role
   gate, and how do existing human approvals and revision limits constrain
   that authorization? Executor retryability alone is insufficient.
4. **Artifact base and retrieval binding:** what minimum repository identity
   and base-revision evidence binds a retrieved patch/manifest to the exact
   operation and governed workspace, including non-Git reports? Fidenaut
   must reject changes it cannot independently reconstruct and verify.

These are design questions, not implementation work. Until they are resolved
and separately approved, the existing managed runtime remains active and no
executor adapter is authorized.
