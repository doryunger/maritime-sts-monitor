# Agent brief (read this instead of the full docs)

Conventions are in CLAUDE.md (already loaded). Read docs by section only: `grep -n '^## ' <file>`, then
`sed -n` the range. Use `.venv/Scripts/python`. Print compact output.

## Module map
- `maritime_tip_cue/sts/`: STS pair check. `s2window.py`, `s2pair.py` (Sentinel-2 hull pair), `s1object.py`,
  `s1pair.py` (Sentinel-1 outline pair), `ai2point.py` (Ai2 point half-pixel shift), `rules.py` (reads
  `rules/sts_graph.json`), `fixtures.py` (fixture loaders and tallies), `score.py` (CLI summary).
  Why notes: `maritime_tip_cue/context/sts.md`.
- `maritime_tip_cue/watch/`: hot-spot run. `run.py` (CLI: passes -> detector -> shore filter -> pair check), `detector.py` + `ai2_runner.py` (Ai2 in the ai2-detectors venv; ai2_runner is re-read per scene), `s1.py`, `s2.py` (chips / windows from the detector scratch), `state.py` (SQLite run state, `import`, `summary`), `report.py` (static report). Why notes: `maritime_tip_cue/context/watch.md`.
- `maritime_tip_cue/passes.py`: S1/S2 passes over a hot spot (`--map` burns footprints). `maritime_tip_cue/land.py`: coastline from `data/land/`.
- `maritime_tip_cue/ais/archive.py`: AIS archive client (GizmoSQL; `GIZMOSQL_*` in `.env`, cert path in `config.yaml`).
- `maritime_tip_cue/hotspots.py`: loads the shared hot-spot list from S3 (`config.yaml` `s3`, bucket and keys in `.env`).
- `maritime_tip_cue/config.py`: `.env` and `config.yaml`. `db/`, `__main__.py initdb`: AIS-era scaffold, removed in stage 2.

## Rules
- `rules/sts_graph.json`: scene selection, detection cleanup, pair check, AIS matching. Values only there; unknown stays `null` with `basis: "tbd"`.

## Data (gitignored `data/`)
- `data/sts_fixtures/s1`, `data/sts_fixtures/s2`: labelled hulls and passes with their expected verdicts.
- `data/runs/<name>/`: one folder per run (`state.db`, scene windows/chips, `report/`).
- `data/earlier_runs/`: outputs of earlier runs of this method, reference only.
- Stage 0 detector bench (outside the repo): `C:\Users\Shadow\projects\ai2-detectors\` (README there).

## Docs
- `README.md`: hot spots, processing, S3 output tree, decisions. `PLAN.md`: stages and open gaps.
- `docs/feasibility.md`: measured research. Tip-era docs and rules: tag `tip-era-final`.

## Common commands (repo root)
- `.venv/Scripts/python -m pytest tests -q` fixture tests: verdicts to 1e-6 on the frozen rules (`tests/sts_graph_frozen.json`), label scores on the live rules.
- `.venv/Scripts/python -m maritime_tip_cue.sts.score` per-set STS tallies.
- `.venv/Scripts/python -m maritime_tip_cue.watch.run --site <hot spot> --end <ISO> --days 7 --out data/runs/<name> [--reanalyse]` a run (long: launch detached with a log file; resumes from `state.db`).
- `.venv/Scripts/python -m maritime_tip_cue.watch.state summary <run folder>` per-scene counts.
- `.venv/Scripts/python -m maritime_tip_cue.watch.report <run folder> --qa` also writes `report/qa.html` (QA page to publish); `watch.state qa-import <run folder> <flags dir>` loads reviewed flags into `qa_flags`.
- `.venv/Scripts/python -m maritime_tip_cue.watch.report <run folder> [--singles N] [--seed S]` static report (`report/index.html`, `data.json`, `img/`) from a run's `state.db`; runs without the detector.
- `.venv/Scripts/python -m maritime_tip_cue.hotspots [publish <file>]` print (or replace) the S3 hot-spot list.
