# Provider qualification evaluator

purpose: Evaluate signed U1/U3 evidence without running provider workflows.
entry: `scripts/qualification_evaluator.py`; no workflow imports or integration.

## API

```python
evaluate_qualification(
    policy_bytes: bytes,
    evidence_bytes: bytes,
    evidence_root: pathlib.Path,
    trusted_keys: Mapping[tuple[str, str], bytes],
) -> QualificationReport
```

`trusted_keys` is caller-supplied and keyed by `(role, key_id)`. Roles are
`authority` and `independent_verifier`; values are raw 32-byte Ed25519 public
keys. The caller must source these role assignments from deployment-approved
trust configuration. The evaluator does not authenticate that governance
source. Keys in candidate JSON are never trusted.

The report is a dictionary with exactly:

| Field | Type | Meaning |
| --- | --- | --- |
| `schema` | string | `fidenaut-provider-qualification-report-v1` |
| `qualification_id` | string or null | Parsed policy qualification identity |
| `policy_sha256` | lowercase SHA-256 or null | Canonical complete signed policy |
| `evidence_sha256` | lowercase SHA-256 or null | Canonical evidence manifest |
| `evaluated_at` | UTC RFC 3339 or null | One captured evaluator time |
| `outcome` | string | `qualified` or `adoption unavailable` |
| `reason_codes` | sorted unique strings | Fail-closed reasons; empty only when qualified |

## Policy and evidence

Policy schema is `fidenaut-provider-qualification-policy-v1`. Its exact
top-level keys are `schema`, `qualification_id`, `binding`, `validity`,
`shared_inputs`, `authority`, `verifier`, `provider_key_ids`, `boundary`,
`u3_signal`, and `signature`. It binds a
qualification to a deployment, provider revision, time window, and at least
two distinct role/context/session assignments. Workspace and credential
assignments may repeat only when the exact shared resource appears in
`shared_inputs`. Shared kinds are `history`, `workspace`, `session_state`,
`tools_processes`, `credentials_environment`, and `shared_state`. Wildcards
are not permitted.

`binding` contains `deployment_id`, `provider`, `window`, and `contexts`.
`provider` contains `name`, `version`, and `adapter_revision`; `window`
contains UTC `not_before` and `not_after`. Each context contains `role`,
`context_id`, `provider_session_id`, `workspace_id`, and
`credential_assignment_id`. `validity` contains UTC `not_before`, `not_after`,
and positive `max_evidence_age_seconds`. `shared_inputs` contains unique
`{kind, id}` objects. `authority` contains `id` and `key_id`; `verifier`
contains `id`, `key_id`, and boolean `independence_approved`. `provider_key_ids`
is an array of unique key IDs. `boundary` contains `id`, boolean `approved`,
`descriptor_path`, and `descriptor_sha256`; `u3_signal` contains `id`,
boolean `approved`, `criteria_path`, and `criteria_sha256`. `signature`
contains `algorithm`, `key_id`, and `value`.

Policy validity includes the whole qualification window and the evaluation
time. The policy names authority and independent verifier identities and keys,
provider key IDs, an approved deployment-boundary descriptor, approved U3
criteria, and the explicitly allowed shared inputs. These values are governed
inputs; the evaluator assigns none.

Evidence schema is `fidenaut-provider-qualification-evidence-v1`. Its exact
top-level keys are `schema`, `qualification_id`, `policy_sha256`, `binding`,
`captured_at`, `valid_until`, `claims`, `verifier_signature`, and
`qualification_approval`; the last key may be absent or null to represent
missing approval. Evidence binds the exact policy digest, qualification ID,
deployment, provider, contexts, sessions, and window. It contains one claim
for each:

- `U1.fresh_context_creation`
- `U1.history_session`
- `U1.workspace_filesystem`
- `U1.tools_processes`
- `U1.credentials_environment`
- `U1.shared_state`
- `U3.exact_resume`
- `U3.continuity`

Each claim identifies status, source kind, observer, observation and expiry
times, exact ordered context/session subjects, and one or more referenced
artifacts. Status is `PASS`, `FAIL`, `UNKNOWN`, or `UNAVAILABLE`. Source kind
is `independent_verifier`, `provider`, or `synthetic`. Only `PASS` claims
signed by the policy-approved independent verifier can qualify. Provider or
synthetic source kinds fail provenance. Fresh-context and exact-resume claims
cover every policy context.

