# Plan

The steps to build the app described in [README.md](README.md): hot-spot passes → ships detected →
STS pair check → AIS enrichment → findings published. Update the status column as stages finish.

Each stage ends with something we run and check before moving on. Every stage covers all hot spots;
results are reported per hot spot, and one without data is named as such.

## Stages

| # | Stage | Machine | Work | Done when | Status |
|---|---|---|---|---|---|
| 0 | **Detector test** | Shadow PC ([setup](docs/feasibility.md#setup-on-windows)) | Ai2 detectors installed natively, run on recent S1 and S2 scenes; detections burned onto the images; length vs pixel size, S1/S2 agreement on anchored ships, time and memory per scene | Missed and false detections counted by eye on a crop; go / adjust / fall back | In progress: S2, S1 and the cross-check done; by-eye miss/false count and the decision left |
| 1 | **STS pair check** | Dev | S1 and S2 pair check in `maritime_tip_cue/sts/`, all parameters in `rules/sts_graph.json` | Fixture tests reproduce the labelled verdicts | Done |
| 2 | **Hot-spot list + scaffold removal** | Dev → S3 | Shared hot-spot file on S3 (`hotspots/sts_hotspots.json`: name, bbox, shore distance, basis), this repo reads it (done). Remove the AIS-era scaffold: PostGIS schema, `initdb`, psycopg, websockets, regions and coverage config | This repo loads all hot spots from S3; scaffold gone | Hot-spot list on S3 done; scaffold left |
| 3 | **Pass search** | Dev | Per hot spot, S1 and S2 scenes since a given time via STAC, with footprints and the cloud cover of the site crop. No download | For a past month, the pass list per hot spot with footprints burned onto a map and checked by eye | |
| 4 | **Detect + pair check** | GPU | Range reads of the hot-spot crop of each scene (S1 control points kept), S1 historical image cached per track; detectors, shore-distance filter, merge of overlapping detections, pair check per tanker; run state in the run's SQLite file (`runs`, `scenes`, `raw_detections`, `hulls`, `events`), images written locally | End to end from raw scenes gives the fixture verdicts on the fixture passes; per-hot-spot counts on a past month | fujairah_outer done for 12–19 Oct 2026: S1 fixture 193/197 and S2 fixture 73/79 agree; QA rules applied (S1 possible band, width maximum, S2 hull B). Other hot spots, range reads and sea mask left |
| 5 | **AIS enrichment** | GPU ↔ ubuntu3 | Query the archive around each pass (time window, site bbox plus drift margin); project positions to capture time; match hulls by position, length, heading; record named / no AIS per hull; AIS-only STS pairs computed here from the archive (stopped tankers alongside, away from shore) as weak leads checked on the next pass. Matching parameters in a rules graph | On past passes at every hot spot, matches checked by eye on the crops; per-hot-spot match rates and unmatched pairs reported | |
| 6 | **Publish** | GPU | STS findings to `sts/<DD-MM-YYYY>/<hot_spot>/<source>.parquet` with raw captures under `captures/`; parquet schema fixed; nothing written where there is no STS; each parquet and capture recorded in the SQLite `outputs` and `captures` tables | For a past month the S3 tree holds exactly the dates and hot spots with STS findings | Runs 1-5 (9 and 12-19 Oct 2026, all four hot spots) published from the dev PC 2026-10-19: 45 findings in 8 parquet files, duplicates across S2 tiles and the fujairah_outer / off_sohar overlap merged (`maritime_tip_cue/publish.py`). Not yet run from the GPU job |
| 7 | **Scheduler + GPU lifecycle** | Lightsail + GPU | Scheduler polls for new passes, starts the GPU instance and hands it the jobs; job server starts on boot, drains its queue, shuts down when idle; max-runtime watchdog | A new pass ends with findings in S3 and the instance stopped, no manual step | |
| 8 | **Viewer** | Viewer | Static site reading the `sts/` tree: dates, hot spots, findings with crop and SVG overlay | Findings browsable from S3 | |
| 9 | **Packaging + ops** | All | Containers or systemd per machine, restart policies, logs, billing alarm | Runs unattended | |

## Open gaps

| Area | Gap | Needs | By stage |
|---|---|---|---|
| Hot-spot file | Who edits it and how (now `python -m maritime_tip_cue.hotspots publish <file>`) | Decision | 2 |
| Detector output cleanup | S2 test: one detection on an inland reservoir; one ship detected twice ~100 m apart (see feasibility) | Sea mask; merge overlapping detections | 4 |
| Whole-scene processing | Ai2's pipelines download and reproject the full scene (S1: ~250 × 170 km, ~3.5 min reprojection per image on one process), though a hot spot is ~100 × 100 km or less | Check the pipelines accept a cropped input with correct georeferencing and give the same detections as the full scene on that area | 4 |
| Ships near ports | S1 test: a 170 m ship anchored in harbour (ADAM A) was not detected. Hot spots are anchorages, most within a few km of land (shore distance 2 km) | Check clutter vs Ai2's near-infrastructure filter on a hot-spot scene with known anchored ships | 4 |
| S1 data source | Ai2's scene-ID mode reads AWS `sentinel-s1-l1c` (requester-pays); the test used Planetary Computer's free copies via local-file mode | Pick one for deploy: availability delay, rate limits, cost | 4 |
| AIS matching rule | Position tolerance vs report age, length and heading tolerance, what happens when two AIS ships fit one hull. Report age stays 60 min for every ship, stationary or not (decision 2026-10-19: N/A over a stale match) | Measure length and heading tolerance per hot spot | 5 |
| AIS coverage | Satellite AIS rows carry no identity or size; `johor_east_scs` is 72% satellite AIS, so "no AIS match" there often means missing identity, not a dark ship | Per-hot-spot rule for when "no AIS" counts as a signal | 5 |
| Archive access | GPU instance reaching the GizmoSQL archive on ubuntu3: network, credentials, query time per pass; archive keeps 30 days | Check from the GPU instance | 5 |
| AIS-only leads | How a lead outside the hot spots is scheduled (one-off watch area) and graded | Decision | 5 |
| S2 availability | A Sentinel-2 tile listed on Earth Search can be missing from the Google bucket Ai2 reads (off_sohar 19 Oct T40RDN failed); it is retried on the next run | Scheduler retries; measure the delay | 7 |
| AIS identity | IMO/MMSI come only from the archive's `identities` view (489 resolved ships); none of the matched Fujairah ships are resolved | Identity resolution on the vessels-aggregator side | 5 |
| GPU instance | Instance type, OS, CUDA, RAM and disk for the detectors installed natively; time per scene there (Shadow PC timings don't transfer) | Check | 7 |
| Output | S3 bucket not created; parquet columns; whether the viewer also needs vector tiles per date ([measured](docs/feasibility.md#columnar-storage-for-alerts-and-findings-measured-2026-10-12)) | AWS access; schema | 6, 8 |
| Wake + handoff | IAM for the scheduler to start/stop only the GPU instance; push vs SQS/S3 pull; boot-to-ready time | Decision + IAM policy | 7 |
| Cost guard | A stuck GPU instance runs up cost | Watchdog + billing alarm | 7 |
| Viewer | S3 static hosting (+ CloudFront) vs Lightsail; public or authenticated | Decision | 8 |
