# maritime-sts-monitor

Watches known **ship-to-ship (STS) transfer hot spots** in free satellite imagery. On every new
Sentinel-1 or Sentinel-2 pass over a hot spot it detects the ships, checks each tanker for a second
hull alongside (a possible STS transfer), and then uses AIS to name the vessels it sees, or to show
that they were not broadcasting. AIS alone can also point to a possible STS place and time outside
the hot spots; that is a weaker lead, checked on the next pass.

## Boundary

| This repo | `vessels-aggregator` (separate) |
|---|---|
| Hot spots, imagery search, detection, STS pair check, AIS matching, AIS-only STS leads, output | AIS collection and the AIS archive server |

The only interface is the AIS archive server (GizmoSQL on the dedicated AIS/control host; views `vessels` and
`identities`), queried with `maritime_tip_cue/ais/archive.py`, plus the shared hot-spot list on S3.
No code, rules or files are shared; everything this repo needs is in it.

For AIS archive access, place its TLS certificate at `data/secrets/ais_archive_cert.pem` on each machine that runs matching. This path is ignored by Git; the archive endpoint and credentials are read from `.env`.

The staged work is in [PLAN.md](PLAN.md); measured feasibility data is in
[docs/feasibility.md](docs/feasibility.md).

## Flow

```
hot-spot list (S3, shared) ──> scheduler: new S1 / S2 passes over each hot spot (STAC)
                                   │
                                   v
                               GPU job, per pass and hot spot
                                 1. read the hot-spot crop of the scene
                                 2. detect ships, drop those near shore
                                 3. STS pair check per tanker (one hull or two)
                                 4. AIS enrichment: archive positions near the pass time,
                                    projected and matched -> vessel names, or "no AIS"
             S3 <── sts/<DD-MM-YYYY>/<hot_spot>/<source>.parquet ─┘
viewer ──map queries──> MVT files built from S3 Parquet deltas
viewer ──image requests──> S3 captures

AIS-only STS pairs (from the archive) ──> weaker lead: place and time
                                                               checked on the next pass
```

Layers: **hot spot** (a site where STS is known to take place) → **pass** (one scene over it) →
**finding** (one STS pair seen at that pass: pair verdict, AIS identity, crop).

## Hot spots

