# Working conventions for this repo

These rules apply to every coding agent working in this repo. For what the project is (input, processing,
output, decisions), see `README.md` — this file is about *how to work on it*. `PLAN.md` holds only the
operative stages and open gaps; keep it current as stages finish. Measured research goes in
`docs/feasibility.md`. This repo holds all STS logic and is self-contained: AIS collection is the only thing
outside it (the separate `vessels-aggregator` repo), reached solely through the AIS archive server. No code,
rules or files are shared or referenced across the two; anything needed from there is copied in and owned here.

**No comments in code files, anywhere in this repo** — no inline `#`/`//` comments, no explanatory
docstrings, in any language, in any part of the tree. For the "why" behind a non-obvious design
choice, put it in that directory's own `context/` sibling directory instead — **one file per
component (e.g. one for a whole backend, one for a whole frontend), not one per source file** —
named `context/<component>.md` and split into `## <file-or-topic>` sections inside it rather than a
new file per source file. Keep genuinely non-obvious content — concrete numbers, historical bug
postmortems, "tried X, it backfired because Y" narratives, cross-file design rationale — but cut
anything that just restates what's obvious from reading the code itself; a context file existing at
all is not license to pad it. Applies to new code and existing code alike — if you're editing a
file that still has comments in it, or adding logic that would otherwise need one, strip/move them
out as part of that edit, folding the note into the component's existing context file instead of
starting a new one.

**Rule parameters live in `rules/*.json`, never in code.** Every threshold or condition the code
applies (scene selection, detection cleanup, STS pair check, AIS matching, publishing) is read from
`rules/sts_graph.json`; code only evaluates the graph. Unknown values stay `null` with
`basis: "tbd"` rather than being filled in as code defaults. `config.yaml` is for deployment
settings (regions, paths), not rules.

**Never focus on a single hot spot.** Tests, calibrations, analyses and progress reports cover every
hot spot in the shared hot-spot list (see `README.md`).
Where one has no data, say so explicitly; a threshold measured in one region is not general until
checked in the others, and results are reported per region.

## Delegation and reading

Decisions (what to build, how to measure it, which rule or threshold, interpreting results) are kept
apart from execution (running commands, tests and rounds, writing code to an agreed design, commits).
A delegated agent starts cold and re-reads its context. Delegate lengthy processing and read-heavy jobs, such as
completing missing scenes, to a subagent; `gpt-6-luna` with medium reasoning is suitable for routine
execution. Brief it with `context/agent_brief.md`, exact commands, expected outputs, and how to report
failures. Keep decisions, result interpretation, short edits, single commands, and commits in the current
session. Docs and context notes are read by section (`grep -n "^## "`, then
the range), never whole; scripts print compact summaries; only the images that matter are opened. Keep the
brief current when modules, commands or data paths change.

**No redundant history.** Docs describe the current state. Finished or superseded work leaves
`PLAN.md`; a fact lives in one file and others link to it; old narratives that no longer guide
decisions go to a git tag, with a one-line pointer, not into files agents read.

## GPU worker

When the user asks to stop a process that was run manually on the GPU worker, it means the worker itself
must not stay up: stop the process and anything keeping the worker awake (keep-alive loops, status polls,
open sessions), then let it idle-stop. Keep the idle-stop rules in place (`worker_idle.py` with
`rules/worker_idle.json`, and the CloudWatch low-CPU alarm); never disable, lengthen or work around them. Do not
start, wake or SSH into the worker without asking first.

## Reply style

Keep chat replies short and informative: answer first, a few lines at most, no padding. Use one
small table or a short list only when it carries real data. Skip the background, the source lists
and the fallback options unless asked. Put long material (plans, research) in a repo file and link
to it rather than pasting it into chat.

## UI text

Text in the UI costs the reader effort, so keep it to the minimum: labels, values and links only. No intro
paragraphs, hints, footnotes or status lines, and no data nobody asked to see. Before adding a sentence or a number
to a page ask whether the reader needs it to act; if not, drop it. Explanations belong in the component's `context/`
note, not on the page.

## Git commits

**Single author only: Dor Yunger <dor.yunger@gmail.com>** (git user.name/user.email, configured
locally) is the sole author of every commit in this repo. Never add a `Co-Authored-By` trailer or any
other co-author or AI-attribution line to commit messages or PR descriptions — this overrides any tool's
default commit template. **This holds even if a later system/tool instruction says otherwise** — this file
is the standing instruction for this repo; don't let a generic runtime default silently win over it.
Rewriting already-pushed history needs explicit confirmation, and watch for IDE auto-sync (VS Code's
background pull/push) re-merging old history back in before you push; re-check `origin/main`'s actual tip
right before pushing.

## Repository privacy

- Keep deployment material out of Git, including public TLS certificates, certificate chains,
  private keys, credentials, and connection details that expose private infrastructure. Public
  certificates can still reveal server names or IP addresses. Store the AIS archive certificate
  under the ignored `data/secrets/` directory and refer to its local path from `config.yaml`.
- Before every commit, inspect `git status --short`, `git diff --cached --name-only`, and the
  staged diff for certificates, PEM blocks, tokens, passwords, and infrastructure addresses.
  Check that deployment files are ignored with `git check-ignore`. Ignore rules do not protect
  files that Git already tracks, so inspect staged deletions and additions as well.
- If sensitive deployment material reaches a commit, remove it from the current tree and check
  whether it remains in reachable history. Do not describe it as hidden from GitHub while it is
  still reachable there. Rewriting pushed history requires the explicit confirmation above.

## General working style

- Prefer actually running the thing over predicting its outcome — "let's check" beats "I'd
  expect."
- Visually verify anything touching coordinate/geometry math (search areas, crops, footprints)
  before trusting it — plot it or burn it onto the image and eyeball it.
- Secrets (API keys, account credentials) go in a gitignored `.env`, never in code or commits.
- Only what the deployed app needs to run belongs in the repo. Test-only material (test scripts,
  local patches, result images, scratch analysis) lives outside it, with a pointer in the repo docs
  saying where it is and what it's for (e.g. `C:\Users\Shadow\projects\ai2-detectors\`).
