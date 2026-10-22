# Local shared-queue processing

## Shared jobs and ownership

`run-scenes.cmd` selects existing ready jobs from the shared Redis queue. It never searches a catalogue,
asks for a lookback period, or invents another set of scenes. Queue counts cover every shared hot spot,
including hot spots with no jobs. Claimed and failed jobs display whether this machine retains their
original owner workspace; foreign claims cannot be selected or stolen. Interactive selection includes owned
failed/interrupted jobs as explicit recovery choices, even if New previously replaced their local session.
Noninteractive `--all` selects ready jobs only and never silently resumes claims.

Execution claims each selected Redis job with a unique owner and attempt identity, then calls `worker.run_job`
with the pinned manifest and rules. Artifacts are uploaded to an attempt-specific S3 prefix and the result
receipt is written to Redis. The Ubuntu job manager verifies successful receipts and S3 artifacts, then marks
the job succeeded in SQLite and removes it from the active pending queue. The worker renews its claim while
processing; the manager owns completion and bounded retry transitions. The launcher does not hold the worker lock itself:
`run_job` acquires it per job, so nesting that same lock around the session would deadlock. A separate
session lock protects interactive selection, session replacement and processing against other launchers.

Each entry refreshes shared queue state and validates the immutable manifest against its saved job ID,
key and site before processing. A valid success receipt skips processing through the worker's normal
idempotent path. Completed receipts remain in Redis and artifacts remain in S3 for reconciliation and audit.
Failed work returns to pending; interrupted claims require explicit operator recovery after confirming no
worker is active. There is no lease expiry or foreign-claim stealing.

## Local sessions and logs

`data/local_runner/session.json` schema 2 stores job keys, IDs, sites, local run numbers and progress.
It points to the shared manifests rather than maintaining a separate discovery plan. Reopening offers
Resume, New or Quit. New snapshots selectable shared jobs and truncates only the current `session.log` after
selection succeeds. Cancelling or finding no ready jobs retains existing state and log. Existing run
folders, reviews, worker workspaces and shared queue history remain intact.

The processing terminal is mirrored to `session.log`, including attempt times, manifest keys, Redis receipt
keys, uploaded artifact keys and failure details. Failures remain resumable while other selected jobs
continue; terminal interruption saves progress and stops. The worker's durable `run.json` preserves the
run number even when interruption prevents a receipt from being written.

Legacy schema 1 batches are readable for status but cannot be executed or replaced by this launcher.
They must finish or be explicitly adopted into the shared queue first. This guard prevents simultaneous
processing through two unrelated ownership systems.

## Commands and helpers

`run-scenes.cmd` selects numbered shared jobs or `all`. `python -m maritime_tip_cue.local_runner --all`
processes current ready jobs without prompts; it refuses to replace an unfinished local session.
`--resume` resumes the saved queue session explicitly, including owned failed/interrupted jobs.
`--status` reads local progress and current shared queue counts without processing or catalogue access.

`create_queue_session(jobs)` accepts selected queue dictionaries containing `job_key`, `job_id` and
`site`. `save_session(session, root=None)` saves it atomically. `execute_session(session, client, bucket,
root=None, resume=False)` processes its entries through the worker and returns 0 for completion, 1 for
failures, or 130 for interruption. Set `resume=True` only for explicit recovery. Programmatic callers
must hold `worker.local_lock(root)` across session creation/replacement and execution; the CLI does so.
