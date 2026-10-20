# Runs app

Local web app for looking at runs and doing QA, in place of the per-run static report and QA pages.
`app/start.cmd` (or `.venv/Scripts/python -m app.backend.server` from the repo root) serves it at
http://127.0.0.1:8765/ (`config.yaml` `app`); the first start builds the frontend with npm. On the dev PC it starts
hidden at login from `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\sts-runs-app.vbs` (pythonw from the
repo venv, output to `app/server.log`); after a frontend change run `npm run build` and reload, after a backend
change end the pythonw process and run the vbs again.

## backend
`queue.py` reads shared S3 job manifests, claims and receipts on request for `/api/jobs`; it never claims or
changes jobs. Local scene status is read directly from registered SQLite runs without rendering QA reports.
Matching uses area, sensor and scene ID only, so it is advisory: older local runs can have different boundaries
or rules and cannot serve as completion receipts. A failed S3 read returns an unavailable queue, never an empty one.

QA saves resolve IDs against the complete persisted hull table, including hulls outside the sampled review cards.
The single-hull sample changes as processing adds detections or flags reserve sample slots; validating against
only the current card list rejected valid cards from an already-open page as `unknown hull`. Saving invalidates
the report cache so the corrected hull stays in the review list through its persisted flag.

FastAPI (`server.py`) over `runs.py`, which reads `data/runs/registry.db` for the run list and each run's
`state.db` per request; there is no database of its own. A run's document comes from
`maritime_tip_cue.watch.report.collect` (about 3 s for a week at one hot spot), cached in memory per run and keyed
by hull count, last scene update, AIS match and candidate counts, and invocation count, so a QA save does not rebuild it. Flags
and after-QA verdicts are read fresh on every request. Images are rendered on first request and cached as PNG in
`<run>/cache/`, named by a hash of the hull id and the cache key. A Save is validated against the hull's run
verdict (flags for measured hulls, single / pair labels for unmeasured; a correct verdict other than the
current one), then upserted into `qa_flags` and mirrored to `reviews/<run>.json`. Status: an invocation with an end
time shows its status; without one it is `running`, or `stopped` once the last event is over an hour old (a run
killed when the PC shut down).

## frontend
The `/jobs` page shows pending shared jobs and local scene matches, with an option to include completed jobs.
Refresh retrieves current S3 state. Locally running scenes also appear when the shared queue is empty.

React, Vite and TypeScript in `frontend/`; `dist/` and `node_modules/` are gitignored and rebuilt by
`start.cmd` when missing. During development `npm run dev` proxies `/api` to the server. Runs table: one row per scene, since each capture has its own timestamp and that is the time detections carry: run,
area, source (with the S2 tile or S1 orbit), captured, created (when that scene's processing finished), run status
and review status, all sortable; a row opens the run's QA filtered to that scene (`?scene=`). Scenes with no listed QA cards are hidden by default, with a checkbox to show them; failed scenes remain visible, and the checkbox choice lives in localStorage (`runs.show_empty`). The sort is in the URL (`?sort=&dir=`, so Back restores it) and in localStorage (`runs.sort`, used when the table is opened without one); no app-wide store is needed while the table is the only shared view state. Review status:
`waiting for run` while the run is going, `nothing to review` for a failed scene or one with no listed hulls,
`not started`, `in progress` (unflagged hulls left or an `unresolved` wrong verdict), `done`. Review counts come from
the cached run document, warmed in a thread when the server starts.
Run page: facts,
run vs after-QA verdict counts, and tabs for QA cards, scenes and the fixture comparison; tab and filter live in
the URL so a view can be linked. Each card is headed by its detection ID (the hull id within the scene); `&hull=<id>`
scrolls to that card and outlines it, and the card's link button copies that URL.
Pair cards number the two vessels on the raw image (1 and 2, HTML markers over the untouched image, offset away
from each other) from `vessels` in the run document, and list the selected AIS ship of each vessel below, N/A when unmatched.
Both the image markers and the details list use yellow numbers for matched vessels and white numbers for unmatched vessels.
Historical `moving` verdicts display as singles; the Ai2 speed remains visible in the card details.
After a pair-rule recheck, the app presents earlier QA choices against the current run verdict while preserving the original SQLite and review JSON records.
The expandable AIS matching evidence reads `ais_candidates` from SQLite and shows named terrestrial candidates and
their decision evidence per slot, plus counts of satellite and unnamed exclusions. This is diagnostic context, not a
vessel identity QA label; the imagery review flags remain the only user feedback stored by the app.

