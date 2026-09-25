# Fidenaut

**Fidenaut — a governed workflow provider for bounded delegated autonomy.**

Fidenaut provides the workflow mechanics for issue-driven, evidence-bound
software changes: planning, review, explicit approval gates, candidate
integrity, validation, governed commits, draft-PR publication, and
fail-closed recovery. It was extracted from
[ChessEcho](https://github.com/NathanZK/ChessEcho), which remains the
originating and reference consumer. The workflow is not inherently
ChessEcho-specific: the consumer supplies repository identity, configuration,
roles, conventions, and application validation.

Fidenaut does not provide an application, a consumer's build or test suite, or
independently authenticated human authorization. Current approval commands
record self-attested local acknowledgments; they do not authenticate the
asserted operator. Role strings likewise do not authenticate actor identity.
See [`docs/provenance.md`](docs/provenance.md) for extraction and trust-boundary
context.

## Provider and consumer ownership

The provider owns:

- `scripts/agent_workflow.py` and `scripts/workflow_supervisor.py`;
- provider regression tests and provider role contracts;
- workflow state, evidence, approval, integrity, and recovery semantics; and
- verification of a pinned external provider runtime.

The consumer owns:

- `.github/agent-workflow.json`;
- its authoritative remote, target/base branch, and run root;
- application validation profiles and application tests;
- repository-specific roles and conventions; and
- issue and pull-request conventions.

A consumer must retain its own repository configuration. Do not copy Fidenaut's
repository identity, remote, target branch, run root, or application
validation profiles into another repository.

## Provider quickstart

From a clean Fidenaut checkout, use Python 3.9 (the provider CI runtime):

```bash
python3 -m pip install -r requirements-lint.txt
python3 scripts/run_python_lint.py
python3 scripts/run_agent_workflow_tests.py
```

These commands validate Fidenaut's provider implementation and regression
suite. They do not validate an external consumer and do not require consumer
application dependencies.

## External-consumer quickstart

An external consumer pins an exact Fidenaut commit; it does not consume an
unpinned moving branch. The current runtime consists of these provider-owned
files:

```text
scripts/agent_workflow.py
scripts/workflow_supervisor.py
```

The consumer or its harness materializes those files at the pinned revision
without including them in the consumer's authoritative candidate or commit.
Fidenaut currently provides no package manager, provider bundle command,
automatic installer, or fetch mechanism.

Create a manifest outside the consumer worktree. Its current canonical shape
is:

```json
{
  "format": "fidenaut-provider-runtime-manifest-v1",
  "provider": {
    "repository": "github.com/NathanZK/Fidenaut",
    "revision": "<40-character-lowercase-commit-sha>"
  },
  "files": [
    {
      "path": "scripts/agent_workflow.py",
      "mode": "100755",
      "sha256": "<sha256>"
    },
    {
      "path": "scripts/workflow_supervisor.py",
      "mode": "100755",
      "sha256": "<sha256>"
    }
  ]
}
```

Point the runtime at that external manifest for every governed command:

```bash
export FIDENAUT_PROVIDER_RUNTIME_MANIFEST=/path/outside/consumer/provider-runtime-manifest.json
```

The runtime verifies the provider revision and every listed path, Git mode, and
SHA-256. Missing, relocated, changed, malformed, or tampered provider inputs
fail closed. The manifest identity is captured at `init` and revalidated on
later state reads. The manifest must be outside the consumer worktree; the
current contract is the environment variable above, not an in-worktree
`.agent-workflow/provider-runtime-manifest.json` file.

Keep the consumer's own configuration and select its own application
validation profile. Fidenaut's provider tests are not a replacement for
consumer validation.

## Governed lifecycle

The normal human-gated path is:

```text
init
→ plan submission/review/approval
→ test submission/review/approval
→ implementation submission
→ consumer validation
→ implementation review
→ implementation approval
→ create-draft-pr
```

Representative command shapes are:

```bash
python3 scripts/agent_workflow.py init ISSUE
python3 scripts/agent_workflow.py submit-plan ISSUE --artifact PATH --agent ROLE --scope PATH
python3 scripts/agent_workflow.py review-plan ISSUE --status READY_FOR_HUMAN_APPROVAL --artifact PATH --reviewer ROLE
python3 scripts/agent_workflow.py approve-plan ISSUE --by LOGIN --confirm plan_approved
python3 scripts/agent_workflow.py submit-tests ISSUE --artifact PATH --agent ROLE --not-applicable --reason "Approved rationale"
python3 scripts/agent_workflow.py approve-tests ISSUE --by LOGIN --confirm tests_approved
python3 scripts/agent_workflow.py submit-implementation ISSUE --artifact PATH --agent ROLE --evidence PATH
python3 scripts/agent_workflow.py run-validation ISSUE --profile CONSUMER_PROFILE
python3 scripts/agent_workflow.py approve-implementation ISSUE --by LOGIN --confirm implementation_approved
python3 scripts/agent_workflow.py create-draft-pr ISSUE --title "..." --body-file PATH
```

`--not-applicable` is valid only when the approved plan establishes that test
implementation is not applicable. Implementation evidence is bound to the
exact candidate diff; changing the candidate requires regenerated evidence.
Approval gates remain explicit even when local self-attestation is configured.

Before `create-draft-pr`, the consumer or harness must create the local
non-target publication branch, produce the governed implementation commit, and
push that branch to the configured authoritative remote. `create-draft-pr`
validates the publication topology and invokes GitHub PR creation; it does not
push the initial branch. A disposable end-to-end publication experiment
therefore needs a genuinely disposable writable fork, mirror, or other safe
remote. Do not push an experiment branch to the real ChessEcho repository just
to make publication pass.

## Deeper documentation

- [`docs/engineering/agent-workflow.md`](docs/engineering/agent-workflow.md):
  manifest schema, integrity rules, command reference, state transitions,
  validation, publication, reconciliation, and recovery.
- [`docs/engineering/agent-workflow-architecture.md`](docs/engineering/agent-workflow-architecture.md):
  workflow architecture and trust boundaries.
- [`docs/engineering/agent-workflow-maintainer-summary.md`](docs/engineering/agent-workflow-maintainer-summary.md):
  maintainer-oriented contracts and invariants.
