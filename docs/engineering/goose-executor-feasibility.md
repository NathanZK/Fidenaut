# Goose/ACP external-executor feasibility

> **Feasibility/design only.** This is neither a selection of Goose as
> Fidenaut's executor nor authorization to implement an adapter, integration,
> or workflow change. The normative requirements are in
> [external-executor-contract.md](external-executor-contract.md); the broader
> governance split is in
> [governance-execution-boundary.md](governance-execution-boundary.md).
> This study does not amend either document.

## Evidence and scope

The concrete case is `aaif-goose/goose` at
[`b92a80daf4a77d7e854709965bdfdc489c0472d2`](https://github.com/aaif-goose/goose/tree/b92a80daf4a77d7e854709965bdfdc489c0472d2),
package `1.52.0`. The [pinned source review on #72](https://github.com/NathanZK/Fidenaut/issues/72#issuecomment-5893332099)
and [ACP experiment on #72](https://github.com/NathanZK/Fidenaut/issues/72#issuecomment-5895570243)
are the evidence, not claims about other revisions or deployments. The
experiment used `@agentclientprotocol/sdk@1.5.1`, ACP protocol version `1`,
Node `v26.0.0`, native macOS processes, and a mock provider. The
[OpenHands findings on #74](https://github.com/NathanZK/Fidenaut/issues/74)
are comparative context, not evidence that a Goose host works.

The #60 distinct-resumable-context, #63 same-operation recovery, #65 bounded
revision/human decision, and #69 identity-verified no-change reconciliation
requirements remain governance constraints. Their current Python worker,
socket, receipt, and history implementations are **not** the starting point
for this design.

## Contract-to-Goose capability matrix

Each row classifies the **whole stated requirement** into exactly one
category: **1 Direct Goose/ACP**, **2 Thin adapter**, **3 Host/runtime
infrastructure**, **4 Not provided by Goose under the tested architecture**,
or **5 Unresolved governance policy**. Category 4 describes the tested ACP
surface, not a permanent impossibility or an executor selection. A directly
available primitive in a category 3 or 4 row does not satisfy the whole row.

| Contract requirement | Category | Boundary evidence and implication |
| --- | --- | --- |
| Execute a prompt in an existing session and stream events | **1 Direct Goose/ACP** | `session/prompt` executes work; the temporary `activeRunId` is live-run metadata, not a governed operation ID. |
| Persist and reload a session transcript | **1 Direct Goose/ACP** | `session/new` and `session/load` persisted and replayed conversation in the experiment; this alone says nothing about tools, sandbox state, or in-flight work. |
| Bind a fresh context creation key to exactly one created context after a lost response | **3 Host/runtime infrastructure** | ACP `session/new` creates an ID but has no caller creation-idempotency key. A durable host must control creation, retain the key-to-ID association, and fail closed if creation occurred but its ID cannot be recovered. |
| Map Fidenaut's governed role/context reference to a Goose session/host instance | **2 Thin adapter** | Fidenaut owns role authorization and replacement lineage; the facade only maps references. Do not use `session/fork` for fresh reviewer contexts: tested fork copied history and cwd, and did not persist parentage. |
| Resume the *exact* context, including history, workspace, tools, permissions, configuration, and credentials | **3 Host/runtime infrastructure** | ACP reload supplies transcript/configuration, not evidence that the runtime, tool host, workspace, or credentials are unchanged. A host must preserve/qualify these properties or report the context lost. |
| Provide isolated fresh contexts for all four roles | **3 Host/runtime infrastructure** | Tested ACP sessions had separate transcripts but unsandboxed terminal access crossed workspaces/processes/network. Client filesystem denial was a harness allowlist; shared server data and provider credentials remain a qualification concern. |
| Bind one Fidenaut `operation_id` and immutable execution request to at most one prompt dispatch | **3 Host/runtime infrastructure** | ACP prompt has no caller operation key or deduplication. A durable dispatch authority would have to reject mismatched fingerprints and gate the one ACP submission; it must never equate JSON-RPC request IDs or `activeRunId` with this key. |
| Retrieve the same operation's terminal result after response loss/restart without executing again | **4 Not provided by Goose under the tested architecture** | `session/load` replays messages, not an operation outcome keyed by Fidenaut ID. The disconnect test recovered no structured outcome; resubmission duplicated a side effect. A host retaining the original response while alive helps, but cannot infer an unrecorded outcome across every crash window. |
| Distinguish `NOT_ACCEPTED` from ambiguous admission | **3 Host/runtime infrastructure** | Only a host that can definitively exclude a late ACP dispatch from that submission attempt may report `NOT_ACCEPTED`. Timeout/connection loss is `UNKNOWN`, never proof of non-acceptance. |
| Normalize durable `ACCEPTED`, `RUNNING`, terminal results, and `UNKNOWN` | **3 Host/runtime infrastructure** | A host can record admission, start, and observed completion; on ambiguous server/client failure it must retain `UNKNOWN` rather than synthesize a terminal result. Whether it can recover *all* accepted outcomes is a separate category 4 gap above. |
| Expose reliable provider/agent/executor-failure distinctions with consequential effects accounted for | **4 Not provided by Goose under the tested architecture** | ACP returned `end_turn` for a provider 503 and for a prompt containing a failed tool; a shell timeout appeared completed. Some tool errors are typed, but no complete machine-readable operation classification or side-effect certainty was established. A host can classify its own failures and otherwise use `UNKNOWN`, not reconstruct hidden provider facts. |
| Request cancellation of an active session run | **1 Direct Goose/ACP** | `session/cancel` was exercised and the prompt eventually returned `cancelled`; the cancel request itself is not a durable final-outcome acknowledgement. Cancellation is optional in the contract. |
| Establish a durable, side-effect-accounted final `CANCELLED` outcome after races/disconnect | **3 Host/runtime infrastructure** | The host needs a durable request/result association and evidence that execution stopped; ambiguous or lost acknowledgement remains `UNKNOWN`. |
| Capture complete operation/base-bound repository changes and actual reports, including no-change | **3 Host/runtime infrastructure** | ACP filesystem writes exposed verifiable bytes for one added text file. It did not establish all changes, deletions, modes, symlinks, binaries, or reports; a host must inspect and freeze the whole resulting workspace independent of Goose's narrative. |
| Convey host-produced artifact bytes/metadata for Fidenaut to verify | **2 Thin adapter** | A transport bridge can expose retrievable bytes/metadata; Fidenaut re-obtains, hashes, reconstructs, validates, and accepts them. The adapter cannot declare governance success. |
| Determine acceptable isolation qualification evidence | **5 Unresolved governance policy** | The contract requires evidence but leaves Fidenaut's sufficiency threshold open. |
| Determine a disposition for persistent `UNKNOWN` | **5 Unresolved governance policy** | No human override or automatic replacement is authorized by this feasibility study. |
| Determine which terminal failures allow a new operation | **5 Unresolved governance policy** | Executor classifications and retry hints do not authorize another operation. |
| Determine exact artifact base/retrieval mechanism | **5 Unresolved governance policy** | The completeness and independent-verification requirements are fixed, but the base evidence and retrieval binding are not. |

**Classification boundary:** A host capable of durable operation ownership
could supply some category 4 capabilities only by adding a trusted execution
protocol around Goose, obtaining stronger Goose hooks, or using external
durable execution infrastructure. Merely correlating ACP transcript lines
does not turn these into category 2 adapter features.

## Proposed minimal boundary

```text
Fidenaut governance
  role binding, lineage, approvals, new-operation authorization,
  canonical evidence, repository/scope validation
        |
        | governed context/operation IDs, immutable request, canonical inputs
        v
Adapter facade (mapping and normalization only)
  map IDs to qualified Goose host/session; expose contract responses;
  forward retrievable outcome/artifact references without accepting them
        |
        v
Execution host (conditional, not present in tested Goose/ACP)
  isolate and retain per-role execution environments;
  own admission/dispatch/outcome identity and retention;
  mediate all tool paths; capture complete base-bound workspace outputs
        |
        v
Goose ACP: session/new, session/load, session/prompt, session/cancel
        |
        v
Underlying provider, tools, sandboxed filesystem/process/network
```

The facade belongs **outside Fidenaut** because Fidenaut should not speak
provider/session-specific ACP or manage execution resources. The host
belongs **outside Fidenaut** because context isolation, terminal execution,
dispatch, and effect capture are executor responsibilities; making the
workflow controller own them recreates its managed runtime. They cannot
simply be assigned to **Goose as tested**, whose ACP surface supplies none
of the qualified isolation or durable operation recovery guarantees. Goose
does retain its agent reasoning, transcript and provider interaction. A
host/client can enforce access to client-mediated filesystem and terminal
tools, but must also control or disable any Goose-side filesystem/shell
fallback or other extension path capable of bypassing the same boundary.

The Fidenaut-assigned operation ID, request fingerprint, authorized role,
canonical inputs, base, effective tools/configuration, and output purpose
must travel together. A session ID is only the context reference; a prompt
request ID is a transport correlation; a Goose `activeRunId` is temporary
runtime metadata. None replaces the governed operation key.

### Admission, lost acknowledgement, and restart

A conforming dispatch authority would retain an immutable ID/fingerprint
association before delivering an ACP prompt, refuse conflicting reuse, and
return the recorded disposition/outcome for a repeated key without issuing
another prompt. An accepted operation means durable admission; `RUNNING`
means the prompt actually began. A definitive `NOT_ACCEPTED` requires proof
that this submission attempt cannot later execute. A lost Fidenaut response
must be resolved by lookup, not by resending `session/prompt`.

This exposes the hard crash window: after the host hands `session/prompt`
to Goose but before it durably records Goose's completion, it may not know
whether a tool changed state. Restarting the host or Goose and replaying the
prompt can duplicate effects (the experiment observed count `1` then `2`).
The conservative result is `UNKNOWN`; transcript replay, an in-memory
`activeRunId`, or a one-shot completion message is not a recoverable final
outcome. A live host that durably captures the original response can return
it after *Fidenaut* disconnects; that narrower case must not be confused
with safe recovery after host/Goose death. A host that only writes a journal
but cannot resolve the handoff race has not met full same-operation recovery.
At-most-once *dispatch* does not imply exactly-once external side effects.
No blind retry, second prompt, automatic context replacement, or new
Fidenaut operation follows `UNKNOWN`.

### Exact contexts and role isolation

For planner, reviewer, test implementer, and implementer, Fidenaut allocates
distinct bindings and records fresh/replacement lineage. The execution host
must associate each binding with its own qualified Goose context, workspace,
tool boundary, process/environment, credential scope, and relevant shared
network policy. `session/load` can restore conversation but cannot alone
prove continuity of those other properties; if one is materially lost or
changed, the host reports the context unavailable, not a successful resume.
Provider defaults and server-scoped session visibility/credentials make a
shared `goose serve` process especially hard to qualify; separate server/data
scopes might help, but were **not** tested as a solution. Sharing canonical
review inputs is allowed only through Fidenaut-authorized channels.

Qualification evidence would identify the executor/host boundary, server
and data roots, effective tool and extension capabilities, workspace/mount
mapping, process and credential scope, network/shared-service policy, and
the context-to-host mapping. Conformance probes must try cross-context
history, file read/write/delete, shell/process, environment/credential
markers, and relevant network-visible mutable state in both directions,
including after reload and replacement. The host supplies configuration
and empirical observations; Fidenaut decides whether that evidence is
sufficient. Neither ACP IDs nor the probes prove absolute non-interference
or cryptographic actor identity. The exact qualification policy remains
open.

### Artifacts and failure/cancellation reporting

The host would start from an identified disposable repository/base, observe
the actual *resulting* workspace rather than rely only on ACP file-write
requests, and provide an operation/base-bound completeness assertion. A
complete manifest must cover additions and modifications with bytes,
deletions, file types, executable modes, symlink targets, and reports with
actual retrievable bytes. A complete empty repository-change manifest is
an affirmative no-change claim; an absent/incomplete transfer is not.
Terminal/shell/Goose-side writes may bypass client filesystem requests, so
capturing just those requests is insufficient. Fidenaut independently
obtains and hashes the bytes, reconstructs changes in its verification
checkout, and checks scope and evidence. This is a host workspace coupling
with a subsequent controlled transfer, **not** Goose-native detached artifact
export; the exact base/retrieval binding is unresolved.

The safe external status mapping is conservative:

| Contract observation | Goose/ACP evidence needed; otherwise |
| --- | --- |
| `NOT_ACCEPTED` | Host proves that a particular submission attempt was never handed off and cannot execute later; otherwise `UNKNOWN`. Not a terminal operation result. |
| `ACCEPTED` / `RUNNING` | Host has durably admitted the key / has evidence the matching ACP prompt began. A bare RPC acknowledgement does not prove a completed run. |
| `SUCCESS` | Matching prompt completion **and** sufficient terminal evidence and complete, base-bound artifact transfer; `end_turn` alone (including an error message) is insufficient. |
| `AGENT_FAILURE` | Matching operation ended unsuccessfully with known consequential effects; a failed tool update alone does not prove the overall operation failed. |
| `PROVIDER_FAILURE` | Machine-readable provider cause and known terminal effect state; the tested 503 became human-readable text plus `end_turn`, so classify conservatively rather than parse prose. |
| `EXECUTOR_FAILURE` | Host knows the matched operation is terminal and can account for consequential effects; loss of Goose/host or connection alone is `UNKNOWN`. |
| `CANCELLED` | Matched final cancellation outcome plus known effect state; `session/cancel` acknowledgement alone is `CANCEL_REQUESTED`, not final. |
| `UNKNOWN` | No sufficient admission, completion, cause, or consequential-effect evidence; retain and query the *same* operation without redelivery or replacement. |

The tested ACP terminal timeout emitted `terminal/kill` yet appeared
completed, so neither `stopReason=end_turn` nor tool status alone establishes
success or retryability. If optional cancellation is supported, the facade
can forward `session/cancel` and preserve a separate request acknowledgement.
On a lost cancellation acknowledgement it queries the original operation;
it cannot infer `CANCELLED` or safe replacement. A completed operation wins
the cancellation race if supported by durable evidence. The host cannot
recover a typed provider error from unstructured text; either a stronger
executor surface supplies it or the external outcome stays `UNKNOWN` when
the required distinction matters. Fidenaut alone authorizes any distinct
new operation.

## Capabilities retained by Fidenaut

Fidenaut creates the governed operation ID and immutable request identity,
selects and binds roles to contexts, authorizes explicit replacements and
new operations, and records lineage. It applies #65's revision limit and
human decision/approval gates, and #69's evidence-based reconciliation
constraints; a host no-change assertion is not itself permission to clear
pending work. Fidenaut fetches and hashes authoritative artifact bytes,
validates repository base, changes and approved scope, and accepts evidence
and workflow transitions. These checks stay in Fidenaut even if a host
supplies stronger runtime records. The host may attest execution facts but
cannot authorize their governance consequences.

## Adapter/host size and missing capabilities

| Current-runtime-like responsibility | Small facade or substantial host? | Feasibility against tested Goose |
| --- | --- | --- |
| Role/session ID mapping and Fidenaut-owned replacement lineage | Small facade | Straightforward mapping; never fork to establish independent roles. |
| Durable operation identity, immutable request and outcome records | Substantial host | Missing natively; a durable ledger is necessary, but by itself cannot resolve the ACP dispatch/crash gap. |
| Reliable prompt dispatch, no-duplicate guarantee, restart/response-loss recovery | Substantial host | The decisive gap: requires one authoritative dispatch owner and recoverable final evidence, not a wrapper around `session/prompt`. |
| Process/runtime supervision and context lifecycle | Substantial host if it must isolate/govern Goose processes | Goose runs the agent, but the host still has to keep qualified server/tool environments alive and map exact resumes. |
| IPC/client-mediated tool service | Substantial host where ACP tools are used | ACP supplies requests; a host must enforce all access paths and prevent local fallback from bypassing isolation. No particular IPC implementation is required. |
| Isolation and environment/credential/network scoping | Substantial host | Failed in the unsandboxed experiment; not a Goose session guarantee. |
| Complete repository/artifact capture and retention | Substantial host plus small transfer facade | One ACP file write was verifiable, not a comprehensive operation-bound export; reports and no-change need explicit evidence. |
| Provider failure normalization | Limited facade for clear tool/cancel evidence; otherwise stronger executor/host support | Tested ACP loses typed provider/error distinctions; a generic host cannot safely infer them from assistant text. |
| Workflow/revision limits, approval, scope and canonical artifact acceptance | Fidenaut, not adapter | #60/#63/#65/#69 governance meaning remains Fidenaut-owned. |

**Thin-adapter verdict:** Under the **tested Goose/ACP architecture**, a
meaningfully thin conforming adapter is **not established**. The missing
durable dispatch/outcome path plus isolation and comprehensive workspace
capture require a host that resembles a second execution engine in
operation ownership, recovery, and runtime containment, even though Goose
would still provide the agent, provider abstraction, and transcript engine.
Calling that host a "thin adapter" would hide the largest work. Conversely,
delegation could still be worthwhile if an existing qualified host supplies
those responsibilities (or a stronger Goose execution surface supplies
durable keyed outcomes) without Fidenaut rebuilding them; neither condition
was demonstrated. This is a feasibility finding, not a rejection or selection
of Goose and not a proposal to implement a new runtime.

## Unresolved questions

1. What deployment-specific isolation evidence is sufficient for Fidenaut
   to qualify a Goose ACP client, server, tool fallback, and underlying host?
2. What governed/human disposition, if any, can resolve persistently
   `UNKNOWN` work without pretending it was not executed?
3. Which known terminal failures, if any, may authorize a *new* governed
   operation, subject to approval and revision limits?
4. What exact repository/base and retrieval evidence binds a complete
   workspace result and non-Git reports to the operation without trusting
   Goose's narrative?

For feasibility, the additional technical unknown is whether a deployable
host or stronger Goose surface can actually close the handoff/crash window
and provide typed terminal outcomes without simply recreating the managed
runtime. No experiment cited here demonstrates that it can.
