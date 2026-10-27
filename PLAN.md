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
3. Replace the current S3 manifest queue with hosted Redis on the Ubuntu AIS/control host. The six-hour scan
   discovers jobs into the Postgres ledger; an always-on manager dispatches them, consumes attempt events and
   maintains that ledger. Workers renew leases and publish attempt-fenced receipts. One delayed retry is allowed; a
   second failure is terminal. Redis stores active queue state, while Postgres keeps discovery and attempt history
   and S3 keeps artifacts. Tracker and GPU worker have role-specific deployment instructions and dependencies;
   both still need the shared Python package because its imports are not yet split into separately cloneable
   source trees. The manager container is deployed and reconciles attempts. The worker image claimed a synthetic
   Redis canary from EC2, rejected a duplicate claim, renewed its lease, and cleaned up the canary. The role's
   live S3 receipt-prefix preflight now passes. The worker image includes all four detector checkpoints and no
   longer mounts over their project path. One Fujairah S2 job completed end to end: Redis claim, detector run,
   five S3 artifacts and durable `succeeded` status. The T40REN detector finished in 208 seconds, selected the
   Tesla T4, and peaked at 1.49 GB PyTorch allocation. Each worker attempt now aborts on lost lease, unreachable Redis past the lease, or
   `max_seconds_per_scene` x scenes (needs a worker image rebuild); a session-bounded dispatcher (`deploy/catalogue/dispatch_worker.py`, timer every ten minutes) wakes EC2 only for ready jobs, with session, no-progress and daily caps and a dry run; it is written and unit-tested but not yet installed, and needs the worker image rebuilt first.
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
| 4 | **Detect + pair check** | GPU | Range reads of the hot-spot crop of each scene (S1 control points kept), S1 historical image cached per track; detectors, shore-distance filter, merge of overlapping detections, pair check per tanker; run state in the Postgres `review` schema (`runs`, `scenes`, `raw_detections`, `hulls`, `events`), images written locally | End to end from raw scenes gives the fixture verdicts on the fixture passes; per-hot-spot counts on a past month | fujairah_outer done for 12–19 Oct 2026: S1 fixture 193/197 and S2 fixture 73/79 agree; QA rules applied (S1 possible band, width maximum, S2 hull B). Other hot spots, range reads and sea mask left |
| 5 | **AIS enrichment** | GPU ↔ dedicated AIS host | Query the archive around each pass (time window, site bbox plus drift margin); project positions to capture time; match hulls by position, length, heading; record named / no AIS per hull; AIS-only STS pairs computed here from the archive (stopped tankers alongside, away from shore) as weak leads checked on the next pass. Matching parameters in a rules graph | On past passes at every hot spot, matches checked by eye on the crops; per-hot-spot match rates and unmatched pairs reported | |
| 6 | **Publish** | GPU | Publish every completed run's after-QA STS findings to `sts/<DD-MM-YYYY>/<hot_spot>/<source>.parquet`, with source captures under `captures/`; Parquets are the viewer product, separate from worker detection artifacts. Keep source images unmodified; publish one PNG per pair with the numbered markers drawn on it (yellow for an AIS-matched vessel, white for an unmatched one). Store the S3 path for each image in its Parquet row. Done by the worker at the end of every queue job (PNG and Parquet only, merged per date, hot spot and source; rows `unreviewed` until QA). | Every completed run with pair findings is represented in S3; each Parquet delta is identifiable and safe to apply once; raw and marker-only image paths resolve | Runs 1-9 published as one complete set on 2026-10-21: 82 findings in 12 Parquet files; all rows include raw PNG and marker-only PNG paths, and original captures remain unmarked |
| 7 | **Catalogue tracker + EC2 lifecycle** | Ubuntu AIS/control host + existing EC2 | Six-hour discovery into the Postgres ledger; manager dispatches jobs and applies attempt events. Workers renew leases and publish fenced results. One delayed retry; then terminal failure. A manual runner selects one new S2 scene or targets one eligible existing single-scene job, uses the wake status handler to start/hold EC2, then waits for durable completion. Safe automatic shutdown remains future work. | All four hot spots and both sensors; duplicate claims are prevented, silent workers time out, retry is bounded, terminal state is durable, and lifecycle behavior is validated | Manager and scan timer deployed; wake-driven handoff and S3 receipt-prefix permissions passed. One Fujairah S2 job completed end to end with five S3 artifacts and durable success. Validate both sensors and all hot spots before unattended use |
| 8 | **Viewer** | Viewer | Build and update MVT files from each newly published Parquet delta in S3; map queries read MVT files, while the unmodified source PNG or pair display PNG with only numbered 1 and 2 markers is fetched from S3 on demand | Each Parquet delta is applied once, map queries use MVT files without scanning the Parquet tree, and the image path in each finding loads on demand; verified across all four hot spots | Architecture agreed; implementation and validation remain |
| 9 | **Packaging + ops** | All | Role-specific Docker images and runtime definitions for the viewer, catalogue manager and GPU worker; preserve systemd timer/manager integration, logs and billing alarm | Each role builds and starts with persistent data and protected credentials; unattended operation is verified | Worker image bundles all four checkpoints; manager and worker images run; CUDA, Redis, role credential, S3 permission and one-scene artifact publication checks passed. Per-scene scratch is now removed and checked before advancing. Validate multi-scene disk recovery, full sensor/site coverage and lifecycle safeguards |

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
| S2 hull B on cloud and sea | Rule applied 2026-10-27 (`hull_b_alone_plateau_min_sigma`, [measurements](docs/feasibility.md#sentinel-2-hull-b-on-cloud-haze-and-sea-measured-2026-10-27)); in-sample on fujairah_outer and off_sohar, 4 of 15 rejected pairs still pair, nothing measured on johor_east_scs or malaysia_eopl | Review the next S2 runs; apply to published pairs with `publish sync` after review | 4 |
| S2 availability | A Sentinel-2 tile listed on Earth Search can be missing from the Google bucket Ai2 reads (off_sohar 19 Oct T40RDN failed); it is retried on the next run | Scheduler retries; measure the delay | 7 |
| Sanctioned singles | Singles are AIS-matched and the sanctioned ones published to `sanctioned/` ([notes](maritime_tip_cue/context/publish.md#sanctioned-singles)); implemented, unit-tested and deployed on the worker (image, config, columns and policy done 2026-10-26; smoke test passed), and the 55 existing runs were backfilled to `sanctioned/` (121 findings); no queue job has run on the new image yet. Worker checklist for a new host: [deploy/gpu-worker/README.md](deploy/gpu-worker/README.md). Not done: sanctioned vessel near a pair (the correlation step), viewer layer, sync of QA changes to `sanctioned/` | Run on a real job per hot spot, then the correlation | 6, 8 |
| AIS identity | IMO, MMSI, call sign, tonnage and build year come from the archive's `identities` view and, for the rest, from Equasis lookups triggered per pair (code copied from the aggregator, 2 s delay, [notes](maritime_tip_cue/context/equasis.md)); needs `EQUASIS_EMAIL`/`EQUASIS_PASSWORD` in the `.env` of the machine that publishes, then a backfill of already published pairs | Credentials, then backfill | 5 |
| GPU instance | Instance type, OS, CUDA, RAM and disk for the detectors installed natively; time per scene there (Shadow PC timings don't transfer) | Check | 7 |
| Redis task handoff | Lease/heartbeat, attempt fencing, one delayed retry, terminal failure and the Postgres attempt ledger are implemented; validate against deployed Redis/manager and worker before retiring legacy S3 queue objects | Deployment and end-to-end verification | 7 |
| Worker AWS identity | EC2 instance profile credentials work and live `ListBucket` preflight for `catalogue/results/` passes; the selected test job is terminal after two path-related failures | Rebuild worker with the absolute detector work paths and active-interpreter fallback; create a fresh single-scene job and verify artifact publication | 7, 9 |
| Output | S3 bucket and final Parquet schema ([MVT generation measured](docs/feasibility.md#columnar-storage-for-alerts-and-findings-measured-2026-10-12)) | AWS access; schema | 6 |
| Wake + lifecycle | Validate one-scene processing through artifact publication; then add safe unattended shutdown and a maximum-runtime guard | Worker rebuild and fresh one-scene E2E result | 7 |
| Cost guard | A stuck GPU instance runs up cost | Watchdog + billing alarm | 7 |
| Viewer | Hosting location and access policy (public or authenticated); incremental Parquet-to-MVT updates and image-fetch behavior | Decision; implementation and validation | 8 |
