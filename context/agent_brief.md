# Agent brief (read this instead of the full docs)

Conventions are in CLAUDE.md (already loaded). Read docs by section only: `grep -n '^## ' <file>`, then
`sed -n` the range. Use `.venv/Scripts/python`. Print compact output.

## Module map
- `maritime_tip_cue/sts/`: STS pair check. `s2window.py`, `s2pair.py` (Sentinel-2 hull pair), `s1object.py`,
  `s1pair.py` (Sentinel-1 outline pair), `ai2point.py` (Ai2 point half-pixel shift), `rules.py` (reads
  `rules/sts_graph.json`), `fixtures.py` (fixture loaders and tallies), `score.py` (CLI summary).
  Why notes: `maritime_tip_cue/context/sts.md`.
- `maritime_tip_cue/watch/`: hot-spot run. `run.py` (CLI: passes -> detector -> shore filter -> pair check), `detector.py` + `ai2_runner.py` (Ai2 in the ai2-detectors venv; ai2_runner is re-read per scene), `s1.py`, `s2.py` (chips / windows from the detector scratch), `state.py` (SQLite run state, `findings` view, `import`, `summary`), `registry.py` (run numbers), `report.py` (run document and tiles for the app). Why notes: `maritime_tip_cue/context/watch.md`.
- `app/`: runs app, FastAPI `backend/` + React `frontend/`. Why notes: `app/context/app.md`.
- `maritime_tip_cue/structures.py`: charted offshore structures from OSM (`fetch` writes `data/structures/osm_structures.geojson`); detections within `fixed_structures.exclusion_m` are dropped.
- `maritime_tip_cue/passes.py`: S1/S2 passes over a hot spot (`--map` burns footprints). `maritime_tip_cue/land.py`: coastline from `data/land/`.
- `maritime_tip_cue/ais/match.py`: AIS parties per hull from the archive into `ais_matches` (`<run>` [--all-checked] [--stats]). Why notes: `maritime_tip_cue/context/ais.md`.
- `maritime_tip_cue/ais/archive.py`: AIS archive client (GizmoSQL; `GIZMOSQL_*` in `.env`, cert path in `config.yaml`).
- `maritime_tip_cue/hotspots.py`: loads the shared hot-spot list from S3 (`config.yaml` `s3`, bucket and keys in `.env`).
- `maritime_tip_cue/config.py`: `.env` and `config.yaml`. `db/`, `__main__.py initdb`: AIS-era scaffold, removed in stage 2.

## Rules
- `rules/sts_graph.json`: scene selection, detection cleanup, pair check, AIS matching. Values only there; unknown stays `null` with `basis: "tbd"`.

## Data (gitignored `data/`)
- `data/sts_fixtures/s1`, `data/sts_fixtures/s2`: labelled hulls and passes with their expected verdicts.
- `data/runs/<number>/`: one folder per run (`state.db`, scene windows/chips, image `cache/`); `data/runs/registry.db` numbers them. The QA review is mirrored to the tracked `reviews/<number>.json`.
- `data/earlier_runs/`: outputs of earlier runs of this method, reference only.
- Stage 0 detector bench (outside the repo): `C:\Users\Shadow\projects\ai2-detectors\` (README there).

## Docs
- `README.md`: hot spots, processing, S3 output tree, decisions. `PLAN.md`: stages and open gaps.
- `docs/feasibility.md`: measured research. Tip-era docs and rules: tag `tip-era-final`.

## Common commands (repo root)
- `.venv/Scripts/python -m pytest tests -q` fixture tests: verdicts to 1e-6 on the frozen rules (`tests/sts_graph_frozen.json`), label scores on the live rules.
- `.venv/Scripts/python -m maritime_tip_cue.sts.score` per-set STS tallies.
- `.venv/Scripts/python -m maritime_tip_cue.watch.run --site <hot spot> --end <ISO> --days 7 [--run N] [--reanalyse]` a run; a new one gets the next run number (long: launch detached with a log file; resumes from `state.db` with `--run N`). `watch.registry list|adopt` lists runs / renames finished legacy folders to their number.
- `.venv/Scripts/python -m maritime_tip_cue.ais.match <run folder>` match AIS to the after-QA pair hulls (`--all-checked --stats` for limit measurements).
- `app\start.cmd` runs app at http://127.0.0.1:8765/ (runs table, run pages, QA saved to `qa_flags`).
- `.venv/Scripts/python -m maritime_tip_cue.watch.state summary <run folder>` per-scene counts.
- `.venv/Scripts/python -m maritime_tip_cue.hotspots [publish <file>]` print (or replace) the S3 hot-spot list.

## Where work stands (2026-10-19)
- Runs 1-5 (12-19 Oct 2026): 1 and 2 fujairah_outer (2 reviewed, 1 unresolved: the moving card), 3 off_sohar (not reviewed), 4 johor_east_scs and 5 malaysia_eopl still in folders `johor_east_scs_week` / `malaysia_eopl_week`: EOPL running detached, Johor rerun queued after it (`run.log` in each). When they finish: `watch.registry adopt 4 5`, then `ais.match`, then QA in the app.
- Next stage: 6, publish from `findings` (GeoParquet per date/hot spot/source and raw captures to S3, recorded in `outputs`/`captures`).
