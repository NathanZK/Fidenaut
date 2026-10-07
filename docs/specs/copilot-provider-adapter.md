# Copilot CLI adapter

purpose: Provide fail-closed, testable Copilot CLI session observations and bounded read-only turns.
entry: `scripts/copilot_adapter.py`; the adapter is not wired into workflow execution.

## Contract

- `derive_session_id(creation_key)` derives a deterministic UUIDv5 from a non-empty UTF-8 key and fixed namespace.
- `CopilotAdapter.create(...)` validates its inputs, then returns `ProviderUnavailable`. It never starts a prompt or interactive process.
- Provider session IDs are opaque exact identifiers. Before path or argument use, accept only ASCII letters, digits, `_`, and `-`, up to 128 characters.
- `observe_session(...)` checks only the exact session event log at `~/.copilot/session-state/<session_id>/events.jsonl`.
- `run_turn(...)` requires an exact session, workspace binding, executor marker checkpoint, role, operation identity, request fingerprint, and verified request inputs.
- The canonical request fingerprint uses `executor_protocol.request_fingerprint(op_id, context_id, request)`. Mismatched fingerprints fail before provider execution.
- Invalid inputs raise `ValueError`; unavailable capabilities raise `ProviderUnavailable`.
- Unresolvable observations return `UNKNOWN`; `ProviderUnknown` reports uncertainty outside an observation result.
- Session creation, history editing, marker copying, and parallel history storage are not supported.

## Session observation

- Resolve the session directory beneath the current home directory without following symlinks.
- Require exactly one `session.start` event with matching session ID and canonical workspace.
- Strictly parse JSONL records and reject malformed, duplicate, missing, or contradictory start metadata.
- Capture the canonical path, device, inode, byte length, SHA-256, event count, and last event ID.
- Require two consecutive reads with matching identity, length, digest, and event metadata.
- Before a turn, every observed marker field must match the executor-supplied checkpoint.
- After a turn, require the same file identity and a longer event log.
- Verify that the previous digest and event ID remain at their original prefix positions.
- A confirmed absent session or pre-turn identity mismatch is `UNAVAILABLE`.
- Malformed, unreadable, unstable, replaced, or ambiguous observations are `UNKNOWN`.
- The marker proves continuity only; it does not prove provider truth, isolation, or qualification.

## Turn execution

- Accept only `READ_ONLY`, mapped to exactly `view`, `grep`, and `glob`.
- Reject `WRITE_SCOPED` before starting a process.
- Invoke Copilot with an argv array, exact `--resume=<session_id>`, canonical `-C` workspace, and JSON output.
- Disable custom instructions, built-in MCPs, remote features, ask-user, auto-update, and shell environment loading.
- Use a bounded timeout, closed stdin, and a fresh POSIX process group.
- Pass only `PATH`, `HOME`, and available locale or temporary-directory variables to the child.
- Terminate timed-out groups, escalate to kill after a bounded grace period, and verify observable group exit.
- Parse only supported event types; require unique event IDs and matching `toolCallId` values.
- Check output byte counts and digest; malformed, contradictory, truncated, or unknown events yield `UNKNOWN`.
- Diagnostics and `UNKNOWN` reasons use fixed sanitized messages.
- They never expose provider output, stderr, prompt text, credentials, or filesystem exception details.
- Input files are opened without following symlinks and checked for stable identity, size, and SHA-256.

## Outcomes and boundaries

- Observations are `AVAILABLE`, `UNAVAILABLE`, or `UNKNOWN`; turn outcomes are `SUCCESS`, `PROVIDER_FAILURE`, `AGENT_FAILURE`, or `EXECUTOR_FAILURE`.
- `UNKNOWN` is unresolved and never authorizes a retry or a replacement operation.
- A provider error with no observed tool calls may be `PROVIDER_FAILURE`.
- `AGENT_FAILURE` requires independent executor-side evidence that consequential effects are accounted for.
- `executor_effects_accounted` is an executor-supplied assertion; the adapter does not establish that evidence.
- A success requires successful process exit, a complete assistant message, paired tool calls, and a continuous post-turn marker.
- Terminal results preserve `status`, `op_id`, `context_id`, `request_fp`, provider session, workspace, output, exit code, input verification, tool count, timestamps, and diagnostics.
- The adapter does not authorize governance transitions, retries, context replacement, or artifact acceptance.
- Fake-runner tests establish local behavior only; no real Copilot session or provider behavior is qualified.
- Workflow wiring and `executor-v1` adoption remain disabled until safe native session creation exists.
- Adoption also requires trusted-provider qualification under #83 and the separate human-approved integration gate under #84.
