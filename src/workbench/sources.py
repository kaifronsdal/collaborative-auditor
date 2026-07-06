"""`Step.source` / `AnchorEvent.source` constants used across the workbench.

Centralised so the petri private import (`GEN_SOURCE`) is confined to one
`# noqa: PLC2701` and importers don't need lazy `# noqa: PLC0415` imports.
"""

from inspect_petri._auditor.agent import GEN_SOURCE

#: petri's auditor `Tape.replayable(generate, source=…)` tag.
GEN_SOURCE = GEN_SOURCE  # noqa: PLW0127

#: `target_agent`'s `Tape.replayable(generate, source=…)` tag.
TARGET_GEN_SOURCE = "Model.generate"

#: `workbench_auditor` emits an `AnchorEvent` with this `source` at the *end*
#: of each turn (after `execute_tools`). The auditor timeline keeps only
#: these — petri's own `Tape.replayable` `AnchorEvent` (source = `GEN_SOURCE`)
#: lands *before* the turn's `ToolEvent`s, so splicing on it would drop them.
TURN_END_SOURCE = "workbench:turn"
