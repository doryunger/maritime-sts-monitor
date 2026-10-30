# Runs app

Local web app for looking at runs and doing QA, in place of the per-run static report and QA pages.
`app/start.cmd` serves it at http://127.0.0.1:8765/ (`config.yaml` `app`); the first start builds the frontend
with npm. On the dev PC it starts hidden at logon from the Task Scheduler task `STS runs app`
(`pythonw -m app.backend.server`, working directory the repo, restart on failure; it replaced a Startup-folder script
because Windows delays those by over a minute). The server starts on its own and
never waits for a network connection: the Jobs page reads the ledger over SSH when it is opened, and shows an error
instead of failing to start when the control host is unreachable. After a frontend change run `npm run build` and reload; after a backend change restart the app.

## backend
`ledger.py` runs the read-only `catalogue ledger` command on the control host over SSH (cached 20 s) and
`queue.py` serves it for `/api/jobs`: every job with its ledger status, superseded-by link, attempts and verdicts.
The SQLite ledger there is the only source of truth; local runs, Redis and this PC's registry are not consulted for
job state, so a local run never makes a job look done. An unreachable control host returns an error, never an
empty list. SSH details come from the ignored `data/secrets/control_host.json` (optional `project_dir`).

Image files stay on disk under `data/runs/<folder>/`; the `s3runs` sync fetches a run's review folder (scene chips and
windows, listed with sizes and sha256 in `index.json`; no database file) for each `review.runs` row that has no local
folder, from `catalogue/reviews/<job_id>/<attempt_id>/` (job through `review.run_lineage`) or
`catalogue/reviews/legacy/run-<n>/`, and never replaces a folder that exists. Nothing is registered anywhere: the run row
is already in Postgres.

QA saves resolve IDs against the complete persisted hull table, including hulls outside the sampled review cards.
The single-hull sample changes as processing adds detections or flags reserve sample slots; validating against
only the current card list rejected valid cards from an already-open page as `unknown hull`. Saving invalidates
the report cache so the corrected hull stays in the review list through its persisted flag.

FastAPI (`server.py`) over `runs.py` reads the run list from `review.runs` in Postgres (`STS_DATABASE_URL`). Scene
summaries use a separate per-run cache of minimal rows, invalidated by a Postgres stamp: one grouped query returns, per
run, the row counts and latest update times of its scenes, hulls, flags, AIS tables, invocations and events. An unchanged
run costs that one query for all runs; a changed run is re-read; record JSON, AIS candidates, chips and fixtures are not
loaded. `/api/runs` computes every run's summary with a handful of grouped queries rather than one set per run. The shared `report.select_review_hulls` function
preserves exactly the full report's review sample, including flagged singles; the index skips the same
duplicate and hull-B-box roles as the report, otherwise the two samples differ. Running/stopped status is
derived on every request so a quiet run can age into stopped without a database write.

`/api/scenes` returns an ETag and a 304 response when the visible rows have not changed. A run's detailed document
comes from `maritime_tip_cue.watch.report.collect` only when opened, cached in memory per run and keyed by the same
stamp (flag count and last flag update included), so a QA save changes the key and the flagged sample stays current. Startup does not preload detailed reports. Flags and after-QA
verdicts are read fresh on every run request. Images are rendered on first request and cached as PNG in
`<run>/cache/`, named by a hash of the hull id and the cache key. A Save is validated against the hull's run
verdict (flags for measured hulls, single / pair labels for unmeasured; a correct verdict other than the
current one), then upserted into `review.qa_flags` (with `site` and `hull`) in one transaction; the response is the row
read back after commit, with the after-QA verdict. There is no `reviews/<run>.json` mirror any more. Status: an invocation with an end
time shows its status; without one it is `running`, or `stopped` once the last event is over an hour old (a run
killed when the PC shut down).

`sync.py` and `/api/sync`: POST inserts a `pending` row in `review.sync_runs` (`requested_by` is the Basic-auth user, set
by the auth middleware as `request.state.user`; `local` without a password) unless one is pending or running, answering
409 with the current state; the check and insert share an advisory transaction lock so two clicks cannot both insert. GET returns
`{latest, active, last_finished}` (with the summary and error). The app only records the request; the work is done by
`sts-sync.timer` (see `publish.md` `## Syncing reviews`), because the app container is capped at 400 MB.

