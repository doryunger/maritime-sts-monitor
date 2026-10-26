# Agent brief (read this instead of the full docs)

Conventions are in AGENTS.md (loaded via CLAUDE.md for Claude). Read docs by section only: `grep -n '^## ' <file>`, then
`sed -n` the range. Use `.venv/Scripts/python`. Print compact output.

## Module map
- `maritime_tip_cue/sts/`: STS pair check. `s2window.py`, `s2pair.py` (Sentinel-2 hull pair), `s1object.py`,
  `s1pair.py` (Sentinel-1 outline pair), `s1shape.py` (intensity-derived parallel single-box veto with a weaker-return guard), `confidence.py` (pair confidence score: S1 decides the verdict, S2 display-only), `ai2point.py` (Ai2 point half-pixel shift), `rules.py` (reads
  `rules/sts_graph.json`), `fixtures.py` (fixture loaders and tallies), `score.py` (CLI summary).
  Why notes: `maritime_tip_cue/context/sts.md`.
- `maritime_tip_cue/watch/`: hot-spot run. `run.py` (CLI: passes -> detector -> shore filter -> pair check), `detector.py` + `ai2_runner.py` (Ai2 in the ai2-detectors venv; ai2_runner is re-read per scene), `s1.py`, `s2.py` (chips / windows from the detector scratch), `state.py` (`Store`: Postgres run state bound to one run, `summary`, `qa-import`), `registry.py` (run rows and folders in `review.runs`), `pgutil.py` (jsonb and timestamp helpers), `report.py` (run document and tiles for the app). Why notes: `maritime_tip_cue/context/watch.md`.
- `tests/pgstore.py`: helpers that build a run with scenes, hulls and AIS rows in the `pgdb` database (`new_store`, `add_scene`, `add_match`).
- `app/`: runs app, FastAPI `backend/` + React `frontend/`. `runs.scene_rows` uses a lightweight per-run
  index; full reports load only on opening a run. Cache and refresh rationale: `app/context/app.md`.
- `maritime_tip_cue/structures.py`: charted offshore structures from OSM (`fetch` writes `data/structures/osm_structures.geojson`); detections within `fixed_structures.exclusion_m` are dropped.
- `maritime_tip_cue/passes.py`: S1/S2 passes over a hot spot (`--map` burns footprints). `maritime_tip_cue/land.py`: coastline from `data/land/`.
- `maritime_tip_cue/catalogue.py`: Lightsail catalogue scans, Postgres ledger (`processing` schema: baseline,
  job outbox, attempts), JSONL discovery logs and immutable S3 jobs. `pgledger.py` is the one-time import of the old
  `tracker.db` (`import|verify --tracker-db <file>`; copy of the live file in `data/test_inputs/`, never committed). `worker.py` and `watch/jobs.py`: explicit processing on EC2, claims, pinned manifests/rules,
  detection Parquets and completion receipts. See `maritime_tip_cue/context/catalogue.md` and
  `deploy/catalogue/README.md` for the deployed timer, commands and recovery behavior.
- `maritime_tip_cue/local_runner.py`: shared-queue terminal selection and resumable local worker processing;
  session state and logs live under ignored `data/local_runner/`. See `maritime_tip_cue/context/local_runner.md`.
