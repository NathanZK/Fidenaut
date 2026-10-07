# Executor protocol and deterministic fake

authority:
- `a01ae97:docs/engineering/external-executor-contract.md`
- `a01ae97:docs/staging/specs/2026-10-01-governance-execution-separation.md`
convention: Python 3.9; provider code and unittest coverage live under `scripts/`; use standard-library APIs and explicit errors; new/touched provider Python is checked by Ruff and the repository test runner.

## Contract

- Implement the four mandatory executor operations in a provider-neutral module under `scripts/`: `create`, exact `resume`, identified `submit`, and repeatable `get_outcome`. Do not wire these operations into the workflow provider or add a real provider adapter.
- Accept the contract's context, request identity, operation, and observation fields without changing their meaning. Validate absolute normalized paths, request shape, input byte lengths, and SHA-256 digests before admission. Canonical request fingerprints use sorted-key, ASCII JSON with compact separators and SHA-256. A supplied fingerprint that does not match the submitted request is a conflict.
- `create(creation_key, role, workspace_path)` is idempotent for the same key and same request; same key with changed parameters returns `CONFLICT`. Serialize same-key creation with a lock file named by a digest of the key; never put raw keys in paths. Persist the mapping from creation key to immutable context before returning it.
- `resume(context_id)` addresses only that exact context and returns `AVAILABLE`, `BUSY`, `UNAVAILABLE`, or `UNKNOWN`; it never substitutes a context. A valid existing record with an existing bound workspace and free context lock is `AVAILABLE`; a held context lock is `BUSY`; a definitively absent context or workspace is `UNAVAILABLE`; corrupt state or inability to establish either condition is `UNKNOWN`. A missing workspace does not erase a terminal operation outcome.
- `submit(op_id, delivery_attempt, context_id, request_fp, request)` binds the immutable operation ID, context, and request fingerprint. Different identity is `CONFLICT`; a previously fenced delivery or busy context is `NOT_ACCEPTED`; an eligible first delivery is durably admitted before fake invocation. In accordance with the pinned protocol, a busy-context refusal does not write an E-record; governance fences it with `get_outcome` before any redelivery. Repeated delivery of an already accepted or terminal operation never invokes it again.
- `get_outcome(op_id, fence_through)` never executes work. Under the per-operation lock, if no accepted E-record exists, atomically persist `fence = max(fence, fence_through)` before returning `NOT_ACCEPTED`; any later submit must use a strictly greater `delivery_attempt`. If an E-record exists, do not change its fence or state: return terminal outcome if terminal, otherwise `ACCEPTED`/`RUNNING` while its execution lock is held and `UNKNOWN` when it is free. The per-operation lock serializes this decision against submit. Missing, malformed, unavailable, or inconsistent durable state fails closed and must not be interpreted as absence.
- `UNKNOWN` is an observation, not a terminal E-record state. It persists across restart as the observation of an accepted/running record whose execution lock is free; the E-record remains accepted/running and immutable except for its valid forward transition. Same-key submit of that record may report `ACCEPTED` but never invokes it again. Only repeatable lookup of the same operation can reveal a terminal record; this fake does not invent provider evidence.
- Persist contexts and one E-record per operation in a local filesystem store rooted at an explicit caller-supplied directory. Serialize records as versioned JSON. Write via same-directory temporary file, flush and `fsync`, atomic replace, then `fsync` the containing directory. Use crash-releasing per-operation and per-context POSIX locks; hold both across admission, fake invocation, and outcome persistence. Unsupported lock/filesystem conditions fail explicitly.
- Open lock files without following symlinks; a symlinked lock path or unsupported no-follow/locking behavior fails closed.
- Lock order is fixed: context creation takes its creation-key lock then the context lock; operation submission takes its operation-state lock, context lock, then per-operation execution lock; outcome lookup takes the operation-state lock and observes the execution lock; resume takes only the context lock. Locks are acquired non-blocking where a busy response is possible. No path acquires these locks in reverse order.
- Before `os.replace`, a failed write leaves the prior complete record authoritative and must not invoke the fake. After replace and successful directory `fsync`, the new record is durable. If write, replace, or either `fsync` reports failure, do not invoke or report success; on restart, validate the observed record strictly and fail closed if its state cannot be established. JSON with duplicate keys, unsupported versions/fields, invalid types, or cross-record identity mismatches is corrupt, not absent.
- Records survive executor object/process restart for the caller's retention period. Never garbage-collect records as part of this implementation. Lock files may remain after release; lock ownership is the OS lock, not file presence or the E-record's diagnostic owner PID/start time.
- The deterministic fake is a test adapter with separately durable invocation/effect counters and explicit fault injection immediately before/after each context and E-record write boundary, before invocation, after invocation, after an effect, before/after terminal persistence, before response, during restart, and with partial/corrupt/unavailable records. A crash after an effect but before terminal persistence leaves the E-record accepted/running; after locks release, lookup is `UNKNOWN`, with the fake's effect count preserved. It is contract evidence only, not provider, isolation, or production-durability qualification.

## State transitions