## frontend
The `/jobs` page shows pending shared jobs and local scene matches, with an option to include completed jobs.
Refresh retrieves current S3 state. Locally running scenes also appear when the shared queue is empty.

React, Vite and TypeScript in `frontend/`; `dist/` and `node_modules/` are gitignored and rebuilt by
`start.cmd` when missing. During development `npm run dev` proxies `/api` to the server. Runs table: one row per scene, since each capture has its own timestamp and that is the time detections carry: run,
area, source (with the S2 tile or S1 orbit), captured, created (when that scene's processing finished), run status
and review status, all sortable; a row opens the run's QA filtered to that scene (`?scene=`). Scenes with no listed QA cards are hidden by default, with a checkbox to show them; failed scenes remain visible, and the checkbox choice lives in localStorage (`runs.show_empty`). The sort is in the URL (`?sort=&dir=`, so Back restores it) and in localStorage (`runs.sort`, used when the table is opened without one); no app-wide store is needed while the table is the only shared view state. Review status:
`waiting for run` while the run is going, `nothing to review` for a failed scene or one with no listed hulls,
`not started`, `in progress` (unflagged hulls left or an `unresolved` wrong verdict), `done`. Review counts come from
the lightweight scene index, using the same sampling as the detailed run document. The last scene list and its
ETag are stored in localStorage (`runs.scene_index.v1`) and displayed immediately while a conditional request
checks for changes. Visible pages check every 15 seconds and on focus; an unchanged response transfers no rows.
Failed refreshes keep the cached table visible with an error message, and leaving the page aborts its request.
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
Each card shows the pair confidence next to the verdict badge; its tooltip gives the S1 threshold or says the S2 score is review-only.

The Runs page carries `SyncPanel` (button, status, confirm dialog). The dialog says the sync modifies the published
Parquets; Confirm POSTs and a 409 just shows the request already in flight. The panel polls `/api/sync` every 5 s while
a request is pending or running and every 60 s otherwise, shows elapsed time while active, and for the last finished
request its time, requester, counts per kind (added, rejected, restored, status updated, unchanged) or the error.

## Deployment on the control host

`deploy/app/` holds the image (a Node stage builds the frontend, a Python stage runs `app.backend.server`), the systemd
unit and the install steps. The app reads the ledger and every run from Postgres (`STS_DATABASE_URL` from the host `.env`); only the scene
chips come from the S3 review folders. The port stays on loopback behind Caddy, or an SSH tunnel. The development
PC reaches the same database remotely over TLS.

The review-folder sync skips a run whose attempt is still `running` and retries a folder S3 does not have yet every 30
minutes (`RETRY_MISSING_SECONDS`). The worker creates the run row when the job starts but uploads its review folder only
when the job ends; an earlier version blacklisted a run for the life of the process on the first "no such key" and so
never showed run 66's images (2026-10-25).

## Hotspot-test review

`/hotspot-tests` is served by the same authenticated app through the separate Caddy hostname. It lists versioned
test areas, run summaries and Parquet keys from the private findings prefix. S3 is accessed by the app identity;
the bucket is not public. Test run detail and flag endpoints filter out singles, unmeasured hulls and duplicate
roles, and the existing QA cards save only pair-candidate decisions. The regular runs and scene APIs exclude test
areas so their detector-only results cannot be confused with AIS-enriched findings.

The hotspot-test page shows a worker row between the map and the run table (`/api/hotspot-tests/worker`, one row in `processing.worker_status`). The idle check writes it on every 3-minute check, so it can be about 3 minutes old; the page shows the time of the reading. "Doing" is derived from the same CPU and GPU readings the idle check uses, with the limits in `rules/worker_idle.json`: GPU at or over the limit is detecting, CPU at or over the limit with the GPU quiet is preparing imagery (the download and decoding of a scene, which is all the GPU-idle time of a job: the S2 download and S1 preparation are not separable from outside the worker), both quiet is idle.