- `maritime_tip_cue/publish_service.py`: `python -m maritime_tip_cue.publish_service run-pending` claims one pending `review.sync_runs` row (created by the app's Runs-page "Sync results" button, `app/backend/sync.py`, `/api/sync`) and runs `publish_sync.sync_report` with upload; started every 30 s by `deploy/app/sts-sync.timer`. See `publish.md` `## Syncing reviews`.
- `maritime_tip_cue/ais/match.py`: AIS parties per hull from the archive into selected `ais_matches` and per-slot `ais_candidates`, including boolean `is_sanctioned` (null becomes false) (`<run>` [--all-checked] [--stats]). Why notes: `maritime_tip_cue/context/ais.md`.
- `maritime_tip_cue/publish_sanctioned.py`: `single` hulls matched to a sanctioned vessel -> `sanctioned/<date>/<hot_spot>/<source>.parquet` + capture (`publish_findings`, called from `publish.publish_run`); CLI `python -m maritime_tip_cue.publish_sanctioned <runs> [--match] [--upload]`. `sanctions.entries/listed_name/programs_for` read the list by IMO and name. Rules group `sanctioned`. Why notes: `maritime_tip_cue/context/publish.md` `## Sanctioned singles`.
- `maritime_tip_cue/publish_redraw.py`: `python -m maritime_tip_cue.publish_redraw [--upload]` redraws the images of published pair rows in place (backups under `data/publish/`), no identity or Equasis step. See `publish.md` `## Redrawing published images`.
- `maritime_tip_cue/ais/archive.py`: AIS archive client (GizmoSQL; `GIZMOSQL_*` in `.env`, cert path in `config.yaml`).
- `maritime_tip_cue/hotspots.py`: loads the shared hot-spot list from S3 (`config.yaml` `s3`, bucket and keys in `.env`).
- `maritime_tip_cue/pg.py`, `pgimport.py`: the Postgres review store on the control host (`review` schema, one `runs` table plus run-keyed tables). `pg init|counts`; `pgimport import|verify --runs-root <dir>` loads the per-run SQLite files. Why notes: `maritime_tip_cue/context/store.md`; setup: `deploy/postgres/README.md`.
- `maritime_tip_cue/config.py`: `.env` and `config.yaml`. `db/`, `__main__.py initdb`: AIS-era scaffold, removed in stage 2.

## Rules
- `rules/sts_graph.json`: scene selection, detection cleanup, pair check, AIS matching. Values only there; unknown stays `null` with `basis: "tbd"`.

## Data (gitignored `data/`)
- `data/sts_fixtures/s1`, `data/sts_fixtures/s2`: labelled hulls and passes with their expected verdicts.
- `data/runs/<folder>/`: scene windows/chips and image `cache/` of a run; folder names are in `review.runs` (`<job_id[:12]>-<attempt_id[:8]>` or `legacy-run-<n>`); run state, QA flags and AIS rows are in Postgres.
- `data/earlier_runs/`: outputs of earlier runs of this method, reference only.
- Stage 0 detector bench (outside the repo): `C:\Users\Shadow\projects\ai2-detectors\` (README there).

## Docs
- `README.md`: hot spots, processing, S3 output tree, decisions. `PLAN.md`: stages and open gaps.
- `docs/feasibility.md`: measured research. Tip-era docs and rules: tag `tip-era-final`.

## Common commands (repo root)
- `.venv/Scripts/python -m pytest tests/test_sts_fixtures.py -q` fixture tests (need the PC-only `data/sts_fixtures`): verdicts to 1e-6 on the frozen rules (`tests/sts_graph_frozen.json`), label scores on the live rules.
- Tests that need Postgres (the `pgdb` fixture in `tests/conftest.py`) run in Docker on the Ubuntu host against a throwaway Postgres, never against the live database: `.venv\Scripts\python C:\Users\Shadow\projects\maritime-sts-analysis\2026-10-25-host-tests\run_on_host.py [pytest args]` (default `tests -q` without the fixture file; runs are serialised; details in the README there). A plain local `pytest` skips them.
- `.venv/Scripts/python -m maritime_tip_cue.sts.score` per-set STS tallies.
- `.venv/Scripts/python -m maritime_tip_cue.watch.run --site <hot spot> --end <ISO> --days 7 [--run N] [--reanalyse]` an experiment run (needs `--unledgered`; real work goes through the queue so the job ledger records it); a new one gets the next run number (long: launch detached with a log file; resumes from Postgres with `--run N`). `watch.registry list` lists runs.
- `.venv/Scripts/python -m maritime_tip_cue.ais.match <run no>` match AIS to the after-QA pair hulls (`--all-checked --stats` for limit measurements).
- `.venv/Scripts/python -m maritime_tip_cue.identity_seed --vessel-ids <csv> [--cache <json>] [--dry-run]` loads resolved vessel identities (and an Equasis cache) into `review.vessel_identities` and `equasis.cache`; the identity lookup reads that table.
- `.venv/Scripts/python -m maritime_tip_cue.watch.recheck <run numbers>` re-runs the S2 pair check of runs from their stored windows (after a rule change; no detector).
- `.venv/Scripts/python -m maritime_tip_cue.publish <runs> [--upload]` builds the GeoParquet and captures from the runs' after-QA pairs (dedupe across tiles and hot spots) into `data/publish/`, and uploads with `--upload`. Why notes: `maritime_tip_cue/context/publish.md`.
- `.venv/Scripts/python -m maritime_tip_cue.publish sync [--runs N ...] [--upload] [--dry-run]` brings the published Parquet in line with review verdicts (pair->single stays as `rejected`, single->pair is added, QA status updated; `publish_sync.py`); `--dry-run` prints the plan only. See `publish.md` `## Syncing reviews`.
- `app\start.cmd` runs app at http://127.0.0.1:8765/ (runs table, run pages, QA saved to `review.qa_flags`).
- `run-scenes.cmd` lists numbered shared queue jobs; `python -m maritime_tip_cue.local_runner --all` consumes ready
  jobs, `--resume` recovers the saved session, and `--status` reads local/shared state without processing.
- `.venv/Scripts/python -m maritime_tip_cue.watch.state summary <run no>` per-scene counts.
- `.venv/Scripts/python -m maritime_tip_cue.hotspots [publish <file>]` print (or replace) the S3 hot-spot list.
- `python -m maritime_tip_cue.catalogue scan|status`: discovery/logging on Lightsail; baseline is already set.
- `python -m maritime_tip_cue.worker list|drain`: inspect or process ready S3 jobs on an already-running EC2;
  `run --job-key <key> [--resume]` handles one job. No wake/stop calls. `STS_CONFIG_PATH` selects a complete
  deployment config, including Linux detector paths. Do not run GPU processing on Lightsail.

## Where work stands (2026-10-25)
- All state is in one Postgres database on the control host (`review` and `processing` schemas, see
  `maritime_tip_cue/context/store.md`); no SQLite file is read or written any more except by the one-time importers
  `pgimport` and `pgledger`. The cutover ran on 2026-10-25: 63 run databases (63 runs, 787 review flags) and the
  catalogue ledger (107 jobs, 72 attempts, 158 observations) were imported and verified, `pg check` is clean, and the
  manager, app and EC2 worker run on the Postgres-only code (commit `8c58aa4`). The old SQLite files, a Postgres dump
  and the previous images (`:pre-pg` tags for app, manager and worker) are kept on the hosts until about 2026-11-01.
- End-to-end check on EC2 (2026-10-25): one reprocessed Johor S2 scene claimed, detected (4.7 min on the T4), its run
  written to Postgres by the worker over the private network with the limited worker login, linked scan -> job ->
  attempt -> run (`pg trace --job`), review chips uploaded, nothing published (no pairs). The two hand-adopted Gulf
  catch-up jobs of 2026-10-20 are recorded as the scan event `adopted-20261020T175244Z`.
- Reviews live in Postgres (`review.qa_flags`); the app saves a flag in one transaction and answers after reading the
  stored row back. The tracked `reviews/<n>.json` files are history only.
- Tests that need Postgres run in Docker on the Ubuntu host (see Common commands). Access, certificate and backup
  notes: `deploy/postgres/README.md`. No endpoints, keys or passwords belong in Git.
- Operational priorities are the user's [catalogue tracker and EC2 workflow](../PLAN.md#current-priorities).
- AIS collection and GizmoSQL run on the dedicated Lightsail AIS/control host; connection settings are in ignored
  `data/secrets/control_host.json`. See [migration verification](../docs/feasibility.md#ais-deployment-migration-verification-2026-10-20).
