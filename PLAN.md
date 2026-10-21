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
([measurements](docs/feasibility.md#sentinel-1-single-box-veto-validated-2026-10-21)). Local scene processing and review generation are explicitly
started through `run-scenes.cmd`; the local launcher can resume the interrupted Gulf catch-up.

1. Keep AIS collection coverage aligned with the active hot-spot boundaries and survey overrides. The
   [local geometry audit](docs/feasibility.md#ais-collection-coverage-audit-2026-10-20) passes for all four
   sites, including expanded EOPL/Johor and the AIS query margin; the deployed dedicated-host configuration matches.
   Retain each run's boundary snapshot for AIS matching.
2. Run the catalogue tracker on the selected always-on AIS/control host every six hours. Persist discovered scene identities and processing
   state so repeated checks and restarts do not dispatch completed work again. Check all four hot spots and
   distinguish no new scenes from a catalogue request failure. Deployed at 00:05, 06:05, 12:05 and 18:05 UTC;
   baseline and repeated-scan verification passed for all four sites and both sensors.
3. Replace the current S3 manifest queue with hosted Redis on the Ubuntu AIS/control host. A six-hour catalogue
   scan discovers jobs into SQLite; an always-on manager dispatches pending imagery tasks, reconciles Redis
   result events into SQLite, and later can manage EC2 wake/readiness/stop. EC2 claims tasks, processes only their pinned
   scenes, then acknowledges success or leaves failures recoverable. Refreshes may replace unclaimed pending
   tasks but must retain claimed/in-progress work until acknowledgment. Redis stores the active batch, not the
   history of completed runs. Keep discovery/deduplication state in the tracker's existing SQLite database and
   keep retained artifacts and published Parquets in S3. Configure Redis persistence and restrict EC2 access to
   the required private network path. Validate restarts, retries and concurrent claims before removing S3
   manifests/claims/receipts. The manual local launcher now claims and acknowledges through the same Redis queue
   as EC2. Validate that path before retiring any legacy S3 queue-control objects. Then add EC2
   wake/readiness/stop around the Redis queue; implementation and deployment assets stay in this repo, private
   values outside Git.
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
| 6 | **Publish** | GPU | Publish every completed run's after-QA STS findings to `sts/<DD-MM-YYYY>/<hot_spot>/<source>.parquet`, with source captures under `captures/`; Parquets are the viewer product, separate from worker detection artifacts. Keep source images unmodified; for a pair with known vessel positions, publish a display copy with only numbered 1 and 2 markers. Store the S3 path for each image in its Parquet row. | Every completed run with pair findings is represented in S3; each Parquet delta is identifiable and safe to apply once; raw and marker-only image paths resolve | Runs 1-9 published as one complete set on 2026-10-21: 82 findings in 12 Parquet files; all rows include raw PNG and marker-only PNG paths, and original captures remain unmarked |
| 7 | **Catalogue tracker + EC2 lifecycle** | Ubuntu AIS/control host + existing EC2 | Six-hour catalogue discovery into SQLite; separate always-on manager dispatches jobs to Redis and reconciles result events into SQLite. EC2 and the manual local launcher claim scene tasks with pinned site/rule inputs and acknowledge completion. Refresh pending/unclaimed tasks while retaining claimed/in-progress tasks. Keep discovery/completion history in tracker SQLite and artifacts/results in S3. After validating both workers, retire legacy S3 queue-control objects. Then add EC2 wake/readiness/stop and max-runtime watchdog. | All four hot spots and both sensors; tasks survive Redis/tracker/EC2 restarts, duplicate claims are prevented, failed/in-progress work is recoverable, completed tasks leave the active queue, and S3 contains no queue-control objects | Catalogue scan, Redis Streams ACL and always-on manager deployed on Ubuntu 2026-10-21; manager active, stream group created, and SQLite reports eight dispatched plus two succeeded. EC2 worker validation, legacy S3 retirement and lifecycle remain |
| 8 | **Viewer** | Viewer | Build and update MVT files from each newly published Parquet delta in S3; map queries read MVT files, while the unmodified source PNG or pair display PNG with only numbered 1 and 2 markers is fetched from S3 on demand | Each Parquet delta is applied once, map queries use MVT files without scanning the Parquet tree, and the image path in each finding loads on demand; verified across all four hot spots | Architecture agreed; implementation and validation remain |
| 9 | **Packaging + ops** | All | Containers or systemd per machine, restart policies, logs, billing alarm | Runs unattended | |

## Open gaps

| Area | Gap | Needs | By stage |
|---|---|---|---|
| Hot-spot file | Who edits it and how (now `python -m maritime_tip_cue.hotspots publish <file>`) | Decision | 2 |
| AIS region alignment | Local and deployed collector views jointly cover all expanded survey areas and the AIS query margin; repeat coverage check when boundaries change | Boundary-change coverage check | 2, 5, 7 |
| EOPL / Johor survey | Expanded survey batch launched; compare original versus added waters after processing ([map and coverage inventory](docs/feasibility.md#eopl-and-johor-expansion-survey-prepared-2026-10-20)); expanded shared boundaries now selected for tracking | Processing and review | 3, 4 |
| Detector output cleanup | S2 test: one detection on an inland reservoir; one ship detected twice ~100 m apart (see feasibility) | Sea mask; merge overlapping detections | 4 |
| Missed S1 pairs | [All-region similarity audit](docs/feasibility.md#sentinel-1-missed-pair-similarity-audit-measured-2026-10-20) retained as research; similar signals can be smudges | Detection rules unchanged; further investigation only when requested | 4 |
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
| Redis task handoff | Manager and Streams ACL are deployed; validate worker claims, success/failure result handling, restart recovery and SQLite completion reconciliation before retiring legacy S3 queue objects | Worker end-to-end verification | 7 |
| Output | S3 bucket and final Parquet schema ([MVT generation measured](docs/feasibility.md#columnar-storage-for-alerts-and-findings-measured-2026-10-12)) | AWS access; schema | 6 |
| Wake + lifecycle | Wake EC2 when Redis has pending tasks, verify readiness, drain/acknowledge jobs, then stop safely with a maximum-runtime guard | Existing private deployment settings + lifecycle implementation | 7 |
| Cost guard | A stuck GPU instance runs up cost | Watchdog + billing alarm | 7 |
| Viewer | Hosting location and access policy (public or authenticated); incremental Parquet-to-MVT updates and image-fetch behavior | Decision; implementation and validation | 8 |
