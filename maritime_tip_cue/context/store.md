# Review store

## postgres
Review data for every run lives in one Postgres database (schema `review`) on the control host instead of one SQLite
file per run. A run (one worker attempt, or a run folder uploaded from the PC) is a row of `review.runs`; every other
table carries its `run_key`. `findings` keeps the SQLite view's logic and adds `run_key`.

Choices that are not obvious from the DDL:
- Flags stay run-scoped (`run_key`, `hull_id`). The same scene is often in several runs (a rules change makes a second
  job; legacy PC runs overlap host runs: 26 of 78 scenes, 1,483 of 4,200 hulls on 2026-10-25), and 79 hulls carry a
  flag in more than one run. 78 of those agree; w0_43111_18721 does not (pair in run 32, single in legacy run 6). A
  scene-level flag would have had to pick a winner, so nothing is merged yet.
- SQLite `run_id` columns are `invocation_id` here, so they cannot be mistaken for `run_key`.
- Timestamps are `timestamptz`, JSON text is `jsonb`, 0/1 columns are booleans. All timestamps in the 63 source files
  share one ISO form, so the importer raises on an unreadable one instead of storing null.
- `vessel_identities` is one global table (the SQLite copies were per run and empty on the host).
- The importer reads each source from an in-memory snapshot, writes one run per transaction and checks the row counts
  against the source before it commits. A run already present is skipped, so re-running cannot overwrite reviews.
- Remote access is TLS only with password logins; the user name is random. Credentials never go in Git.