| Entity | Current state | Event and guard | Durable result / observation |
| --- | --- | --- | --- |
| Context | absent | Create with a new key and valid inputs | Persist one immutable context mapping; return it |
| Context | present | Create with same key and same inputs | Return the same context |
| Context | present | Create with same key and changed inputs | `CONFLICT`; no new context |
| Context | present | Resume exact ID, lock free | `AVAILABLE` |
| Context | present | Resume exact ID, lock held | `BUSY` |
| Context | absent | Resume exact ID after successful store read | `UNAVAILABLE` |
| Context | any | State/lock cannot be established | `UNKNOWN`; no substitution |
| Operation | absent or fenced | Submit with attempt `<= fence` | Preserve fence; `NOT_ACCEPTED`; no invocation |
| Operation | absent or fenced | Submit with attempt `> fence`, matching context, both locks acquired | Persist `ACCEPTED` before fake invocation |
| Operation | absent or fenced | Context lock busy | No E-record write; return `NOT_ACCEPTED`; no invocation. The caller must fence with `get_outcome` before redelivery |
| Operation | accepted | Fake begins execution | Persist `RUNNING` before effect |
| Operation | accepted or running | Execution lock released without durable terminal record | Preserve record; lookup observes `UNKNOWN`; never invoke again |
| Operation | accepted or running | Effect and terminal result are known | Persist one terminal outcome; release locks |
| Operation | terminal | Any same-identity submit or lookup | Preserve and return the same immutable terminal outcome; no invocation |
| Operation | absent or fenced | Lookup with `fence_through` | Persist max fence before `NOT_ACCEPTED`; a later eligible attempt must be greater |
| Operation | accepted, running, or terminal | Lookup | Do not change fence; return live/unknown observation or immutable terminal result |
| Operation | any | Identity conflict or invalid/corrupt state | `CONFLICT` or explicit fail-closed error; no invocation |

For submit, acquire the operation-state lock before reading or changing its
record, then acquire the context lock before accepting a new operation. A
busy context returns `NOT_ACCEPTED` without writing an E-record, matching the
pinned architecture; its caller follows recovery with `get_outcome` to write
the delivery fence before redelivery. On successful admission, acquire and
hold a separate per-operation execution lock through terminal persistence.
Lookup acquires the operation-state lock when available and uses the
execution lock—not context occupancy—to distinguish live execution from a
crash-released operation lock. Lookup and submit therefore serialize the
absent-record fence race: whichever acquires the operation-state lock first
defines whether that attempt was accepted or fenced. Only an accepted
operation may progress to `RUNNING`; only a known, accounted-for fake result
may progress to an immutable terminal outcome. There is no transition from
`UNKNOWN` to `NOT_ACCEPTED`, and no automatic transition out of effect-possible
uncertainty.

## Invariants

- An operation ID maps to one context and one immutable request fingerprint. A context maps to its creation key and immutable context identity.
- An E-record is durably admitted before the fake is invoked. No crash, lost response, restart, timeout, malformed state, or retry can turn an accepted or effect-possible operation into `NOT_ACCEPTED` or trigger a second invocation.
- A `NOT_ACCEPTED` result applies only after the delivery fence makes that attempt permanently stale. A later permitted identical delivery retains the same operation ID and fingerprint.
- A terminal outcome is immutable. A known failure never authorizes a distinct operation; that decision belongs to governance outside this module.
- A busy context, stale fence, conflicting identity, invalid fingerprint, or corrupt/unavailable state never invokes the fake.
- No code in this change owns workflow transitions, approval or role policy, session history, provider lifecycle, retries, scheduler/queue, cancellation, or context replacement.

## Acceptance

- Unit tests assert create replay idempotency and conflict behavior including concurrent same-key creation; exact resume for all four observations; request fingerprint verification; operation/context binding; terminal immutability; and durable state across a newly constructed executor.
- Crash/fault tests cover context creation and every file-write phase; admission before invocation; immediately before/after invocation and possible effect; terminal persistence before response; restart; and partial/corrupt/unavailable state including duplicate JSON keys and unsupported record versions. Each checks the returned disposition, durable record/fence, and invocation/effect counters.
- Concurrency tests use separate executor instances/processes to exercise the specified lock order, operation/context/execution locks, busy refusal followed by explicit durable fencing, absent-record lookup racing submission, concurrent submit/lookup, and stale clients. Rejected work has zero invocation; an operation has at most one invocation.
- Tests prove lookup does not execute; a fenced `NOT_ACCEPTED` attempt cannot later be accepted but a strictly newer identical delivery can; dropped terminal responses recover the same immutable outcome; `UNKNOWN` remains after restart and cannot be reinvoked; terminal failures do not authorize a distinct operation; conflicts and invalid input fail closed; and no replacement identity is silently created.
- Tests assert record version validation, same-directory atomic replacement, file and directory `fsync` ordering, explicit failures when locking/flush primitives fail, and the pre/post-replace crash guarantees. Scope checks confirm no workflow transitions, approval/role policy, session history, provider lifecycle, retry, scheduler/queue, cancellation, or replacement logic was introduced.
- Test evidence records exact setup, injected fault, returned observation, persisted state, and fake invocation/effect count. No test or claim treats this fake as evidence of real-provider integration, isolation, or production deployment durability.
- Run focused unittest coverage, `python3 scripts/run_agent_workflow_tests.py`, and `python3 scripts/run_python_lint.py`; record exact commands/results and any checks not run.

## Deferred

- Real provider adapters, workflow-provider integration, artifact transfer or repository acceptance, isolation qualification, cancellation, cross-host storage/locking, retention-duration policy, and governance authorization for replacement or retry.
- Deployment-specific choice of executor root and backup/retention operations.
