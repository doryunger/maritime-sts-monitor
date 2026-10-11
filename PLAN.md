# Plan

The working plan for this app. Update it as stages finish and decisions are made.

## Goal

Listen to live AIS, raise alerts (first rule: vessel going dark), score them into incidents, then
look for the vessel and anyone involved in free satellite imagery (optical + SAR) with an
off-the-shelf ship detector. Each incident is published to S3: one bucket of incident JSON, one
bucket of images the JSON references. The website reads only S3; the background workers own the
data in S3.

## Architecture

```
aisstream WS ──> listen ──> PostGIS (vessel_state, track_points 72h, coverage, alerts)
                 engine (every 60s): rules ──> alerts ──> correlate ──> incidents
                 cue: STAC search ──> detector ──> AIS match ──> finding
                 publish ──> S3 incidents bucket (index.json, incidents/<id>.json)
                         └─> S3 images bucket (crops + overlays, keyed by incident)
web (static) ──reads──> S3
```

- **PostGIS** is the internal working store (spatial queries every 60 s, restart safety).
  **S3** (real AWS) is the published output. No API service.
- Three layers: **alert** (one rule fired) → **incident** (alerts cross-referenced and scored,
  reasons recorded) → **finding** (imagery searched, detector run, outcome incl. "no capture" and
  "nothing found").
- Containers come last, on the AWS GPU instance: one Python app image (`listen`, `engine`, `cue`),
  PostGIS, one container per detector (HTTP), web.

## Stages

Each stage ends with something we run and check before moving on.

| # | Stage | Done when | Status |
|---|---|---|---|
| — | Scaffold, `config.yaml`, PostGIS schema, `initdb` | — | Done |
| 0 | **Detector test** — run the Ai2 detectors on recent scenes over Gibraltar; burn detections with labels (length, type, heading) onto the image; then log AIS live through a satellite pass and match detections to named ships | Labelled detection images reviewed; measured detection rate by ship length, unmatched rate, time and memory per scene. Go / adjust / fall back. | Next |
| 1 | **Listener** — aisstream → PostGIS, sentinels → null, ~1 point / 30 s / vessel, `--record` + `replay` | Recorded session replays into the same rows every time | |
| 2 | **Going-dark rule** — class A, > 2 kn, reporting regularly, then silent > 2 h; coverage confidence separates dark from out-of-range | Replayed silent vessel fires an alert; one leaving coverage doesn't | |
| 3 | **Incidents → S3** — scoring with recorded reasons; boto3 S3 module; publish incident JSON + `index.json` | Incident JSON appears in S3 | |
| 4 | **Imagery + detectors** — search area = last position + sog × elapsed (capped), window [−6 h, +24 h], run stage-0 detectors, match to AIS at capture time, publish crops | Incident JSON shows detections, candidate subject, or "no capture" | |
| 5 | **Website** — static site reading S3: incident reel + detail page with crop and SVG overlay | Incidents browsable from S3 | |
| 6 | **Containers + deploy** (AWS) — app Dockerfile, compose (PostGIS + workers + detectors + web), restart policies | Runs unattended on AWS | |

Later: encounter + loitering rules, Global Fishing Watch events as a second tip source, VIIRS
coarse cue, "follow vessel" live view.

## Feasibility

**Verdict (2026-10-11): sound.** Nothing found blocks a running app. The detectors are not the weak
point; the open question is how strong the evidence is, which stage 0 measures.

| Concern | Risk | Why |
|---|---|---|
| Detectors run at all | Low | Public code and weights, maintained (Ai2, updated Sep 2026), GPU available. Unknown: 16 GB RAM on the Shadow PC; AWS is the fallback. |
| Detectors find ships | Low–medium | Sentinel-2: precision 77%, recall 79% (published). S1 and Landsat unpublished. Ships under ~25 m mostly missed at 10 m pixels; cargo ships and tankers are fine. |
| An image exists | Medium | ~2 in 3 incidents get a capture in the window (table below); the rest are "no capture". |
| **Which ship is ours** | **Main concern** | Detectors give no identity. Inferred: unmatched ship of compatible length near the projected position. Works for captures a few hours after the event; a day later or in a busy strait there can be several candidates. Ships out of AIS receiver range look like false candidates. |

