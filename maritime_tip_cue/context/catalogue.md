# Catalogue tracker and processing jobs

## Discovery and baseline

The Lightsail process reads the shared hot-spot object on each scan. A successful first scan establishes
a baseline separately for every site/sensor; a failed channel must establish its baseline on a later
successful scan. Baseline observations are not queued. This avoids treating an unavailable catalogue as an
empty baseline and subsequently dispatching its historical backlog.

Discovery repeats a rolling capture-time window because catalogues can publish scenes after their capture
time. Its duration is a policy in `rules/sts_graph.json`, not a measured maximum publication delay. Scenes
arriving outside that window need a deliberate historical search. A capture-time watermark alone would miss
late arrivals. Eligible scene IDs, times, cloud values and overlap are written to each scan's JSONL log;
these are scenes passing the existing scene rules, not every raw catalogue item.

## Manifests and queue state

The Postgres ledger (`processing` schema) records observations and the dispatch outbox in the same transaction. A failed S3 write leaves a
pending job for the next poll. Job IDs hash canonical manifest content: exact scene items, the site snapshot
and the raw rules snapshot. Creation time is excluded. The `manifest_sha256` receipt field means this core
hash, which is also the job ID. Immutable conditional S3 writes detect conflicts instead of replacing jobs.

Sentinel-1 scenes are grouped by the existing radar pass identity. A late slice produces a new manifest
containing previously observed slices of that pass too, so pair checks can operate across slice boundaries.
Such expanded jobs intentionally repeat processing of earlier slices. Sentinel-2 scenes are individual jobs.
Boundary or rule changes produce a different job identity rather than mutating queued instructions.

Successful receipts must cover every requested scene and reference uploaded objects whose sizes and SHA256
metadata match. Missing objects or malformed receipts leave work incomplete. The six-hour catalogue scan only
discovers work. The always-on job manager dispatches ledger jobs to Redis and consumes Redis Stream claim,
result and lease-expiry events. The ledger stores each attempt, retry count and next eligible time. A worker renews
its claim lease; a result is accepted only from the current attempt. Expired leases become one delayed retry,
and a second unsuccessful attempt is terminal until explicitly requeued. Redis recovery and the ledger
are reconciled on each manager cycle, so a restart between either store's update can be repaired. Its
controller lock prevents overlap with a catalogue scan.

`catalogue jobs --single-scene` lists ready outbox jobs whose manifest contains exactly one scene. The
`job_manager once --job-id` path republishes only that selected manifest to Redis; it does not run a scan or
drain other pending jobs. The regular manager service remains responsible for normal full queue recovery.
`job_manager scan-one` rotates its starting site from the previous one-scene scan, establishes any missing S2
baselines without queueing historical scenes, records one previously unseen S2 scene as a one-scene manifest,
then stops scanning and dispatches only that manifest to Redis. Other new S2 scenes remain unrecorded for a
later scan-one call.

## EC2 execution

The one-scene runner dispatches one job, then delegates startup and SSH readiness to the wake status handler.
That handler updates its idle activity timestamp on each GET, so the runner keeps polling every 20 seconds while
the worker command runs. The runner checks the worker's S3 receipt-list permission before the worker claims the
Redis task; this prevents a known IAM denial from consuming a job attempt. The SSH command runs the exact job
and the runner waits for the manager's durable result. Worker connection settings stay in the ignored Lightsail
`.env`. Passing `--job-id` targets an eligible existing single-scene job through the manager's targeted `once`
command; without it, the runner scans for one new S2 scene.

The worker consumes manifests without searching the catalogue again. It stores raw pinned rules beside the
job and calls the same watch pipeline with explicit scenes. The legacy watch command can return zero after
per-scene failures, so a successful result depends on checked scene records rather than its exit code.
AIS matching retains the existing pair-only scope; singles still appear in the detection export.

The worker's local lock protects the shared detector scratch directory. Redis atomically claims a pending job,
assigns a unique attempt identity and starts a lease. A heartbeat renews the lease while processing. Result
publication is fenced by both owner and attempt identity, and artifacts use that attempt identity in their S3
prefix, so an expired worker cannot replace a later attempt's result. The manager owns completion and retry
transitions; workers never put their own jobs back in the queue. A lease timeout or reported failure consumes
the single configured retry after a delay. A second unsuccessful attempt is recorded as terminal failed and is
not automatically claimable. Rebuilding Redis state from the ledger preserves running claims, terminal jobs and
retry delays without introducing duplicate claims.

Each attempt uploads `detections.parquet` for all hull roles, `raw_detections.parquet` for detector output,
a run-state snapshot, the manifest and raw rules. Detection geometry is WKB in WGS84 longitude/latitude, with
GeoParquet metadata; empty results retain their schema. Captures/chips remain on the worker. The receipt is
written last, after uploads. A completed job returns its existing receipt on a duplicate invocation.

