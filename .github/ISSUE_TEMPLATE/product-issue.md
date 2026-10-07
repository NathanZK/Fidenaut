---
name: Product issue
about: Minimal, reusable structure for a Fidenaut repository change
title: ""
labels: []
assignees: []
---

## Problem

What is broken, missing, or friction-causing today? Keep this to the
observable symptom, not a proposed solution.

## Goal

What should be true once this issue is done? Describe the desired outcome,
not the implementation.

## Relevant failure modes

Describe material, plausible failure modes where relevant. Identify the affected
behavior, data, or invariant; the triggering condition or mechanism; and the
resulting failure or regression, including any user-visible consequence.
A category such as "concurrency" alone is not a failure mode. For example:
"Two approval attempts observe the same pending gate, both proceed, and record
conflicting approvals for one candidate."

Write "None identified" if no relevant modes are identified. State important
uncertainty rather than guessing. Do not invent speculative risks or enumerate
irrelevant categories. Keep acceptance criteria, validation, mitigations, and
implementation steps out of this section.

## Repository constraints

- Any repository-specific constraints this issue must respect (for example,
  an existing governance boundary or compatibility requirement that must not
  be redesigned).

Respect Fidenaut's provider responsibilities and the consumer's ownership of
application configuration, tests, and conventions; see the README's provider
and consumer ownership guidance.

Include assumptions, non-scope, dependencies, reproduction details, or exact
implementation paths only when materially relevant. Do not prescribe a design
or file ownership merely to fill out the issue.

## Acceptance criteria

- For each criterion, specify the observable outcome, appropriate verification
  method, and evidence an independent reviewer can inspect or reproduce.
  Identify what is checked, the relevant setup/state, and the expected result.
  Use programmatic checks, rendered UI/UX inspection, repository/documentation
  inspection, or empirical analysis only as needed to establish the criterion.

Evidence must be attributable to the reviewed implementation. Where relevant,
identify the reviewed revision, setup/state, how that state was reached, and
fixtures, mocks, manual state changes, or other limitations on what the evidence
proves. Make the relationship between criterion, method, setup/state, and
evidence inspectable or reproducible. Multiple evidence items must establish
the same claimed behavior/state, not unrelated revisions, constructed states,
or execution paths.

When a change affects rendered appearance/state, provide reviewable visual
evidence from the running application. If screenshots are the available
mechanism, capture the relevant state with enough surrounding UI to judge the
claimed property. A screenshot establishes appearance under its captured
conditions, not interaction or reachability. Interactive criteria also require
reproducible interaction evidence; interaction tests alone do not establish
visual correctness. For a user-flow criterion, establish that the rendered
state is reached through the specified flow, not just a separately constructed
component/state.

Backend-only and non-rendered provider criteria need appropriate non-visual
evidence, not screenshots. This guidance does not mandate end-to-end tests, a
real backend for every screenshot, or a universal browser/device matrix.

An implementer's "passed" or "manually verified" assertion is not itself
evidence, even if it references a test or screenshot. Inspect the supporting
evidence: tests must exercise the intended behavior rather than merely assert
the implementation's own output or mock away the behavior being changed.

## Validation

List applicable general validation separately from criterion-specific evidence.
Record exact commands and results, or manual checks and observed outcomes.
A passing lint/typecheck/build or test suite does not by itself establish every
acceptance criterion.

- Workflow provider: `python3 scripts/run_python_lint.py` and
  `python3 scripts/run_agent_workflow_tests.py` (or narrower, targeted commands
  appropriate to the changed provider behavior or tooling).

For documentation-only changes, record relevant document/template inspection
and preview outcomes; unrelated provider execution is not required. State which
checks were not run and why. Provider validation does not replace a consumer's
application validation.
