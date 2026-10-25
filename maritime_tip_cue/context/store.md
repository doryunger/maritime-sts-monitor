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
- The code side (`watch/state.py`, `registry.py`, `pgutil.py`): one `Store` per run, bound to `run_key`, each with its own
  autocommit connection that reconnects when the server drops it (hours-long detector runs). `pg.connect()` is the only
  way in. Timestamps leave the store as ISO strings with a `Z` (`pgutil.iso`), so reports and the HTTP API keep their
  shape; fractional seconds are normalised (the SQLite text kept whatever precision the producer wrote, e.g. `.703000Z`
  now reads `.703Z`; compared on 63 runs, nothing else differed).
- A re-check upserts hulls rather than delete and insert them, because `qa_flags` and `captures` reference hulls with
  `on delete cascade`; a delete-and-insert would silently drop the reviewers' flags. A hull that disappears but has a
  flag stays as `role = 'duplicate'`.
- Run creation takes the number first (`nextval`) and then inserts with that explicit `run_no`, so the folder name
  `legacy-run-<n>` is known at insert time. `pgimport` inserts explicit numbers too; `pg init` resets the sequence.
- The worker records the attempt before it creates the run: after the queue claim it calls `ledger.ensure_attempt`,
  commits, and polls `processing.job_attempts` for up to `worker.attempt_wait_seconds` (120, `config.yaml`; the manager
  may have raced it, or the job may not be eligible) and raises if the row never appears. Only then is the run row
  created with `attempt_id`. A re-run of the same attempt reuses its run row; a new attempt gets a new run and
  re-detects its scenes, so every derived row belongs to the attempt that produced it.
- `vessel_identities` is global; `first_run_key` is set by the run whose lookup first stored the ship.
- Remote access is TLS only with password logins; the user name is random. Credentials never go in Git.

## traceability
Rule: every row derived from an event carries that event's key, so an event can be traced to all its products.
The chain is scan (`processing.scans`) -> observation and job (`scan_id`) -> `job_scenes` -> attempt (`job_id`) ->
run (`review.runs.attempt_id`) -> scenes -> hulls -> AIS, flags, publications (`run_key`, plus composite keys to the
hull). `review.run_lineage` joins the chain for one run. Legacy PC runs have no attempt and `origin = 'legacy'`.
- `qa_flags.hull_id` stays the app's `scene__hull[__site]` id; `site` and `hull` are the real key columns, filled by
  `pg.LINKS` (which `pg init` and the importer run). On 2026-10-25 every one of the 787 flags matched exactly one hull.
- Published captures link to their hull by the id rule `<source>_<capture time>_<hull id with _ as -, dashes collapsed>`;
  all 86 findings matched one hull each. `outputs.scene_ids` (comma text) is mirrored in `output_scenes`.
- `events` carries `run_key` and `scene_id` but no foreign key: it has no `site`, and some events name no scene.
- `pg trace --run|--attempt|--job|--scan` lists every row an event produced; `pg check` fails when a table has no
  event key or no foreign key, or when a link is missing. Run it after every import or cutover.
