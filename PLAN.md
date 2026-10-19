# Plan

The steps to build the app described in [README.md](README.md): hot-spot passes → ships detected →
STS pair check → AIS enrichment → findings published. Update the status column as stages finish.

Each stage ends with something we run and check before moving on. Every stage covers all hot spots;
results are reported per hot spot, and one without data is named as such.

## Stages

| # | Stage | Machine | Work | Done when | Status |
|---|---|---|---|---|---|
| 0 | **Detector test** | Shadow PC ([setup](docs/feasibility.md#setup-on-windows)) | Ai2 detectors installed natively, run on recent S1 and S2 scenes; detections burned onto the images; length vs pixel size, S1/S2 agreement on anchored ships, time and memory per scene | Missed and false detections counted by eye on a crop; go / adjust / fall back | In progress: S2, S1 and the cross-check done; by-eye miss/false count and the decision left |
| 1 | **STS pair check** | Dev | Port the S1 and S2 pair check from the PoC, all parameters in `rules/sts_graph.json` | Fixture tests reproduce the PoC verdicts | Done |
| 2 | **Hot-spot list + scaffold removal** | Dev → S3 | Shared hot-spot file on S3 (`hotspots/sts_hotspots.json`: name, bbox, shore distance, basis), seeded from `vessels-aggregator` `rules/sts_pairs.json`; this repo reads it (done). Remove the AIS-era scaffold: PostGIS schema, `initdb`, psycopg, websockets, regions and coverage config | This repo loads all hot spots from S3; scaffold gone | Hot-spot list on S3 done; scaffold left |
| 3 | **Pass search** | Dev | Per hot spot, S1 and S2 scenes since a given time via STAC, with footprints and the cloud cover of the site crop. No download | For a past month, the pass list per hot spot with footprints burned onto a map and checked by eye | |
| 4 | **Detect + pair check** | GPU | Range reads of the hot-spot crop of each scene (S1 control points kept), S1 historical image cached per track; detectors, shore-distance filter, merge of overlapping detections, pair check per tanker; findings + images written locally | End to end from raw scenes gives the fixture verdicts on the fixture passes; per-hot-spot counts on a past month | |
| 5 | **AIS enrichment** | GPU ↔ ubuntu3 | Query the archive around each pass (time window, site bbox plus drift margin); project positions to capture time; match hulls by position, length, heading; record named / no AIS per hull; AIS-only STS pairs from `sts_pairs.py` as weak leads checked on the next pass. Matching parameters in a rules graph | On past passes at every hot spot, matches checked by eye on the crops; per-hot-spot match rates and unmatched pairs reported | |
| 6 | **Publish** | GPU | STS findings to `sts/<DD-MM-YYYY>/<hot_spot>/<source>.parquet` with crops; parquet schema fixed; nothing written where there is no STS | For a past month the S3 tree holds exactly the dates and hot spots with STS findings | |
| 7 | **Scheduler + GPU lifecycle** | Lightsail + GPU | Scheduler polls for new passes, starts the GPU instance and hands it the jobs; job server starts on boot, drains its queue, shuts down when idle; max-runtime watchdog | A new pass ends with findings in S3 and the instance stopped, no manual step | |
| 8 | **Viewer** | Viewer | Static site reading the `sts/` tree: dates, hot spots, findings with crop and SVG overlay | Findings browsable from S3 | |
| 9 | **Packaging + ops** | All | Containers or systemd per machine, restart policies, logs, billing alarm | Runs unattended | |

## Open gaps

| Area | Gap | Needs | By stage |
|---|---|---|---|
| Hot-spot file | Who edits it and how (now `python -m maritime_tip_cue.hotspots publish <file>`); `vessels-aggregator` still reads its own copy | Decision | 2 |
| Detector output cleanup | S2 test: one detection on an inland reservoir; one ship detected twice ~100 m apart (see feasibility) | Sea mask; merge overlapping detections | 4 |
| Whole-scene processing | Ai2's pipelines download and reproject the full scene (S1: ~250 × 170 km, ~3.5 min reprojection per image on one process), though a hot spot is ~100 × 100 km or less | Check the pipelines accept a cropped input with correct georeferencing and give the same detections as the full scene on that area | 4 |
| Ships near ports | S1 test: a 170 m ship anchored in harbour (ADAM A) was not detected. Hot spots are anchorages, some within 10 km of land (EOPL at 2 km) | Check clutter vs Ai2's near-infrastructure filter on a hot-spot scene with known anchored ships | 4 |
| S1 data source | Ai2's scene-ID mode reads AWS `sentinel-s1-l1c` (requester-pays); the test used Planetary Computer's free copies via local-file mode | Pick one for deploy: availability delay, rate limits, cost | 4 |
| AIS matching rule | Position tolerance vs report age, length and heading tolerance, what happens when two AIS ships fit one hull | Measure on past passes per hot spot | 5 |
| AIS coverage | Satellite AIS rows carry no identity or size; `johor_east_scs` is 72% satellite AIS, so "no AIS match" there often means missing identity, not a dark ship | Per-hot-spot rule for when "no AIS" counts as a signal | 5 |
| Archive access | GPU instance reaching the GizmoSQL archive on ubuntu3: network, credentials, query time per pass; archive keeps 30 days | Check from the GPU instance | 5 |
| AIS-only leads | How a lead outside the hot spots is scheduled (one-off watch area) and graded | Decision | 5 |
| GPU instance | Instance type, OS, CUDA, RAM and disk for the detectors installed natively; time per scene there (Shadow PC timings don't transfer) | Check | 7 |
| Output | S3 bucket not created; parquet columns; whether the viewer also needs vector tiles per date ([measured](docs/feasibility.md#columnar-storage-for-alerts-and-findings-measured-2026-10-12)) | AWS access; schema | 6, 8 |
| Wake + handoff | IAM for the scheduler to start/stop only the GPU instance; push vs SQS/S3 pull; boot-to-ready time | Decision + IAM policy | 7 |
| Cost guard | A stuck GPU instance runs up cost | Watchdog + billing alarm | 7 |
| Viewer | S3 static hosting (+ CloudFront) vs Lightsail; public or authenticated | Decision | 8 |
