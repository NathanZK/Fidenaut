# Governance / execution separation — architecture and deletion plan

status: architecture proposal. It does not authorize implementation, select an executor or provider, or change workflow behavior.
basis:
- Fidenaut `main` @ `432cc34`, the code audited here.
- #76 report and its credentialed continuity addendum.
- `docs/engineering/governance-execution-boundary.md` (#73).
- `docs/engineering/external-executor-contract.md`, the "#72 contract", proposed in PR #75 (closed, unmerged).

normative-relationship: the #72 contract stays the normative boundary protocol, and this spec does not weaken it. This spec does five things:
- (a) inventories and deletes current machinery;
- (b) defines the smallest mechanisms that satisfy the #72 contract;
- (c) resolves #72 open decision 4, artifact base binding;
- (d) proposes the qualification gate for #72 open decision 1;
- (e) fixes fail-closed defaults for open decisions 2 and 3, leaving their policies deferred.

```
Fidenaut governance   — "Was this action governed correctly, and may the workflow transition?"
      | #72 contract: create / resume / submit / get-outcome (+ optional cancel)
minimal executor      — "Did the agent run, in which context/workspace, with what outcome?"
      | provider adapter (§6 A)
provider-native role session  (owns conversational continuity)
```

## 0. Driving finding

The current role "execution context" is a long-lived Python worker in its own process group, reached over a Unix socket. It holds **no conversation**. Per `run-role`, it `Popen`s a fresh, caller-supplied argv with stdout and stderr sent to DEVNULL (`WS` `_WORKER`). What persists is a worker's identity and a history file. #76 measured that the provider session survives process exit and restores context. Resumed sessions recalled their codeword 4/4, fresh sessions 0/4, with about 7.6× lower incremental usage. So Fidenaut manufactures a context identity that the provider already supplies, then builds machinery to keep that manufactured identity alive, reachable and reconcilable.

## 1. Inventory and classification

Line numbers refer to `432cc34`. `AW` = `scripts/agent_workflow.py`, `WS` = `scripts/workflow_supervisor.py`.

| # | Component | Where | Class | Reason |
|---|---|---|---|---|
| 1 | Long-lived role worker (`_WORKER`; `establish` spawns it with `start_new_session`) | WS:66-214, 221-263 | OBSOLETE — DELETE | Embodies a "context" that holds no conversation. The provider session replaces it. |
| 2 | Unix-socket IPC (`_request`, worker `send`; endpoint `fidenaut-<id>.sock` under `tempfile.gettempdir()`) | WS:225-227, 389-422 | OBSOLETE — DELETE | Transport to #1 only. The executor is invoked, not addressed. |
| 3 | 2 s controller response timeout, `RuntimeResponseTimeout`, busy vs unavailable split | WS:26, 33, 403-406; AW:309-316, 456-458, 708-713 | OBSOLETE — DELETE | A 2 s IPC wait around work of up to 3,600 s (#63). Busy becomes a lock fact (§6 C1). |
| 4 | Ping as resume | WS:507-510; AW:302-308 | OBSOLETE — DELETE | Liveness of a worker is not continuity of a context. Replaced by V1-V4 (§6). |
| 5 | Cross-role alias pings | AW:298-307 | OBSOLETE — DELETE | A dead non-current role can block the current one (#76 inversion 6). Distinctness becomes a record comparison (G4). |
| 6 | Per-instance history journal (`<id>.history.json`) | WS worker `persist_history` | EXECUTION — REPLACE | Outcome durability is still required, but as one record per op (§4 E-record), not a context journal. |
| 7 | Response-write status and recovery-attempt counters | WS worker, after `send` | OBSOLETE — DELETE | Records whether a socket write succeeded. With idempotent submit and get-outcome, a lost response is harmless. |
| 8 | Runtime receipt (`worker_pid`, `process_group`, `history_length`, `execution_evidence`) | WS worker; AW:459-474 | MIXED — SPLIT | Keep the op/context/outcome binding (§6 Outcome). Delete the worker attestation, which proves nothing about the agent. |
| 9 | Operation-identity registry (`<root>.operation-identities.json`, lock, fsync, dir-fsync) | WS:277-387 | EXECUTION — REPLACE | Merges governance binding (G-record) with executor dedupe (E-record). Each becomes one record in its owner's store. |
| 10 | Request digest (instance, command, cwd, timeout, changed_paths) | WS:37-54, 443-452; AW:381-384 | MIXED — SPLIT | Replaced by `request_fp` over every execution-affecting input (§4, #72 "immutable request"). Instance and argv leave; tool policy, adapter/model config and base revision enter. |
| 11 | `recover` op and recover-then-execute | WS:481-505; AW:436-452 | REPLACE WITH SMALLER PRIMITIVE | Becomes #72 `get_outcome` (never executes; may only raise a delivery fence) plus idempotent `submit`. |
| 12 | `execute` timeout → recover → re-request | WS:454-479 | OBSOLETE — DELETE | Exists only because of #3. |
| 13 | `role_work_pending` map and "another op pending" guard | AW:386-435 | REPLACE WITH SMALLER PRIMITIVE | Becomes the G-record status. The guard becomes G9 (one open op per run). |
| 14 | Stale-pending reconciliation on replacement | AW:584-677 | OBSOLETE — DELETE | Exists because pending state was split between the controller and a worker-owned file (#69). Under R3, replacement never reads another context's records. |
| 15 | Completed nonzero leaves op pending forever (#61) | AW:475-479 | MIXED — SPLIT | Governance gets a terminal `failed` status. The executor supplies the #72 failure class. No new-op policy is added (D2). |
| 16 | `replace-role-context` | AW:580-582, 680-730 | MIXED — SPLIT | Keep: decision, requester, lineage, gate check. Replace: "unavailable" comes from executor `resume` → `unavailable`, not a failed ping. Delete: row 14. |
| 17 | `_role_context` establish-on-first-use and `state.role_contexts` | AW:288-318 | MIXED — SPLIT | Keep the binding table. Establish becomes #72 `create` with a creation key. Pings are deleted. |
| 18 | Opaque caller argv (`--command`), `FIDENAUT_CANONICAL_INPUTS` env, DEVNULL output | AW:335-345; WS worker | EXECUTION — REPLACE | Opaque argv forces a fresh provider process per op and makes failures unclassifiable. Becomes a typed request, with the provider invocation in the adapter. |
| 19 | Caller-supplied `--changed-path` plus worker-side hashing of those paths | AW:370-379, 507-524; WS worker | MIXED — SPLIT | Worker hashing is deleted. Caller-declared paths are replaced by a Fidenaut-computed baseline delta (§4 R5), which is stronger than today. |
| 20 | Admission: gate→role map, revision capacity, `_require_role`, input identity, scope/test split | AW:274-281, 327-369 | GOVERNANCE — RETAIN | Defines who may act, on what, within which scope. |
| 21 | Acceptance: re-read and re-hash output and sources, `role_work` record | AW:480-535 | GOVERNANCE — RETAIN | Only bytes Fidenaut re-hashed count as evidence. The worker-pid checks are deleted (row 8). |
| 22 | `_require_role_work` gate-time revalidation | AW:538-578 | GOVERNANCE — RETAIN | Keys become `context_id` plus `op_id`. |
| 23 | `_role_work_lock` (per-run flock) | AW:260-271 | GOVERNANCE — RETAIN | Held only for admission and acceptance, not across execution (§4 L1). |
| 24 | State revision check and atomic write | AW:766-887 | GOVERNANCE — RETAIN | Governs every transition. |
| 25 | `COPILOT_AGENT_SESSION_ID` observation and `review-session-collision` | AW:982-1040, 6341, 6363, 6639, 6778, 7680, 7899 | MIXED — SPLIT | The rule stays. The source changes from the ambient env of whoever ran the CLI to the bound context's `provider_session_id`. This removes the #76 test-environment sensitivity. |
| 26 | `supervise()` bounded POSIX supervisor | WS:556-1296; AW:1390-1410 | GOVERNANCE — RETAIN (shared utility) | Runs Fidenaut's own git, gh and validation commands (G7). The executor may reuse it to run the provider command (§6 E6). It has no runtime state. |
| 27 | Approval, revision, reopening, target/candidate, PR and completed-run commands | AW:914-11118 | GOVERNANCE — RETAIN | They don't touch the agent runtime. Not re-audited. |
| 28 | `workflow.role_execution_contexts` flag | `.github/agent-workflow.json:7`; AW:284-285 | MIXED — SPLIT | Becomes a mode selector (§7 M). |
| 29 | Provider-specific launch | implicit in the operator's argv | EXECUTION — REPLACE | Moves into the adapter (§6 A). |
| 30 | Workspace binding | implicit `cwd=root` for all roles | UNCERTAIN — NEEDS EVIDENCE | Per-role worktrees versus a shared worktree is the #72 isolation question (§8 U1). Attribution does not depend on it (G9, R5). |

Execution-only state that disappears:
- `runs/issue-N/contexts/<id>.history.json`
- `runs/issue-N/contexts.operation-identities.json` and its lock
- the socket files
- the `state.json` keys:
  - `role_contexts.*.{instance_id, process_group, history_path, endpoint}`
  - `role_work_pending`
  - `role_work_reconciliations`
  - `role_work.*.receipt.{worker_pid, work.process_group, history_length, execution_evidence}`

## 2. Governance boundary (Fidenaut-owned)

```
G1 invariant: workflow state, gate→role map, transitions, revision limits are decided only by Fidenaut; no executor response is a transition.
G2 invariant: human approvals are Fidenaut records; no executor state or outcome (including SUCCESS or a context being available) is approval.
G3 invariant: an artifact is evidence only after Fidenaut re-reads, hashes and stores it; repository changes are evidence only after Fidenaut computes them itself (R5) and passes scope/Git checks.
G4 invariant: role bindings for planner, reviewer, test_implementer, implementer are pairwise distinct in both context_id and provider_session_id. This is necessary, not sufficient: isolation additionally requires the qualification in §8 U1 (#72 "Context identity ... isolation").
G5 invariant: every submit is preceded by a durable G-record with a Fidenaut-minted op_id.
G6 invariant: UNKNOWN fails closed and blocks the run; elapsed time never resolves it.
G7 invariant: validation evidence for transitions comes from Fidenaut-run commands (supervise/run-validation), never executor claims.
G8 invariant: lineage (creation, replacement, requester, reason) is a Fidenaut record; a replacement is never presented as the original.
G9 invariant: at most one G-record per run is non-terminal (reserved|dispatched|unknown) at any time. This preserves today's "another role operation is pending" rule and makes baseline deltas attributable.
```

### Identities (must not be conflated)

| Identity | Minted by | Lifetime | Used for | Never used for |
|---|---|---|---|---|
| `op_id` | Fidenaut | one governed operation. Immutable and never reused. | idempotency key, outcome correlation, acceptance binding | locating or resuming a conversation |
| `delivery_attempt` | Fidenaut | per submit delivery of one op_id (1, 2, …) | fencing stale deliveries (P5) | op identity. It is excluded from `request_fp`. |
| `creation_key` | Fidenaut | one governed context creation | idempotent create (#72) | op identity |
| `context_id` | executor | one role-context generation | separation, lineage, routing | dedupe |
| `provider_session_id` | provider, via executor | the conversation behind one context_id | resume verification, separation | governance decisions on its own |
| `generation` | Fidenaut | per role, starting at 1, +1 per replacement | lineage ordering | — |

```
invariant: op → exactly one context_id; context_id → many ops, sequentially; context_id → exactly one provider_session_id for life.
invariant: resuming never changes op_id; redelivering never changes op_id, context_id or request_fp; a new context requires a new op_id.
```

## 3. Execution boundary (executor-owned)

```
E1 create(creation_key, role, workspace_path) → context; idempotent by creation_key (#72).
E2 resume(context_id) → available | busy | unavailable | unknown; exact context or unavailable, never a substitute (V1).
E3 submit(op_id, delivery_attempt, context_id, request_fp, request) → accepted | not_accepted | conflict; at most one execution per op_id.
E4 get_outcome(op_id, fence_through) → Observation (§6); never executes. If no accepted E-record exists it durably records fence := max(fence, fence_through) and reports NOT_ACCEPTED.
E5 cancel(op_id) (optional, #72) → cancel_requested | already_terminal | unknown; only a terminal CANCELLED establishes cancellation.
E6 run the provider turn in its own process group; publish a terminal outcome only after the group has exited (Q1).
E7 classify terminal failures per #72 (AGENT/PROVIDER/EXECUTOR_FAILURE, CANCELLED) only when effects are accounted for; otherwise UNKNOWN.
E8 mechanical session and workspace verification before and after every turn (V1-V4).
```

What the executor excludes, and why:

| Candidate | Decision | Reason |
|---|---|---|
| Long-lived worker | excluded | The provider session carries continuity (#76). A resident process adds a liveness problem and holds no state. |
| Custom IPC | excluded | Each executor call is a synchronous library/CLI call. |
| Operation journal / event sourcing | excluded | Only the latest state per op is needed (§4 E-record). |
| Durable workflow engine | excluded | Governance already owns the state machine. |
| Containers / sandbox | excluded unless U1 qualification fails | No governance rule requires them if qualification passes. The contract does not change either way. |
| Provider-level retry | excluded | A retry could repeat tool effects inside one op_id and break at-most-once. #61-style failures surface as terminal `PROVIDER_FAILURE` (§6 A3) for governance to decide on. |
| Worktree creation | excluded | Governance chooses and owns the path (R5). |
| Multi-host execution | excluded | Single-host deployment is an invariant (R6). |

## 4. Operation durability (separate from session continuity)

Session continuity answers "does the agent remember?". Operation durability answers "did this governed request run, and what happened?". #76 answered only the first. A response can be lost after the provider ran the turn.

### G-record (Fidenaut; in run `state.json` under `role_ops[op_id]`)

```
data:
  format:            "fidenaut-role-op-v1"
  op_id:             str (uuid4 hex)
  gate:              str            # workflow status at admission
  revision:          int
  role:              str
  context_id:        str
  generation:        int
  request_fp:        sha256 hex     # canonical JSON (sorted keys, ASCII, no whitespace) of the full Request (§6); delivery_attempt is not part of Request
  baseline:          {repo_head: sha, tree_digest: sha256}   # R5; computed at admission
  delivery_attempt:  int ≥ 1
  status:            reserved | dispatched | succeeded | failed | unknown
  observation:       null | Observation (§6), last one received
  accepted_artifact: null | {path, sha256, byte_length}
  accepted_delta:    null | [{path, change: added|modified|deleted|mode|symlink, sha256?, byte_length?}]
  prior_op:          null | op_id   # set only when governance authorizes a new op after a terminal one (D2)
```

### E-record (executor; one file per op: `<executor_root>/ops/<op_id>.json`, plus `<op_id>.lock`)

```
data:
  format:            "fidenaut-executor-op-v1"
  op_id, context_id, request_fp
  fence:             int            # highest delivery_attempt refused as not_accepted (0 if none)
  state:             accepted | running | terminal
  owner:             {pid, start_time}   # executor process running the turn; diagnostics only
  outcome:           null | TerminalOutcome (§6)
```

### State machines

G-record status (initial `reserved`; terminal `succeeded`, `failed`):

| From | Event | To |
|---|---|---|
| reserved | submit returned `accepted` | dispatched |
| reserved | submit returned `not_accepted` | reserved (with delivery_attempt+1) |
| reserved | crash, or no response to submit | reserved; recovery goes through P5 |
| dispatched | get_outcome → SUCCESS and acceptance passes | succeeded |
| dispatched | get_outcome → SUCCESS and acceptance fails | failed (reason `acceptance-rejected`) |
| dispatched | get_outcome → terminal failure or CANCELLED | failed (class recorded) |
| dispatched | get_outcome → ACCEPTED, RUNNING or CANCEL_REQUESTED | dispatched |
| reserved, dispatched | get_outcome → UNKNOWN | unknown |
| unknown | get_outcome → a terminal or live observation | the corresponding row above |
| unknown | anything else | unknown. The only escape is the existing `supersede-run` (D1). |

E-record state (initial: none; terminal: `terminal`):

| From | Event | To |
|---|---|---|
| none | submit, lock acquired, fence < delivery_attempt | accepted |
| none or fenced | submit with delivery_attempt ≤ fence | unchanged → `not_accepted` |
| none or fenced | get_outcome(op_id, fence_through) | fence := max(fence, fence_through) (fsync); observation `NOT_ACCEPTED` |
| accepted | executor begins provider turn | running |
| running | process group exited and effects accounted for | terminal |
| accepted, running | owner lock released without terminal (crash) | unchanged. get_outcome reports `UNKNOWN`. |

Observation mapping from get_outcome:
- none or fenced → `NOT_ACCEPTED`
- accepted with lock held → `ACCEPTED`
- running with lock held → `RUNNING`
- accepted or running with lock free → `UNKNOWN`
- terminal → its outcome

### Protocol

```
L1 lock order: governance lock (_role_work_lock) held for P1 and P4 only; released during submit/turn; P4 re-reads state and checks revision before writing. Executor per-op lock and per-context lock are held by the executor process for the turn's duration (flock: crash-releasing, interprocess).
P1 admit (under L1): governance checks (G1, G3 inputs, scope, G9); compute baseline (R5); write G-record reserved, delivery_attempt=1 (atomic state write).
P2 deliver: submit(op_id, delivery_attempt, …). The executor, atomically under the per-op lock and per-context lock:
     - E-record exists with a different request_fp or context_id → conflict (G-record stays reserved; governance error; nothing runs);
     - E-record accepted/running/terminal with same fp → accepted (no second execution);
     - delivery_attempt ≤ fence → not_accepted;
     - context busy (another op holds the context lock) → not_accepted;
     - else: V1-V3 pre-checks; on failure → not_accepted with reason; else write E-record accepted (fsync file + dir) BEFORE invoking the provider.
   Governance sets G-record dispatched on accepted.
P3 turn: executor sets running, invokes the adapter under supervise (E6), waits for process-group exit, runs V4, writes terminal outcome (fsync), releases locks.
P4 accept (under L1): on a terminal observation, governance verifies Observation.{op_id, context_id, provider_session_id, workspace_path, request_fp} against G-record and binding (V5); for SUCCESS it computes the delta (R5), checks scope/role path split, reviewer-no-mutation, re-reads and hashes the output; then writes succeeded or failed.
P5 recovery (any later command finding a non-terminal G-record): call get_outcome(op_id, fence_through=delivery_attempt).
     - NOT_ACCEPTED: the executor has fenced delivery_attempt ≤ fence, so a delayed stale delivery can never be accepted; governance increments delivery_attempt and redelivers the identical request (same op_id, same fp) — #72 "same-key delivery recovery".
     - ACCEPTED/RUNNING/CANCEL_REQUESTED: report busy; no transition.
     - terminal: continue at P4.
     - UNKNOWN: status unknown; blocked (G6).
```

```
R1 invariant: an op_id is executed at most once: E-record written before invocation; submit is create-if-absent under the per-op lock; the fence blocks stale deliveries after a NOT_ACCEPTED observation.
R2 invariant: the output path is op-scoped: <run_root>/ops/<op_id>/<kind>; acceptance reads only that path, so another op's output is never accepted.
R3 invariant: replacement never reads the replaced context's records; a non-terminal op bound to the old context keeps the run blocked (G9) until P5 resolves it.
R4 invariant: the only Fidenaut-side pending state is the G-record.
R5 invariant (resolves #72 open decision 4): the workspace is a Fidenaut-created worktree on the same host. Before submit, Fidenaut records baseline = {HEAD sha, digest of `git status --porcelain=v2 -z --untracked-files=all` plus the bytes of every dirty/untracked path}. After a terminal outcome, Fidenaut recomputes the same and derives the delta itself: additions, modifications, deletions, mode and symlink changes relative to baseline. Completeness is established by Fidenaut, not claimed by the executor, and G9 plus the context lock make the delta attributable to this op. An executor artifact manifest is therefore not required for same-host worktrees; a remote executor would need #72's manifest (out of scope, R6).
R6 invariant: single host. The executor store and locks live on a local filesystem shared with Fidenaut; multi-host is out of scope.
Q1 invariant: a terminal outcome is written only after the provider process group has exited (supervise waits for group exit or kills it on timeout/cancel). If exit cannot be confirmed, the op stays running and the lock is released only by process death, so get_outcome returns UNKNOWN.
failure: completed-nonzero (#61) → a terminal PROVIDER_FAILURE or AGENT_FAILURE, and the G-record becomes `failed`. The run then needs a governed decision to admit a new op (D2). The difference from today is that the op is terminal and classified rather than stranded.
```

This replaces rows 6, 7, 9, 11, 12, 13 and 14 with two single-record files and two idempotent calls.

## 5. Deletion map

| Component (§1 #) | Disposition | Replacement |
|---|---|---|
| Worker (1) | DELETE | provider session |
| Socket IPC (2) | DELETE | direct executor call |
| Response timeout / busy vs unavailable (3), timeout fallback (12) | DELETE | context lock (`busy`), `resume` → `unavailable` |
| Ping as resume (4) | DELETE | V1-V4 |
| Alias pings (5) | DELETE | G4 record comparison |
| History journal (6) | REPLACE WITH SMALLER PRIMITIVE | E-record |
| Response and recovery counters (7) | DELETE | idempotent submit and get_outcome |
| Receipt (8) | SPLIT | Observation retained; worker attestation deleted |
| Operation-identity registry (9) | REPLACE WITH SMALLER PRIMITIVE | G-record plus E-record |
| Request digest (10) | SPLIT | `request_fp` over the full Request |
| recover dance (11) | REPLACE WITH SMALLER PRIMITIVE | get_outcome plus fence |
| `role_work_pending` (13) | REPLACE WITH SMALLER PRIMITIVE | G-record status plus G9 |
| Pending reconciliation (14) | DELETE | R3 |
| Nonzero gap (15) | SPLIT | G-record `failed` plus #72 class |
| `replace-role-context` (16) | SPLIT | decision and lineage retained; availability comes from `resume` |
| `_role_context` (17) | SPLIT | binding table retained; `create` in the executor |
| Opaque argv (18) | MOVE OUT OF FIDENAUT | typed Request plus adapter |
| `--changed-path` and worker hashing (19) | SPLIT | R5 Fidenaut-computed delta |
| Admission, acceptance, revalidation, locks, state (20-24) | RETAIN IN GOVERNANCE | — |
| Session-collision source (25) | SPLIT | bound `provider_session_id` |
| `supervise()` (26), governance commands (27) | RETAIN IN GOVERNANCE | — |
| Provider launch (29) | MOVE OUT OF FIDENAUT | adapter |

Indicative size:
- **Deleted from WS:** `ManagedExecutionRuntime` plus its worker, about 520 of 1,296 lines.
- **Deleted or replaced in AW:** about 250 of roughly 450 lines in 288-730.
- **Tests deleted:** 8 `ManagedExecutionRuntime` tests.
- **Tests rewritten against a fake executor:** 16 direct role-work tests.
- **Also rewritten:** the shared bootstrap helpers that drive `run-role`.

Machinery that exists only because Fidenaut launches a fresh provider process per op and manufactures a context around it: rows 1-5, 7, 12 and 14, plus the attestation half of 8.

## 6. Minimal executor contract (provider-neutral; realizes #72)

Wire format:
- `format: "fidenaut-executor-v1"` on every request and response.
- Canonical JSON (sorted keys, ASCII, no insignificant whitespace).
- Unknown fields are rejected.
- Enums are upper-case strings.
- Paths are absolute and resolved (no symlinks).
- Timestamps are RFC 3339 UTC.

```
Request  := {role, workspace_path, base: {repo_head}, instructions: {path, sha256},
             inputs: [{kind, path, sha256, byte_length}], output_path, timeout_s,
             tool_policy: READ_ONLY | WRITE_SCOPED, adapter: {name, version, model, config_sha256}}
Context  := {context_id, creation_key, role, workspace_path, provider_session_id, generation, created_at}
Observation :=
   {status: NOT_ACCEPTED, op_id}
 | {status: ACCEPTED | RUNNING | CANCEL_REQUESTED, op_id, context_id, request_fp}
 | {status: UNKNOWN, op_id, reason}
 | TerminalOutcome
TerminalOutcome := {status: SUCCESS | AGENT_FAILURE | PROVIDER_FAILURE | EXECUTOR_FAILURE | CANCELLED,
                    op_id, context_id, request_fp, provider_session_id, workspace_path, output_path,
                    exit_code: int | null (null only if the provider never started), inputs_verified: bool,
                    tool_calls_observed: int, started_at, ended_at, diagnostics: str}
contract: create(creation_key, role, workspace_path) -> Context | error{WORKSPACE_INVALID | PROVIDER_UNAVAILABLE | CONFLICT}
contract: resume(context_id) -> {status: AVAILABLE | BUSY | UNAVAILABLE | UNKNOWN, context}
contract: submit(op_id, delivery_attempt, context_id, request_fp, Request) -> {status: ACCEPTED | NOT_ACCEPTED | CONFLICT, reason?}
contract: get_outcome(op_id, fence_through: int) -> Observation
contract: cancel(op_id) -> {status: CANCEL_REQUESTED | ALREADY_TERMINAL | UNKNOWN}   (optional)
```

Context availability:
- The initial state is `AVAILABLE` after `create`.
- `AVAILABLE` ↔ `BUSY` follows the context lock.
- `UNAVAILABLE` is terminal for that context_id. It is reported only when the adapter definitively reports the session absent, or V2 or V3 fails.
- `UNKNOWN` covers adapter lookup errors. It blocks replacement.
- A replaced binding is recorded in lineage as retired.

```
V1 invariant: turns use only a resume path that fails when the session is absent (never implicit creation). Copilot CLI 1.0.89: `--resume <unknown>` → rc 1, no session; `--session-id <unknown>` silently creates → used only by create.
V2 invariant: before a turn, adapter.session_workspace(session) == context.workspace_path, else UNAVAILABLE.
V3 invariant: before a turn, adapter.session_marker(session) == the marker the executor recorded after the previous turn (e.g. events-log length/last event id); a mismatch means out-of-band use of the session → UNAVAILABLE.
V4 invariant: after a turn, the session id, workspace and marker advance are re-read; any mismatch → UNKNOWN, never SUCCESS.
V5 invariant: governance re-checks every identity field of a TerminalOutcome against its G-record and binding before P4 acceptance.
C1 invariant: per-context flock held from E-record accepted to terminal; a submit on a busy context returns NOT_ACCEPTED (reason BUSY) with no E-record write.
A  contract (adapter): start_session(creation_key→session_id, workspace) ; session_exists(id) -> YES|NO|ERROR ; session_workspace(id) -> path|ERROR ;
   session_marker(id) -> str|ERROR ; run_turn(id, workspace, prompt, tool_policy, timeout) -> {exit_code, tool_calls_observed, diagnostics}.
   A1 session_id is derived deterministically from creation_key where the provider allows caller-chosen IDs (Copilot `--session-id`), making create idempotent across a lost response.
   A2 READ_ONLY maps to the provider's tool allow-list (Copilot `--available-tools view grep glob`); governance independently rejects any R5 delta after a READ_ONLY op.
   A3 failure classification: nonzero exit with tool_calls_observed == 0 and a provider error → PROVIDER_FAILURE; nonzero exit after tool calls → AGENT_FAILURE only if R5 shows the delta, else UNKNOWN; otherwise EXECUTOR_FAILURE or UNKNOWN per #72's effects rule.
```

The planner → reviewer → planner revision → reviewer → implementation cycle runs as follows:
1. At the first entry to PLANNING, Fidenaut calls `create(k1, planner)`; at the first entry to PLAN_REVIEW it calls `create(k2, reviewer)`. Both bindings are recorded (G8, generation 1).
2. Each turn is `resume` (expecting `AVAILABLE`), then `submit` with a new op_id, then `get_outcome`.
3. A plan revision reuses the planner context_id, so it runs in the same provider session (V1-V3).
4. The reviewer's second pass reuses the reviewer's context.
5. IMPLEMENTATION calls `create(k3, implementer)`, and G4 checks that all bindings are pairwise distinct.
6. A replacement is allowed only when `resume` returns `UNAVAILABLE` (never `BUSY` or `UNKNOWN`). It runs `create` with a new creation_key and gets generation+1 plus a lineage record.

## 7. What becomes simpler

**Complexity that existed only to manufacture durable conversational context:**
1. A resident worker per role, with its process group, start deadline and stop/kill path (WS:221-263, 512-555).
2. The socket protocol and endpoint files.
3. A 2 s IPC wait against 3,600 s of work, with the `RuntimeResponseTimeout` paths (#63).
4. Resume defined as "the ping answered", and busy versus unavailable inferred from timeouts.
5. Cross-role pings, plus the stranding risk they introduce.
6. A per-context history journal with response-write and recovery bookkeeping.
7. Worker-attestation receipt fields and their acceptance checks.
8. Sequencing: recover-then-execute, and timeout-then-recover.
9. Reconciling a replaced worker's history file (#69).
10. Opaque argv with DEVNULL output, which is what makes failures unclassifiable (#61).
11. Operator-declared `--changed-path`, which R5 replaces with a Fidenaut-computed delta.

**Complexity that remains because it is governance:**
1. Gate→role map, revision limits and approvals.
2. Admission: role, scope, test/implementation path split, and canonical input identity.
3. Acceptance: re-hashing, baseline delta (R5), reviewer no-mutation check, gate-time revalidation.
4. The G-record, including `unknown` and the fail-closed P5.
5. The binding table and lineage, G4, and the replacement decision.
6. Fidenaut-run validation, and Git/PR governance.
7. State revision checks, the per-run lock, and G9.

**Migration boundary (M):**
- New mode: `workflow.role_execution: "executor-v1"`. The old `role_execution_contexts: true` means `managed-v0`.
- Enumeration: any `runs/issue-*/state.json` whose `role_contexts.*` contains `instance_id` is a managed-v0 run.
- Admission cutoff: new runs use the mode configured at `init`, recorded in state. A run never switches mode; `executor-v1` commands refuse managed-v0 state and vice versa. That refusal is a test.
- Existing runs complete under managed-v0 or are retired with `supersede-run`.
- Deletion gate: managed-v0 code is removed only when the enumeration returns zero runs. Rollback is by config for new runs only.

## 8. Open items

```
U1 isolation qualification (#72 open decision 1) — acceptance gate of the first adapter, not of this architecture: a conformance test showing that two contexts cannot read each other's history, workspace, session-state directory or credentials-bearing state beyond enumerated shared inputs. #76 measured history separation only (0/8 cross-role leaks); tool/path isolation for Copilot is unmeasured. If the test fails, the adapter adds per-role worktrees and/or OS-level isolation; the §6 contract does not change.
U2 provider neutrality: only Copilot CLI 1.0.89 measured (n=4, one run). Every adapter must satisfy V1-V4 and A1-A3 or be rejected.
U3 whether Copilot exposes a stable session_marker (events.jsonl present under ~/.copilot/session-state/<id>/ in 1.0.89; format stability unverified).
D1 unknown-op disposition (#72 open decision 2): default is remain blocked; the existing supersede-run is the only exit. No new abandonment path is designed here.
D2 terminal-failure policy (#72 open decision 3): default is no new op without a governed authorization; which authorization (revision budget, requester) is deferred. No retry is implemented.
D3 executor packaging (in-repo module vs. separate tool) and context cleanup (hygiene only).
```

### Acceptance tests (implementation must provide)

| Rule | Test |
|---|---|
| R1, P2 | Kill the executor after writing E-record `accepted`, before the provider starts. get_outcome returns `UNKNOWN`, the G-record becomes `unknown`, and resubmitting the same op_id returns `ACCEPTED` with 0 additional provider invocations. |
| R1, P3 | Drop the response after a terminal write. get_outcome returns the same outcome, with exactly 1 provider invocation. |
| Fence | A `NOT_ACCEPTED` observation, then a delayed attempt-1 delivery, returns `not_accepted`; the attempt-2 delivery runs once. |
| P2 conflict | Same op_id with a different request_fp or context_id returns `CONFLICT`, with 0 invocations. |
| C1 | A concurrent submit on a busy context returns `NOT_ACCEPTED`/`BUSY`. |
| Q1 | A child that ignores SIGTERM on timeout: the terminal outcome is written only after the group has exited. |
| V1 | An absent session returns `UNAVAILABLE`; the provider is never asked to create a session. |
| V2, V3 | A changed workspace, or an out-of-band turn on the session, returns `UNAVAILABLE`. |
| V4, V5 | A tampered Observation field is rejected at P4. |
| G4 | Aliased context_id or provider_session_id across any two roles is rejected. |
| G9 | Admission is refused while any op is reserved, dispatched or unknown. |
| R2 | Output written outside the op-scoped path is not accepted. |
| R5 | Add, modify, delete, chmod and symlink changes are all detected; a delta outside scope is rejected; any delta after a READ_ONLY op is rejected. |
| #61 | A nonzero provider exit with 0 tool calls gives `PROVIDER_FAILURE`, and the G-record becomes `failed` (not pending). |
| M | Each mode refuses the other mode's run state. |
| G6 | UNKNOWN persists across restarts, and only `supersede-run` leaves it. |

## Working notes

- Probe (2026-10-01): `copilot --resume <unknown-uuid> -p …` exits rc 1 and creates no session. `--session-id <unknown-uuid>` creates one. This is the basis for V1 and A1.
- Spec review found several issues:
  - The spec now adopts the #72 outcome taxonomy, NOT_ACCEPTED, the separate get_outcome, the creation key, and full request identity.
  - It drops the invented "abandon" transition, because #72 keeps persistent UNKNOWN unresolved.
  - It adds the fence, Q1, V3, G9, R5 and L1.
  - G4 is now all-role and necessary-not-sufficient.
- Overridden reviewer point: the reviewers asked for a mandatory executor artifact manifest. R5 instead has Fidenaut compute the delta on a same-host worktree, which is stronger evidence (Fidenaut-observed rather than executor-claimed). A remote executor would reinstate the #72 manifest requirement (R6).
- Ruled out: wrapping the current worker as an "executor" behind this contract. It would preserve rows 1-5 for no governance benefit.
