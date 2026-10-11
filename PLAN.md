# Plan

The steps to build the app described in [README.md](README.md): tip in → imagery searched →
ships detected and matched → graded findings published. Update the status column as stages finish.

Each stage ends with something we run and check before moving on.

## Stages

| # | Stage | Machine | Work | Done when | Status |
|---|---|---|---|---|---|
| — | Scaffold | — | Package, `config.yaml`, PostGIS schema, `initdb` | — | Done (superseded in stage 1) |
| 0 | **Detector test** | Shadow PC (RTX 2000 Ada 16 GB, 16 GB RAM) | Install the Ai2 detectors natively (no Docker), run them on recent scenes over Hormuz / Gulf of Oman and the southern Red Sea, including at least one optical + SAR pair close in time. Burn detections with labels (length, type, heading) onto the images. Hand-build a few tips for ships known to be in those scenes and match by hand | Labelled detection images reviewed; detection rate by ship length, unmatched rate, time and memory per scene measured. Go / adjust / fall back | Next |
| 1 | **Tip contract + intake** | Dev → Intake host | JSON schema for the tip (`schemas/tip.schema.json`); validator with per-field reasons; example tips (valid, invalid, minimal, one from a real UKMTO/JMIC incident); intake stores tips and rejections. Remove the AIS-era scaffold: PostGIS schema, `initdb`, psycopg, websockets, `dark`/`correlate`/coverage config; set regions in `config.yaml` | Example tips validate, invalid ones are rejected with the right reasons, re-sending a tip id updates instead of duplicating | |
| 2 | **Search planning** | Dev | From a tip: time window, search area per capture time, STAC search for S1 / S2 / Landsat, optical–SAR pairing per the evidence rules. No download yet | For the example tips, the candidate scene list with footprints and search areas burned onto a map and checked by eye | |
| 3 | **Detect + match** | GPU | Download and crop the planned scenes, run the detectors from stage 0, project tip vessels to capture time, remove context vessels, match the subject and parties, grade the evidence; write finding JSON + images locally | Each example tip gives findings with the expected outcome, including `no_capture` and `nothing_found` | |
| 4 | **Publish** | GPU + Intake | Finding and image upload to S3, `tip.json` status, `index.json`; output schemas fixed | Findings for an example tip appear in S3 and `index.json` | |
| 5 | **Job handoff + GPU lifecycle** | Intake + GPU | Intake starts the GPU instance and hands it the job; job server starts on boot, drains its queue, shuts down when idle; max-runtime watchdog | A tip sent to the intake ends with results in S3 and the instance stopped, no manual step | |
| 6 | **Viewer** | Viewer | Static site: tip list + detail page with crop and SVG overlay | Tips browsable from S3 | |
| 7 | **Packaging + ops** | All | Containers or systemd per machine, restart policies, logs, billing alarm | Runs unattended | |

Later: re-run a job when a later pass lands (see gaps), VIIRS night lights as a coarse cue.

## Open gaps

| Area | Gap | Needs | By stage |
|---|---|---|---|
| Credentials | Landsat needs AWS keys (requester-pays); S1 needs a free Copernicus account | `COPERNICUS_USERNAME` / `COPERNICUS_PASSWORD`, AWS keys in `.env` | 0 |
| Shadow PC | Ai2 code targets Linux; may need fixes on native Windows | Find out at install | 0 |
| GPU instance | Instance type, OS, CUDA, RAM and disk for the detectors installed natively; time per scene there (Shadow PC timings don't transfer) | Check | 5 |
| SAR validation | Real rate of optical + SAR pairs and their gaps per region; matching tolerance; what counts as "stopped" | Measure on catalog data; tune in stage 0 | 0 |
| Tip delivery | How producers send tips: HTTP endpoint on the intake, or drop into S3 / SQS | Decision | 1 |
| Tip fields | Whether producers can supply `context_vessels` and sizes; matching quality drops without them | Check with the first producer | 1 |
| Intake host | Lightsail plan size; static IP; S3 access | Check | 1 |
| Search area | 24 h after at 12 kn ≈ 530 km drift vs the 60 km cap | Policy: shorter window, or accept lower odds | 2 |
| Identification | Matching rule (length, heading, projected position) and confidence not defined | Define from stage 0 results | 3 |
| Output | S3 buckets not created; finding and index schemas not defined | AWS access; schemas | 4 |
| Wake + handoff | IAM for the intake to start/stop only the GPU instance; push API vs SQS/S3 pull; boot-to-ready time | Decision + IAM policy | 5 |
| Later imagery | The first job usually sees only a pre-event image; post-event scenes arrive with the next pass and catalog delay | Decide: one-shot, or re-run the job at fixed re-checks | 5 |
| Cost guard | A stuck GPU instance runs up cost | Watchdog + billing alarm | 5 |
| Viewer | S3 static hosting (+ CloudFront) vs Lightsail; public or authenticated | Decision | 6 |