### Detectors

Ai2 `rslearn_projects` covers all three sensors with the same API: `POST /detections` with a scene
ID → GeoJSON with position, length, width, heading, speed, type. No training. GPU; the Docker
images ask for 15 GB shm. Weights download from public GCS.

| Sensor | Image | Scene source | Notes |
|---|---|---|---|
| Sentinel-1 GRD (SAR) | `ghcr.io/allenai/sentinel1-vessel-detection` | Copernicus Data Space (account) | IW, VV+VH; also uses one image ~60 days earlier |
| Sentinel-2 L1C | `ghcr.io/allenai/sentinel2-vessel-detection` | Public GCS (current to 2026-10-09) | All 13 bands, baseline ≥ N0400 |
| Landsat 8/9 L1 | `ghcr.io/allenai/landsat-vessel-detection` | AWS requester-pays | All bands B1–B11 |

Dropped: xView3 (needs SNAP-preprocessed input, up to 12 min/scene); geo-dataset-generator's
`yolo11n-obb` (sub-meter optical, doesn't transfer). SAR fallback: CFAR (CPU, needs land mask).

### Imagery availability

Measured on Earth Search STAC, 30 days to 2026-10-10: share of event times with a capture inside
[−6 h, +24 h].

| Point | S1 | S2 < 30% cloud | Landsat | Any usable |
|---|---|---|---|---|
| Gibraltar strait | 46% | 44% | 12% | 75% |
| Alboran Sea | 46% | 25% | 14% | 61% |
| Bab el-Mandeb | 45% | 38% | 14% | 66% |
| S. Red Sea | 34% | 38% | 12% | 62% |
| Hormuz | 55% | 33% | 22% | 75% |
| Gulf of Oman | 38% | 29% | 24% | 67% |

S1 passes Gibraltar ~06:00 and ~18:00 UTC.

## Decisions

- Regions: Gibraltar/Alboran, Bab el-Mandeb/S. Red Sea, Hormuz (`config.yaml`).
- PostGIS as working store; real AWS S3 as published output; website reads S3 only.
- Python workers; React/Vite/TS website.
- Containers last, on the existing AWS GPU instance. The Shadow cloud PC can't run Docker (nested
  virtualization blocked, `HCS_E_HYPERV_NOT_INSTALLED`).
- MarineTraffic ruled out (internal endpoint behind Cloudflare, ToS forbids automation).

## Open gaps

| Component | Gap | Needs | By stage |
|---|---|---|---|
| Dev host | Where the DB and detectors run during development (Shadow PC natively vs AWS) | Decision | 0 |
| Image download | Landsat needs AWS credentials; S1 needs a free Copernicus account | `COPERNICUS_USERNAME` / `COPERNICUS_PASSWORD`, AWS keys in `.env` | 0 |
| AIS stream | aisstream now sends `SubscriptionConfirmation` (`CompressionEnabled`) first; volume looked low on 2026-10-11 (16 msgs / 15 s vs ~190 / 20 s earlier); no history; shore receivers only | Handle in listener; watch volume | 1 |
| Rules / scoring | Thresholds in `config.yaml` are guesses; no labelled real dark events | Reference set (e.g. GFW gap events) | 2 |
| Output | S3 buckets not created; incident JSON schema not defined | AWS access; schema | 3 |
| Search area | 24 h after at 12 kn ≈ 530 km drift vs 60 km cap | Policy: shorter window, or accept lower odds | 4 |
| Identification | Matching rule (length, heading, projected position) not defined | Define from stage 0 results | 4 |
| Website | How the site reads S3 (public bucket, CloudFront, auth) | Decision | 5 |
| AWS box | Instance type, RAM vs 15 GB shm, GPU in Docker, IAM to buckets | Check | 6 |
