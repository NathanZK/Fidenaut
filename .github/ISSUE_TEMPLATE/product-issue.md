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

## Repository constraints

- Any repository-specific constraints this issue must respect (for example,
  an existing governance boundary or compatibility requirement that must not
  be redesigned).

## Acceptance criteria

- List the concrete, checkable conditions that mean this issue is done.

## Validation

- Workflow provider: `python3 scripts/run_agent_workflow_tests.py` (or
  narrower, targeted commands if this issue does not touch provider behavior).
