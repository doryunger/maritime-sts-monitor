# Plan

The steps to build the app described in [README.md](README.md): hot-spot passes → ships detected →
STS pair check → AIS enrichment → findings published. Update the status column as stages finish.

Each stage ends with something we run and check before moving on. Every stage covers all hot spots;
results are reported per hot spot, and one without data is named as such.

## Current priorities

The user-selected missing pieces are a catalogue tracker and execution on the designated existing EC2
instance. The user has supplied a dedicated Lightsail AIS/control host; its [preflight](docs/feasibility.md#dedicated-control-host-preflight-2026-10-20)
confirms approximately 2 GiB RAM. AIS collection and the archive are deployed there; the old AIS deployment
was removed from ubuntu3 after data and capture validation.
The S1 single-box geometry veto is implemented and validated across all four regions
([measurements](docs/feasibility.md#sentinel-1-single-box-veto-validated-2026-10-21)).

1. Keep AIS collection coverage aligned with the active hot-spot boundaries and survey overrides. The
   [local geometry audit](docs/feasibility.md#ais-collection-coverage-audit-2026-10-20) passes for all four
   sites, including expanded EOPL/Johor and the AIS query margin; the deployed dedicated-host configuration matches.
   Retain each run's boundary snapshot for AIS matching.
2. Run the catalogue tracker on the selected always-on AIS/control host four times daily. Persist discovered scene identities and processing
   state so repeated checks and restarts do not dispatch completed work again. Check all four hot spots and
   distinguish no new scenes from a catalogue request failure. Deployed at 00:05, 06:05, 12:05 and 18:05 UTC;
   baseline and repeated-scan verification passed for all four sites and both sensors.
3. Wake the designated EC2 instance when pending scenes exist, wait for worker readiness, and hand over
   explicit scene jobs with their hot-spot settings and rule version. Match geo-dataset-generator's existing
   EC2 network setup: AWS lifecycle calls, discovery of the worker's current address and an HTTP readiness
   check. The STS controller runs on the selected always-on AIS/control host. Implementation and deployment assets needed here must be
   owned by this repo; private deployment values stay outside Git. S3 manifest handoff and the explicit EC2
   consumer are implemented; automatic instance management remains outside the current delivery.
4. Execute this repo's detection, pair check, AIS enrichment and publishing on that worker. Record per-scene
   completion and failures durably so interrupted or failed work can be retried without losing completed work.
5. Stop the EC2 instance after the pending batch is finished and results are durable. Verify restart recovery,
   handoff failures and shutdown behavior, including the maximum-runtime safeguard, before unattended use.

## Stages

| # | Stage | Machine | Work | Done when | Status |
|---|---|---|---|---|---|
| 0 | **Detector test** | Shadow PC ([setup](docs/feasibility.md#setup-on-windows)) | Ai2 detectors installed natively, run on recent S1 and S2 scenes; detections burned onto the images; length vs pixel size, S1/S2 agreement on anchored ships, time and memory per scene | Missed and false detections counted by eye on a crop; go / adjust / fall back | In progress: S2, S1 and the cross-check done; by-eye miss/false count and the decision left |
| 1 | **STS pair check** | Dev | S1 and S2 pair check in `maritime_tip_cue/sts/`, all parameters in `rules/sts_graph.json` | Fixture tests reproduce the labelled verdicts | Done |
| 2 | **Hot-spot list + scaffold removal** | Dev → S3 | Shared hot-spot file on S3 (`hotspots/sts_hotspots.json`: name, bbox, shore distance, basis), this repo reads it (done). Remove the AIS-era scaffold: PostGIS schema, `initdb`, psycopg, websockets, regions and coverage config | This repo loads all hot spots from S3; scaffold gone | Hot-spot list on S3 done; scaffold left |
| 3 | **Pass search** | dedicated Lightsail | Per hot spot, S1 and S2 catalogue checks, baseline, durable scene queue and discovery logs; no image download | All sites/sensors checked, repeated polls avoid duplicates, failures and late arrivals covered | Tracker deployed; 37-record baseline, repeat scan, failure/retry tests and real S3 handoff passed |
| 4 | **Detect + pair check** | GPU | Range reads of the hot-spot crop of each scene (S1 control points kept), S1 historical image cached per track; detectors, shore-distance filter, merge of overlapping detections, pair check per tanker; run state in the run's SQLite file (`runs`, `scenes`, `raw_detections`, `hulls`, `events`), images written locally | End to end from raw scenes gives the fixture verdicts on the fixture passes; per-hot-spot counts on a past month | fujairah_outer done for 12–19 Oct 2026: S1 fixture 193/197 and S2 fixture 73/79 agree; QA rules applied (S1 possible band, width maximum, S2 hull B). Other hot spots, range reads and sea mask left |
| 5 | **AIS enrichment** | GPU ↔ dedicated AIS host | Query the archive around each pass (time window, site bbox plus drift margin); project positions to capture time; match hulls by position, length, heading; record named / no AIS per hull; AIS-only STS pairs computed here from the archive (stopped tankers alongside, away from shore) as weak leads checked on the next pass. Matching parameters in a rules graph | On past passes at every hot spot, matches checked by eye on the crops; per-hot-spot match rates and unmatched pairs reported | |
| 6 | **Publish** | GPU | STS findings to `sts/<DD-MM-YYYY>/<hot_spot>/<source>.parquet` with raw captures under `captures/`; parquet schema fixed; nothing written where there is no STS; each parquet and capture recorded in the SQLite `outputs` and `captures` tables | For a past month the S3 tree holds exactly the dates and hot spots with STS findings | Runs 1-5 (9 and 12-19 Oct 2026, all four hot spots) published from the dev PC 2026-10-19: 45 findings in 8 parquet files, duplicates across S2 tiles and the fujairah_outer / off_sohar overlap merged (`maritime_tip_cue/publish.py`). Not yet run from the GPU job |
| 7 | **Catalogue tracker + EC2 lifecycle** | dedicated Lightsail + existing EC2 | Four daily catalogue checks, durable scene jobs, explicit worker processing and uploads; later wake/readiness/stop and max-runtime watchdog | New scenes reach durable results with retries; lifecycle remains separate | Tracker deployed; S3 handoff and worker CLI implemented/tested; live EC2 processing validation and lifecycle remain |
| 8 | **Viewer** | Viewer | Static site reading the `sts/` tree: dates, hot spots, findings with crop and SVG overlay | Findings browsable from S3 | |
| 9 | **Packaging + ops** | All | Containers or systemd per machine, restart policies, logs, billing alarm | Runs unattended | |

## Open gaps

| Area | Gap | Needs | By stage |
|---|---|---|---|
| Hot-spot file | Who edits it and how (now `python -m maritime_tip_cue.hotspots publish <file>`) | Decision | 2 |
| AIS region alignment | Local and deployed collector views jointly cover all expanded survey areas and the AIS query margin; repeat coverage check when boundaries change | Boundary-change coverage check | 2, 5, 7 |
| EOPL / Johor survey | Expanded survey batch launched; compare original versus added waters after processing ([map and coverage inventory](docs/feasibility.md#eopl-and-johor-expansion-survey-prepared-2026-10-20)); expanded shared boundaries now selected for tracking | Processing and review | 3, 4 |
| Detector output cleanup | S2 test: one detection on an inland reservoir; one ship detected twice ~100 m apart (see feasibility) | Sea mask; merge overlapping detections | 4 |
| Missed S1 pairs | Strong bright-line evidence can fail combined-outline length, width and fill checks; [all-region similarity audit](docs/feasibility.md#sentinel-1-missed-pair-similarity-audit-measured-2026-10-20) provides candidates and reviewed controls | Review cue; validate independent per-vessel support before changing automatic pair rules | 4 |
| S1 shape generalisation | [Single-box veto](docs/feasibility.md#sentinel-1-single-box-veto-validated-2026-10-21) corrects the reported false pair without other verdict changes in stored runs or fixtures | Validate on independently reviewed new passes; unresolved weaker returns remain a sensing limit | 4 |
| Whole-scene processing | Ai2's pipelines download and reproject the full scene (S1: ~250 × 170 km, ~3.5 min reprojection per image on one process), though a hot spot is ~100 × 100 km or less | Check the pipelines accept a cropped input with correct georeferencing and give the same detections as the full scene on that area | 4 |
| Ships near ports | S1 test: a 170 m ship anchored in harbour (ADAM A) was not detected. Hot spots are anchorages, most within a few km of land (shore distance 2 km) | Check clutter vs Ai2's near-infrastructure filter on a hot-spot scene with known anchored ships | 4 |
| S1 data source | Ai2's scene-ID mode reads AWS `sentinel-s1-l1c` (requester-pays); the test used Planetary Computer's free copies via local-file mode | Pick one for deploy: availability delay, rate limits, cost | 4 |
| AIS matching rule | Position tolerance vs report age, length and heading tolerance, what happens when two AIS ships fit one hull. Report age stays 60 min for every ship, stationary or not (decision 2026-10-19: N/A over a stale match) | Measure length and heading tolerance per hot spot | 5 |
| AIS coverage | Satellite AIS rows carry no identity or size; `johor_east_scs` is 72% satellite AIS, so "no AIS match" there often means missing identity, not a dark ship | Per-hot-spot rule for when "no AIS" counts as a signal | 5 |
| Archive access | GPU instance reaching the GizmoSQL archive on the dedicated AIS host: network, credentials, query time per pass; archive keeps 30 days | Check from the GPU instance | 5 |
| AIS-only leads | How a lead outside the hot spots is scheduled (one-off watch area) and graded | Decision | 5 |
| S2 availability | A Sentinel-2 tile listed on Earth Search can be missing from the Google bucket Ai2 reads (off_sohar 19 Oct T40RDN failed); it is retried on the next run | Scheduler retries; measure the delay | 7 |
| AIS identity | IMO/MMSI come only from the archive's `identities` view (489 resolved ships); none of the matched Fujairah ships are resolved | Identity resolution on the vessels-aggregator side | 5 |
| GPU instance | Instance type, OS, CUDA, RAM and disk for the detectors installed natively; time per scene there (Shadow PC timings don't transfer) | Check | 7 |
| Output | S3 bucket not created; parquet columns; whether the viewer also needs vector tiles per date ([measured](docs/feasibility.md#columnar-storage-for-alerts-and-findings-measured-2026-10-12)) | AWS access; schema | 6, 8 |
| Wake + handoff | Match geo-dataset-generator's existing EC2 network setup with the STS controller on the dedicated Lightsail host; IAM limited to the designated worker; durable handoff and restart recovery | Existing private deployment settings + handoff implementation | 7 |
| Cost guard | A stuck GPU instance runs up cost | Watchdog + billing alarm | 7 |
| Viewer | S3 static hosting (+ CloudFront) vs Lightsail; public or authenticated | Decision | 8 |