Each claim contains `claim_id`, `status`, `source_kind`, `observer_id`,
`observed_at`, `valid_until`, `subject_context_ids`,
`subject_provider_session_ids`, and a nonempty `artifacts` array. Artifact
objects contain exactly `path`, `sha256`, and nonnegative integer
`byte_length`. U3 continuity also contains `signal_id`, `criteria_sha256`,
`stability`, `out_of_band_state`, and `observations`. Each observation contains
`sequence`, `observed_at`, `context_id`, `provider_session_id`, `workspace_id`,
`signal_sha256`, `predecessor_sha256`, and `signal_artifact`; the first
predecessor is null. Stability is `STABLE`, `UNSTABLE`, or `UNKNOWN`;
out-of-band state is `NONE`, `DETECTED`, or `UNKNOWN`.
`verifier_signature` contains `algorithm`, `key_id`, and `value`. A
qualification approval contains `status`, `authority_id`, `key_id`,
`approved_at`, `policy_sha256`, `manifest_sha256`, `binding_sha256`, `outcome`,
and `signature`. Approval status is `APPROVED` or `REJECTED`; outcome is
`qualified`.

U3 continuity contains stable marker status and one ordered chain per bound
context, with at least two observations per chain. Each observation binds
context, provider session, workspace, signal digest, and predecessor digest.
The signal and criteria IDs must match policy. Unstable, missing, extra, or
contradictory chains fail closed.

Qualification approval must be signed by the policy authority, approved,
current, and bound to the policy, manifest, binding, and qualified outcome.
The manifest digest also commits to the evidence qualification ID. A missing
or rejected approval never qualifies.

## Canonicalization and integrity

Input is strict UTF-8 JSON. Duplicate keys, floats, non-finite numbers,
unknown fields, malformed encodings, and invalid schemas fail closed. Canonical
bytes are exactly
`json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")`.
Array order is preserved. Hash fields are lowercase hexadecimal SHA-256;
signature payloads use raw 32-byte SHA-256 digests.

Ed25519 signatures use the `fidenaut-policy-v1`, `fidenaut-evidence-v1`, and
`fidenaut-approval-v1` domains. Policy signature payload is the policy domain,
newline, and digest of canonical policy without `signature`. Evidence
`manifest_sha256` hashes canonical evidence without `verifier_signature` and
`qualification_approval`; the evidence signature payload is its domain,
newline, and raw manifest digest. `policy_sha256` hashes the complete
canonical signed policy. `binding_sha256` hashes canonical `policy.binding`.
Approval signature payload is its domain, newline, and digest of canonical
approval without `signature`. Policy signatures resolve only under the
authority role; evidence signatures resolve only under the independent
verifier role. Signature values are canonical RFC 4648 base64 encodings of
64-byte signatures. A role or public-key alias across roles is rejected. The
report `evidence_sha256` is `manifest_sha256`.

Artifact paths are relative POSIX paths beneath an existing evidence root.
The root itself and every referenced path component are opened without
following symlinks. Artifacts must be regular readable files with the expected
length and SHA-256. Reads are streamed, and file identity and size are checked
before and after hashing. The boundary descriptor and U3 criteria are hashed
as well as all claim and signal artifacts.

Capture the UTC evaluation clock once. Policy validity, evidence capture,
claim/observation timestamps, approvals, and evidence expiry must satisfy the
qualification window and maximum evidence age. Timestamps are RFC 3339 UTC
values with `Z` or `+00:00`. A missing or failed clock produces
`CLOCK_UNAVAILABLE`; independent checks still run.

## Outcomes and boundaries

Malformed policy produces `INVALID_INPUT`; malformed evidence produces
`EVIDENCE_INVALID`. Other applicable failures include `POLICY_UNTRUSTED`,
`POLICY_EXPIRED`, `BINDING_MISMATCH`, `EVIDENCE_STALE`, `INTEGRITY_FAILURE`,
`SIGNATURE_INVALID`, `PROVENANCE_FAILURE`, `APPROVAL_MISSING`,
`VERIFIER_UNAPPROVED`, `BOUNDARY_UNAPPROVED`, `U3_SIGNAL_UNAPPROVED`,
`CLAIM_MISSING`, `CLAIM_NOT_PASS`, `FRESH_CONTEXT_UNAVAILABLE`, and
`U3_SEQUENCE_INVALID`. After schema validation, the evaluator reports all
independently established failures in sorted order.

`qualified` requires a trusted signed policy, trusted independent verifier,
approved boundary and U3 criteria, all passing claims, valid approval,
matching bindings, current evidence, verified artifacts, and valid U3 chains.
Every failure returns `adoption unavailable`.

The evaluator does not invoke a provider, create contexts, write files, access
the network, update workflow state, or alter adoption configuration. Synthetic
test fixtures use ephemeral keys and verifier-signed fixture claims to exercise
the positive branch. They are not U1/U3 qualification evidence. #82's Copilot
adapter still cannot create fresh contexts and remains unwired. A `qualified`
result does not enable real provider adoption or satisfy #84's separate
integration gate.
