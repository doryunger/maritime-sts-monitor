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
metadata match. Missing objects or malformed receipts leave work incomplete. Failed receipts are checked
again because an explicit worker resume can replace failure with success. The tracker logs interrupted
scans on its next invocation; its local lock prevents overlapping controllers.

## EC2 execution

The EC2 command consumes manifests without searching the catalogue again. It stores raw pinned rules beside
the job and calls the same watch pipeline with explicit scenes. The legacy watch command can return zero
after per-scene failures, so acknowledgement depends on checked scene records rather than its exit code.
AIS matching retains the existing pair-only scope; singles still appear in the detection export.

The worker's local lock protects the shared detector scratch directory. A conditional S3 claim also prevents
different machines from processing the same job concurrently. Claims have no guessed timeout: failure or
interruption requires `--resume` from the original durable workspace. Moving or losing that workspace needs
operator recovery of the claim; a new worker cannot silently steal it. `drain` processes only ready jobs from
one queue snapshot and exits, without polling, automatic retries or instance lifecycle actions.

Each attempt uploads `detections.parquet` for all hull roles, `raw_detections.parquet` for detector output,
a SQLite backup, the manifest and raw rules. Detection geometry is WKB in WGS84 longitude/latitude, with
GeoParquet metadata; empty results retain their schema. Captures/chips remain on the worker. The receipt is
written last, after uploads. A completed job returns its existing receipt on a duplicate invocation.

Artifacts use job/attempt prefixes. Writing a single scene through the existing daily STS publisher could
replace other scenes' daily files; this worker therefore does not write the canonical `sts/` partitions.
Those reviewed, pair-only products remain the responsibility of the existing publishing command.

## Deployment and credentials

The tracker has a minimal dependency set under `deploy/catalogue/`; GPU packages are not loaded on Lightsail.
Its systemd timer runs at 00:05, 06:05, 12:05 and 18:05 UTC, away from hourly AIS collection at minute 15.
It persists missed timer activation across reboots. Scan history is in SQLite, JSONL and the service journal.

`STS_CONFIG_PATH` selects a complete deployment config file, relative to the repo root or absolute. The
default is `config.yaml`. An EC2 installation needs its Linux detector environment, scratch and coastline
paths configured there, plus protected S3/AIS credentials and the pinned AIS certificate. Settings and
runtime assets are local to that installation; the catalogue job supplies detection rules, not deployment
paths or credentials. No EC2 start/stop calls or remote command transport are implemented by this component.
