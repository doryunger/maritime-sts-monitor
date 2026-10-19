# Runs app

Local web app for looking at runs and doing QA, in place of the per-run static report and QA pages.
`app/start.cmd` (or `.venv/Scripts/python -m app.backend.server` from the repo root) serves it at
http://127.0.0.1:8765/ (`config.yaml` `app`); the first start builds the frontend with npm.

## backend
FastAPI (`server.py`) over `runs.py`, which reads `data/runs/registry.db` for the run list and each run's
`state.db` per request; there is no database of its own. A run's document comes from
`maritime_tip_cue.watch.report.collect` (about 3 s for a week at one hot spot), cached in memory per run and keyed
by hull count, last scene update, AIS match count and invocation count, so a QA save does not rebuild it. Flags
and after-QA verdicts are read fresh on every request. Images are rendered on first request and cached as PNG in
`<run>/cache/`, named by a hash of the hull id and the cache key. A Save is validated against the hull's run
verdict (flags for measured hulls, single / moving / pair labels for unmeasured; a correct verdict other than the
current one), then upserted into `qa_flags` and mirrored to `reviews/<run>.json`. Status: an invocation with an end
time shows its status; without one it is `running`, or `stopped` once the last event is over an hour old (a run
killed when the PC shut down).

## frontend
React, Vite and TypeScript in `frontend/`; `dist/` and `node_modules/` are gitignored and rebuilt by
`start.cmd` when missing. During development `npm run dev` proxies `/api` to the server. Runs table: run number,
area, created (run start), captured (first and last capture), finished (processing end), status, scenes, pairs and
possible pairs after QA, reviewed of listed; sortable by run, area, created, captured, finished. Run page: facts,
run vs after-QA verdict counts, and tabs for QA cards, scenes and the fixture comparison; tab and filter live in
the URL so a view can be linked.
