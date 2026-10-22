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

SQLite records observations and the dispatch outbox in the same transaction. A failed S3 write leaves a
pending job for the next poll. Job IDs hash canonical manifest content: exact scene items, the site snapshot
and the raw rules snapshot. Creation time is excluded. The `manifest_sha256` receipt field means this core
hash, which is also the job ID. Immutable conditional S3 writes detect conflicts instead of replacing jobs.

Sentinel-1 scenes are grouped by the existing radar pass identity. A late slice produces a new manifest
containing previously observed slices of that pass too, so pair checks can operate across slice boundaries.
Such expanded jobs intentionally repeat processing of earlier slices. Sentinel-2 scenes are individual jobs.
Boundary or rule changes produce a different job identity rather than mutating queued instructions.

Successful receipts must cover every requested scene and reference uploaded objects whose sizes and SHA256
metadata match. Missing objects or malformed receipts leave work incomplete. The six-hour catalogue scan only
discovers work. The always-on job manager dispatches SQLite jobs to Redis and consumes Redis Stream claim,
result and lease-expiry events. SQLite stores each attempt, retry count and next eligible time. A worker renews
its claim lease; a result is accepted only from the current attempt. Expired leases become one delayed retry,
and a second unsuccessful attempt is terminal until explicitly requeued. Redis recovery and the SQLite ledger
are reconciled on each manager cycle, so a restart between either store's update can be repaired. Its SQLite
lock prevents overlap with a catalogue scan.

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
not automatically claimable. Rebuilding Redis state from SQLite preserves running claims, terminal jobs and
retry delays without introducing duplicate claims.

Each attempt uploads `detections.parquet` for all hull roles, `raw_detections.parquet` for detector output,
a SQLite backup, the manifest and raw rules. Detection geometry is WKB in WGS84 longitude/latitude, with
GeoParquet metadata; empty results retain their schema. Captures/chips remain on the worker. The receipt is
written last, after uploads. A completed job returns its existing receipt on a duplicate invocation.

Artifacts use job/attempt prefixes. Writing a single scene through the existing daily STS publisher could
replace other scenes' daily files; this worker therefore does not write the canonical `sts/` partitions.
Those reviewed, pair-only products remain the responsibility of the existing publishing command.

## Deployment and credentials

The tracker runs in its Docker image under systemd; GPU packages are not loaded on Lightsail. The
systemd timer invokes a one-shot scan at 00:05, 06:05, 12:05 and 18:05 UTC, away from hourly AIS collection at
minute 15. The timer persists missed activation across reboots. The manager service restarts after failure and
host reboot. Scan history is in SQLite, JSONL and the service journal. Manager and worker have separate Docker
images and dependency lists while sharing this repository's `maritime_tip_cue` package. Their images build
from a clone; `.env`, `config.local.yaml`, and persistent `data/` stay host-local.

`STS_CONFIG_PATH` selects a complete deployment config file, relative to the repo root or absolute. The
default is `config.yaml`. An EC2 installation needs its Linux detector environment, scratch and coastline
paths configured there, plus protected S3/AIS credentials and the pinned AIS certificate. Settings and
runtime assets are local to that installation; the catalogue job supplies detection rules, not deployment
paths or credentials. No EC2 start/stop calls or remote command transport are implemented by this component.
