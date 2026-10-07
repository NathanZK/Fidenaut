# Copilot empty-session creation

## Context

The supported Copilot CLI interface has no safe operation for creating an empty session.
A prompt-driven bootstrap would execute work outside an admitted workflow operation.

## Choice

Keep adapter session creation unavailable until Copilot supports native empty-session creation.
Fail closed before launching a process; treat deterministic IDs as candidates, not proof of existence.

## Ruled out

- Starting an interactive session, because its lifecycle and effects are not bounded by the operation contract.
- Starting a prompt with `--session-id`, because that executes an ungoverned bootstrap action.
- Enabling provider adoption, because session creation and trusted-provider qualification remain incomplete.
