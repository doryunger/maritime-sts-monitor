# Agent brief (read this instead of the full docs)

Conventions are in AGENTS.md (loaded via CLAUDE.md for Claude). Read docs by section only: `grep -n '^## ' <file>`, then
`sed -n` the range. Use `.venv/Scripts/python`. Print compact output.

## Module map
- `maritime_tip_cue/sts/`: STS pair check. `s2window.py`, `s2pair.py` (Sentinel-2 hull pair), `s1object.py`,
  `s1pair.py` (Sentinel-1 outline pair), `s1shape.py` (intensity-derived parallel single-box veto with a weaker-return guard), `confidence.py` (pair confidence score: S1 decides the verdict, S2 display-only), `ai2point.py` (Ai2 point half-pixel shift), `rules.py` (reads
  `rules/sts_graph.json`), `fixtures.py` (fixture loaders and tallies), `score.py` (CLI summary).
  Why notes: `maritime_tip_cue/context/sts.md`.
- `maritime_tip_cue/watch/`: hot-spot run. `run.py` (CLI: passes -> detector -> shore filter -> pair check), `detector.py` + `ai2_runner.py` (Ai2 in the ai2-detectors venv; ai2_runner is re-read per scene), `s1.py`, `s2.py` (chips / windows from the detector scratch), `state.py` (SQLite run state, `findings` view, `import`, `summary`), `registry.py` (run numbers), `report.py` (run document and tiles for the app). Why notes: `maritime_tip_cue/context/watch.md`.
- `app/`: runs app, FastAPI `backend/` + React `frontend/`. `runs.scene_rows` uses a lightweight per-run
  index; full reports load only on opening a run. Cache and refresh rationale: `app/context/app.md`.
- `maritime_tip_cue/structures.py`: charted offshore structures from OSM (`fetch` writes `data/structures/osm_structures.geojson`); detections within `fixed_structures.exclusion_m` are dropped.
- `maritime_tip_cue/passes.py`: S1/S2 passes over a hot spot (`--map` burns footprints). `maritime_tip_cue/land.py`: coastline from `data/land/`.
- `maritime_tip_cue/catalogue.py`: Lightsail catalogue scans, SQLite baseline/outbox, JSONL discovery logs and
  immutable S3 jobs. `worker.py` and `watch/jobs.py`: explicit processing on EC2, claims, pinned manifests/rules,
  detection Parquets and completion receipts. See `maritime_tip_cue/context/catalogue.md` and
  `deploy/catalogue/README.md` for the deployed timer, commands and recovery behavior.
- `maritime_tip_cue/local_runner.py`: shared-queue terminal selection and resumable local worker processing;
  session state and logs live under ignored `data/local_runner/`. See `maritime_tip_cue/context/local_runner.md`.
- `maritime_tip_cue/ais/match.py`: AIS parties per hull from the archive into selected `ais_matches` and per-slot `ais_candidates`, including boolean `is_sanctioned` (null becomes false) (`<run>` [--all-checked] [--stats]). Why notes: `maritime_tip_cue/context/ais.md`.
- `maritime_tip_cue/ais/archive.py`: AIS archive client (GizmoSQL; `GIZMOSQL_*` in `.env`, cert path in `config.yaml`).
- `maritime_tip_cue/hotspots.py`: loads the shared hot-spot list from S3 (`config.yaml` `s3`, bucket and keys in `.env`).
- `maritime_tip_cue/pg.py`, `pgimport.py`: the Postgres review store on the control host (`review` schema, one `runs` table plus run-keyed tables). `pg init|counts`; `pgimport import|verify --runs-root <dir>` loads the per-run SQLite files. Why notes: `maritime_tip_cue/context/store.md`; setup: `deploy/postgres/README.md`.
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
- `.venv/Scripts/python -m maritime_tip_cue.watch.run --site <hot spot> --end <ISO> --days 7 [--run N] [--reanalyse]` an experiment run (needs `--unledgered`; real work goes through the queue so the job ledger records it); a new one gets the next run number (long: launch detached with a log file; resumes from `state.db` with `--run N`). `watch.registry list|adopt` lists runs / renames finished legacy folders to their number.
- `.venv/Scripts/python -m maritime_tip_cue.ais.match <run folder>` match AIS to the after-QA pair hulls (`--all-checked --stats` for limit measurements).
- `.venv/Scripts/python -m maritime_tip_cue.watch.recheck <run numbers>` re-runs the S2 pair check of runs from their stored windows (after a rule change; no detector).
- `.venv/Scripts/python -m maritime_tip_cue.publish <runs> [--upload]` builds the GeoParquet and captures from the runs' after-QA pairs (dedupe across tiles and hot spots) into `data/publish/`, and uploads with `--upload`. Why notes: `maritime_tip_cue/context/publish.md`.
- `app\start.cmd` runs app at http://127.0.0.1:8765/ (runs table, run pages, QA saved to `qa_flags`).
- `run-scenes.cmd` lists numbered shared queue jobs; `python -m maritime_tip_cue.local_runner --all` consumes ready
  jobs, `--resume` recovers the saved session, and `--status` reads local/shared state without processing.
- `.venv/Scripts/python -m maritime_tip_cue.watch.state summary <run folder>` per-scene counts.
- `.venv/Scripts/python -m maritime_tip_cue.hotspots [publish <file>]` print (or replace) the S3 hot-spot list.
- `python -m maritime_tip_cue.catalogue scan|status`: discovery/logging on Lightsail; baseline is already set.
- `python -m maritime_tip_cue.worker list|drain`: inspect or process ready S3 jobs on an already-running EC2;
  `run --job-key <key> [--resume]` handles one job. No wake/stop calls. `STS_CONFIG_PATH` selects a complete
  deployment config, including Linux detector paths. Do not run GPU processing on Lightsail.

## Where work stands (2026-10-20)
- Runs 1-5 (Fujairah 9 Oct and 12-19 Oct, off_sohar, Johor, EOPL; 12-19 Oct 2026) reviewed, re-checked with the S1 and S2 shape rules, AIS-matched and published to S3 (45 findings, 8 parquet files). Johor and EOPL have one pair each; their singles are largely unreviewed.
- Expanded EOPL/Johor runs 6 and 7 finished. Gulf catch-up uses Fujairah run 8 and Sohar run 9; both explicit
  jobs were adopted into the shared tracker/outbox and claimed by their existing local workspaces. The local
  launcher's schema 2 session points to those shared jobs. Current status belongs in
  `data/local_runner/session.json`, not the superseded external survey supervisor's progress file.
  Its frozen survey manifest remains outside the repo under the earlier analysis directory. Do not launch
  overlapping detectors: scene scratch paths are shared.
- Operational priorities are now the user's [catalogue tracker and EC2 workflow](../PLAN.md#current-priorities).
- AIS collection and GizmoSQL now run on the dedicated Lightsail AIS/control host; connection settings
  are in ignored `data/secrets/control_host.json`. The AIS URI now points there, not to ubuntu3. Both local
  clients and a complete capture cycle passed. Old AIS services/files were removed from ubuntu3; unrelated
  apps remain. See [migration verification](../docs/feasibility.md#ais-deployment-migration-verification-2026-10-20)
  for the external backup and manifests. No endpoints or keys belong in Git.
- The catalogue tracker is deployed with its four-daily timer. All eight site/sensor channels were baselined
  without historical dispatch; repeat polling produced no duplicates. Expanded Malaysian bounds are now
  published in the shared list. Worker execution is implemented and its S3 handshake tested; an actual EC2
  detector run and automatic lifecycle are not verified or enabled by this delivery. External tests and
  measurements are in `docs/feasibility.md`.