The canonical hot-spot list is kept on S3 at `hotspots/sts_hotspots.json` in the bucket named by
`S3_BUCKET_NAME` (key in `config.yaml`). AIS collection uses separately configured capture views; their
combined coverage must contain these regions and active survey extensions ([audit](docs/feasibility.md#ais-collection-coverage-audit-2026-10-20)). This repo loads the list with
`maritime_tip_cue/hotspots.py` (`python -m maritime_tip_cue.hotspots` prints it):

| Site | Current area | Prior-box availability, 15 Jul – 15 Oct 2026 |
|---|---|---|
| `malaysia_eopl` | EOPL / Singapore Strait east, 103.0–106.5 E, 0.3–1.8 N | S1 on 27 days, S2 38 clear days; 74% coastal AIS |
| `johor_east_scs` | Johor east / South China Sea, 103.5–106.5 E, 1.8–3.3 N | S1 27 days, S2 35 clear days; 28% coastal AIS, mostly satellite AIS |
| `fujairah_outer` | Outer Fujairah / Khor Fakkan, 56.3–57.2 E, 24.8–25.8 N | S1 60 days, S2 40 clear days; 76% coastal AIS |
| `off_sohar` | Gulf of Oman off Sohar, 56.6–57.6 E, 24.2–24.9 N | S1 36 days, S2 41 clear days; 44% coastal AIS |

Per site: bounding box and minimum distance from shore (2 km for all, decision 2026-10-19); detections nearer to land
are dropped.

The expanded Malaysian boxes were published on 2026-10-20. Availability and AIS percentages above were
measured on the original boxes; those measurements have not been repeated for the extensions.

## Processing

The deployed [catalogue tracker](deploy/catalogue/README.md) checks all sites every six hours and records
discoveries in SQLite. The separate always-on job manager refreshes the active task batch in private Redis and
reconciles worker results back into SQLite; the manual local launcher reads
that same queue. The initial Redis-backed scan retained two completed jobs and published eight available jobs,
all discovered after the tracker baseline. EC2 worker validation and automatic startup/shutdown remain; see
`deploy/catalogue/redis/`.

1. **Pass search**: Sentinel-1 GRD and Sentinel-2 L1C scenes covering each hot spot since the last
   run, via STAC.
2. **Detection**: Ai2 detectors (one per sensor) on the hot-spot crop. Output per ship: position,
   length, width, heading, type. Detections on land or within the site's shore distance are dropped.
3. **STS pair check** (`maritime_tip_cue/sts/`, parameters in `rules/sts_graph.json`): per tanker,
   `pair`, `single` or `unmeasured` (no `possible_pair`: weak evidence is reported as single and kept in the record). Sentinel-2 uses the hull's spectral
   width and a second-hull search beside it; Sentinel-1 uses the radar outline and two light lines.
4. **AIS enrichment**: from the AIS archive, the AIS positions in and around the
   hot spot near the pass time, projected to the capture time, matched to each hull by position,
   length and heading. Result per hull: named (name, flag, type, size; IMO and MMSI from the archive's `identities` view
   where resolved), or no AIS match. A
   pair with one or both hulls unmatched is a stronger lead, since a ship there without AIS is
   hiding. Matching parameters go in a rules graph, `null` / `basis: "tbd"` until measured.
5. **AIS-only leads**: STS candidate pairs found in the AIS archive (two stopped tankers alongside,
   away from shore) are recorded as weak leads and checked on the next pass over that place.
6. **Publish** the STS findings per date, hot spot and source (see Output).

## Output

Two kinds of data, kept apart: **run state** is local SQLite that the process needs to carry on and
to account for what it did; the **product** is GeoParquet on S3 plus the captures it points to.

### Product (S3)

Processing artifacts are under `catalogue/artifacts/<job_id>/<attempt>/`: all-hull `detections.parquet`,
`raw_detections.parquet`, a SQLite snapshot, manifest and rules. These include singles and unmeasured hulls.
They are separate from the reviewed, pair-only product below; worker attempts do not overwrite daily STS files.

```
sts/<DD-MM-YYYY>/<hot_spot>/s1.parquet
sts/<DD-MM-YYYY>/<hot_spot>/s2.parquet
captures/<DD-MM-YYYY>/<hot_spot>/<source>/<finding>[_<vessel names>].tif / .png
```

- `<DD-MM-YYYY>` is the UTC capture date; `<hot_spot>` is the site name from the hot-spot list.
- A file is written only when that source has STS findings (`pair`, after QA) at that hot
  spot on that date. No findings at a hot spot means no hot-spot folder; no STS anywhere on a date
  means no date folder. Passes with no STS, `single` and `unmeasured` hulls are not
  published.
- One row per STS finding (GeoParquet, point geometry): position, capture time, source, scene id,
  verdict and its measurements, involved parties and their vessel data where AIS matched them (name,
  flag, type, size, IMO where resolved; match distance and reasons), or no AIS, and the paths of the
  capture.
- The capture is the raw crop as the source delivered it, untouched: a GeoTIFF of the original pixel
  values (S2 L1C bands, S1 VV/VH) and a plain PNG of the same crop with a fixed stretch, no boxes or
  labels. When AIS names the vessels their names are in the file name and in the row. For confirmed pairs,
  publishing may create a separate PNG copy with only numbered 1 and 2 markers at the vessel positions; the
  source GeoTIFF and plain PNG remain unchanged. Each image path is stored in the finding row.
- Listing `sts/` gives the dates with STS; listing a date gives its hot spots. The viewer updates MVT files from
  newly published Parquet deltas and serves map queries from those tiles; it fetches the image path in the row
  from S3 when a user requests an image. For a pair, the marker-only PNG is the display image; no detection boxes
  or other overlays are used.

### Run state (SQLite)

Every process keeps its state in one SQLite file in its working folder (WAL mode, one writer, any
number of readers), the same way the earlier runs of this method did. A scene's result and its status
are written in one transaction, so a run killed mid-way (the dev PC shuts down when idle) resumes from
the last finished scene and never leaves a half-saved one.

| Table | Holds |
|---|---|
| `runs` | One row per invocation: start / end time, hot spots, time window, sources, rules hash, code commit, status |
| `scenes` | One row per scene and hot spot: source, capture time, pass, status and stage, attempts, error, detector seconds, counts (raw, kept, checked, verdicts) |
| `raw_detections` | The detector's own output per scene, before any filter |
| `hulls` | Every checked hull: position, Ai2 size and heading, verdict, measurements |
| `ais_matches` | Selected vessel assignments used by publishing and the viewer, one row per matched hull slot |
| `ais_candidates` | Per-slot AIS evidence for QA: named terrestrial candidates, rejected candidates, and satellite or unnamed exclusions with report timing, projection, distance, and comparison measures |
| `outputs` | One row per parquet written: path and S3 URI, created time, date, hot spot, source, rows, pairs, possible pairs, scene ids, checksum, upload time, run id |
| `captures` | One row per capture file: finding, local path, S3 URI, format |
| `qa_flags` | The QA review: per hull, flag, correct verdict, note, reviewer (stored in Postgres) |
| `findings` (view) | Each hull's verdict after QA, next to the run's own; what AIS matching and publishing use |
| `events` | The run log |

Only image data stays as files next to it (pair-check windows, radar chips, captures, review
sheets), with paths stored relative to the working folder. Runs are numbered: `review.runs` in Postgres maps each run number to
its folder under `data/runs`.

### Runs app

For an explicit local processing session, run `run-scenes.cmd` from this repo (or
`python -m maritime_tip_cue.local_runner`). The terminal lists the shared queue's numbered jobs; select which
to process or choose `all`. It offers to resume the latest unfinished session or start a new queue selection.
Owned interrupted/failed jobs can also be selected explicitly to resume them. Starting new resets only the
session log; previous runs and QA are retained. Processing downloads/scans the pinned scenes, matches pair AIS,
creates review outputs and uploads detection Parquets. Success receipts are written to Redis and remove
completed jobs from its active pending queue; the Ubuntu job manager verifies them and marks the durable SQLite
job record succeeded. Completed SQLite rows remain as history. There is no separate local
scene search. See
[local launcher operations](maritime_tip_cue/context/local_runner.md) for logs and recovery. Local processing is
manual; catalogue discovery does not launch local review generation.

`app/` is a local web app (FastAPI over the run databases, React frontend) that lists the runs and shows each
one: scenes, verdicts before and after QA, the fixture comparison and the QA cards with their images. QA saves
go straight into the run's `qa_flags`. Start it with `app/start.cmd`; it serves http://127.0.0.1:8765/.
## Where it runs

| Machine | Uptime | Runs |
|---|---|---|
| **Dedicated Lightsail AIS/control host** | 24/7 | Hourly AIS collection/archive, four-daily Redis-backed catalogue tracker, and Redis service; eight jobs currently available in the active queue |
| **AWS GPU instance** (existing) | Per batch | Explicit worker CLI processes assigned imagery jobs and uploads artifacts; automatic startup/shutdown remains planned |
| **S3** | — | Hot-spot list, STS findings, captures and worker artifacts |
| **Shadow cloud PC** | Dev only | Editing, git, the stage 0 detector bench |

## Sources

| Source | Access | Cost |
|---|---|---|
| Sentinel-1 GRD | Planetary Computer (free) or AWS requester-pays | Free / pennies |
| Sentinel-2 L1C | Public GCS / Earth Search STAC | Free |
| AIS | Archive server on ubuntu3 (`GIZMOSQL_*` in `.env`) | — |

## Decisions

- **Hot-spot driven, tip path dropped** (2026-10-19). Work is driven by new passes over known STS
  hot spots; the earlier design, where a tip naming one vessel started a search, is dropped.
- **One canonical hot-spot list on S3** (2026-10-19). AIS collection coverage is checked against it;
  collection views are configured separately.
- **Catalogue tracker on the dedicated Lightsail AIS/control host** (2026-10-20): four checks daily; newly available scenes trigger the
  designated existing EC2 instance to run this repo's pipeline, then stop when its pending work finishes.
  AIS collection regions must follow the same hot-spot boundaries. Findings research continues alongside
  this operational work; it is not a prerequisite for implementing it.
- **AIS by querying the archive** (2026-10-19): this repo reads the AIS archive server
  directly by time and area around each pass; it does not collect AIS.
- **Sentinel-1 and Sentinel-2 only** (2026-10-19); Landsat dropped for now.
- **Output tree** (2026-10-19): `sts/<DD-MM-YYYY>/<hot_spot>/<source>.parquet`, written only where there
  are STS findings.
- **AIS-only STS pairs are weaker** than an imagery pair verdict: they add a place and time to check.
- **Shut down when idle**, not after each job.
- **No database server of our own**: findings are GeoParquet in S3; each process keeps its run
  state in a local SQLite file (2026-10-19). The PostGIS scaffold is removed.
- Python workers; React/Vite/TS viewer.

## Code

`maritime_tip_cue/sts/`: the Sentinel-1 / Sentinel-2 STS pair check, driven by `rules/sts_graph.json`
and tested against `data/sts_fixtures` (`tests/`). The rest (`config.py`, a PostGIS schema and `initdb`) is
scaffolding from the earlier AIS-stream design, to be removed.