Artifacts use job/attempt prefixes. Writing a single scene through the existing daily STS publisher could
replace other scenes' daily files; this worker therefore does not write the canonical `sts/` partitions.
Those reviewed, pair-only products remain the responsibility of the existing publishing command.

## Deployment and credentials

The tracker runs in its Docker image under systemd; GPU packages are not loaded on Lightsail. The
systemd timer invokes a one-shot scan at 00:05, 06:05, 12:05 and 18:05 UTC, away from hourly AIS collection at
minute 15. The timer persists missed activation across reboots. The manager service restarts after failure and
host reboot. Scan history is in the ledger, JSONL and the service journal. Manager and worker have separate Docker
images and dependency lists while sharing this repository's `maritime_tip_cue` package. Their images build
from a clone; `.env`, `config.local.yaml`, and persistent `data/` stay host-local.

`STS_CONFIG_PATH` selects a complete deployment config file, relative to the repo root or absolute. The
default is `config.yaml`. An EC2 installation needs its Linux detector environment, scratch and coastline
paths configured there, plus protected S3/AIS credentials and the pinned AIS certificate. Settings and
runtime assets are local to that installation; the catalogue job supplies detection rules, not deployment
paths or credentials. No EC2 start/stop calls or remote command transport are implemented by this component.

## Worker dispatcher

Progress is measured as growth of the Redis `completed` set, not exit codes: a drain can exit 0 after only
failing jobs. Three consecutive sessions with no growth open the breaker, so a systematic fault (bad image,
unreachable archive) costs at most three bounded sessions instead of waking the instance every ten minutes
indefinitely. Termination is layered because killing the SSH client alone leaves the remote container running:
remote `timeout` plus a named container removed before and after, the local wake script killed at the session
limit, and the worker's own abort inside the container. The local launcher claims from the same Redis, so
local and EC2 work never overlap on a job; pause the dispatcher anyway to keep EC2 from being woken for jobs
the local machine is about to take. `max_seconds_per_scene` is left unset in the root config so local runs
are not killed by a GPU-sized limit.

