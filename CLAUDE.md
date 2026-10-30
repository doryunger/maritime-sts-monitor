@AGENTS.md

## Claude: model, effort and subagents

The repo rules are in `AGENTS.md` (imported above) and apply in full. On top of them, for Claude Code:
decisions are made by Opus at medium effort and execution is done by Sonnet at medium effort; delegated long
runs, rounds and read-heavy jobs go to a Sonnet subagent briefed with `context/agent_brief.md` plus exact
commands. The single-author rule in `AGENTS.md` overrides the default Claude Code commit template: never add
a `Co-Authored-By: Claude` trailer. The GPU worker rule in `AGENTS.md` applies too: stopping a manual process
there means the worker goes down and the idle-stop rules stay in place.