GPU guard (2026-10-29): the worker's T4 (15.4 GB) is shared with another project's idle demo app whose process keeps its
PyTorch allocator cache (9.8 GB on 10-28, 12.5 GB on 10-29), leaving less than the detector's ~4.8 GB peak; the detector
then fails in torchvision `roi_align` and the breaker opens. With `WORKER_GPU_FREE_CONTAINER` set, the remote command
first checks free GPU memory (`WORKER_GPU_MIN_FREE_MIB`, 5376: the S2 detector's measured peak reserved memory is 4.76 GB, 4,540 MiB). If it is low and that container's last
`WORKER_GPU_IDLE_MINUTES` (15) of heartbeat log lines show no requests, no open websockets and no `ws_open`
event, with at least idle-5 heartbeats seen, the container is restarted; otherwise the session exits 75 and is recorded as
`deferred` without counting toward the breaker (it still counts toward the daily worker-seconds cap). The remote
command now stops when any pre-step fails: before, `head` was followed by `;`, so a failed preflight still ran the worker.

Scenes processed in the earlier hand-run passes (runs 2-9) are not in the catalogue ledger: 29 of the 47
scenes in the ready and failed jobs on 2026-10-22 already have a `checked` row in a local run. Rerunning them
through the queue is what writes their receipts, so the ledger and Redis stay the single record.

## Attempt verdicts and run records

Every attempt ends as exactly one of `succeeded`, `failed` or `stopped`, stored in `job_attempts.status` and
shown by `catalogue job-status`. `stopped` means the attempt did not finish by its own result: the worker
aborted on a lost lease, unreachable Redis or the runtime limit (it writes a `stopped` receipt first when the
lease is still valid), or it died and its lease expired with no receipt. A job is `failed` only after its
retry budget is spent. Whatever the verdict, the worker uploads the manifest and rules under the attempt prefix and the Parquets on success; the run's own
record (runs, scenes, hulls, events) is written to Postgres as the attempt goes, so a failed attempt keeps it
too. Runs 1-9 predate
the ledger and stay as legacy; new runs are only created through the queue (`watch.run` refuses without
`--unledgered`).

## Scenes, not jobs, are the unit of work

A job id is the hash of its manifest, which includes the rules, so a rules change makes the next scan create a
second job for every scene still unfinished (found 2026-10-22: 12 scenes with two jobs, one per rules hash).
`catalogue.supersede_jobs` runs each manager cycle and closes the extras as `superseded` with `superseded_by`
set: an open or failed job whose scenes are all covered by a succeeded job; an open job with a newer open job
over the same scenes (newest rules win); a failed job with an open or running twin. Partially overlapping
multi-scene jobs and running jobs are never touched. Superseded jobs leave Redis and are not requeued; their
attempt history stays. Failed jobs with no covering job stay failed; `requeue-failed` is for those and never
resurrects a superseded one.

`catalogue audit` is the check that the ledger is unambiguous. Every observed scene gets one state: `completed`,
`running`, `queued`, `failed`, `baseline` (seen at the first scan, never queued by design), or a violation
(`orphan`: observed after baseline but in no job; completed yet still in an open job; in more than one open
job; every job superseded). It also lists Redis drift against the ledger statuses. Observation `version` is a
hash of the site and rules, so a rules change makes a catalogue scan see every scene as new again; the
supersede step is what stops that from re-running completed scenes. Reprocessing under new rules must be a
deliberate action, not a side effect of a scan.

## Review data on S3

The worker also uploads each attempt's run folder to `catalogue/reviews/<job_id>/<attempt>/`, whatever the verdict:
`review.png`, `scenes/**` (the S1 chips and S2 windows the app renders QA cards from) and an `index.json` listing
every file with size and sha256. `cache/` and `work/` are left out, and run state is in Postgres, not in this
folder. A run folder is a few MB. The receipt carries `review` (prefix, index,
file count) or `review_error`; a failed review upload never fails the job, because the Parquets and run record are
the result and the review data can be regenerated from the run record only by rerunning. Run numbers are per
machine, so the S3 layout is keyed by job and attempt, never by run number. The app will later list attempts from
this prefix and fetch files on demand. The EC2 role needs `s3:PutObject` on `catalogue/reviews/*` (worker README).

## Postgres ledger

The ledger moved from `data/catalogue/tracker.db` to the `processing` schema of the control-host Postgres (schema in
`pg.py`; setup in `deploy/postgres/README.md`). Redis stays the task queue; every container gets `STS_DATABASE_URL`
from the same `.env`.
- One controller at a time is a session-level advisory lock (`catalogue.controller_lock`, one fixed key) on the
  connection that does the work; a second holder gets `BlockingIOError` at once, and a dropped connection frees the
  lock. This replaces the old file lock; `worker_host.manager_json` retries on that message.
- `catalogue.connect()` returns an autocommit connection and writes go through `catalogue.tx`, so each ledger
  change is one transaction. Timestamps are `timestamptz` (aware datetimes in, ISO strings out of `ledger`,
  `job_status` and `status`, so the JSON shapes the app and CLI print did not change).
- Manifests are `jsonb`. A job id is the digest of the canonical manifest, and jsonb reorders keys and normalises
  whitespace, so only canonical bytes of the value read back matter: all 107 real manifests round-trip to the same
  bytes and still match their job id and `manifest_sha256` (`tests/test_pgledger.py`).
- Event keys (`store.md`, traceability): `scan()` stamps each new observation and each job it creates with its
  `scan_id`, and writes a `job_scenes` row for every scene of the manifest in the job's own transaction. A repeat sighting
  keeps the scan that first found the observation.
- The manager handles the Redis `claimed` event with `ledger.ensure_attempt`, the same call the worker makes right
  after claiming. Whichever runs second finds the attempt and changes nothing; the manager does not pre-check for an attempt itself, because `ensure_attempt` decides under a row lock on the job.

### Import of the old tracker.db
`python -m maritime_tip_cue.pgledger import --tracker-db <file> [--dry-run]` copies the six tables in one transaction
(source read from an in-memory snapshot; existing keys are skipped, so it is idempotent) and checks that every
source key is present before commit, then derives the event keys and reports each as contained, preceding,
ambiguous or unmatched. `verify` re-checks presence, manifest bytes, `job_scenes` and counts what is unresolved.
- `scan_id` comes from the scan whose [started, ended] holds `first_discovered_utc` / `created_utc`; failing that, the
  latest earlier scan, only if it has no end or the gap is under its own duration. Never a guess: otherwise it stays
  null. On the 2026-10-25 copy 158 of 158 observations and 105 of 107 jobs fall inside a scan. The other two (fujairah_outer
  s1 and off_sohar s1, created 2026-10-20 17:52:44) are the explicit Gulf catch-up jobs adopted by hand between the
  13:20 and 18:05 scans, so no scan made them and the import leaves them null (`pg check` lists them). They are
  recorded as an event of their own: `python -m maritime_tip_cue.pgledger adopt --job <id> --job <id> --note <why>`
  creates one scan row `adopted-<UTC time of the earliest job>` (status `adopted`, `summary_json` kind `manual
  adoption` with the note and the jobs) and sets it as their `scan_id`. It refuses unknown jobs and jobs a scan
  already made, and repeating it changes nothing. Adoption is always named explicitly, never inferred from a gap.
- `review.runs.attempt_id` is set where the run folder `<job_id[:12]>-<attempt_id[:8]>` names exactly one attempt;
  none or several is reported and left null; legacy runs stay null. Run it again after the runs are imported.
  On 2026-10-25 all 54 job runs in the live review store match one attempt each (9 legacy); one succeeded attempt has
  no run yet.
