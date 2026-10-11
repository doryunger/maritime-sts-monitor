# Plan

The working plan for this app. Update it as stages finish and decisions are made.

## Goal

Listen to live AIS, raise alerts (first rule: vessel going dark), score them into incidents, then
look for the vessel and anyone involved in free satellite imagery (optical + SAR) with an
off-the-shelf ship detector. Each incident is published to S3: one bucket of incident JSON, one
bucket of images the JSON references. The viewer reads only S3.

## Where each component runs

| Machine | Uptime | Runs |
|---|---|---|
| **Lightsail** (existing, cheap) | 24/7 | AIS listener (continuous), PostGIS, rules engine, incident scoring, waking the GPU on a new incident, sending the job, publishing incident JSON |
| **AWS GPU instance** (existing) | Only per job | Job server that starts on boot; finds the scene(s) for the job's area and time, downloads, runs detectors, matches to the parties in the job, uploads results to S3, then shuts itself down |
| **Viewer host** (Lightsail, or none) | 24/7 | Static incident viewer reading S3 |
| **S3** | — | Incidents bucket (JSON) + images bucket |
| **Shadow cloud PC** | Dev only | Editing, git. Can't run Docker (nested virtualization blocked) |

```
                      LIGHTSAIL (24/7)                                  AWS GPU (on demand)
aisstream ──> listen ──> PostGIS
              engine (60s): rules ─> alerts ─> incidents ─> S3 incident.json
              new incident ──> start instance ──────> boot: job server
                               └──> job ────────────> find scene, download
                                                                       detect, crop
                                                                       match to parties
                                  S3 <── findings + images ───────────  upload
                                                                       shutdown when idle
VIEWER (static) ──reads──> S3
```

Three data layers: **alert** (one rule fired) → **incident** (alerts cross-referenced and scored,
reasons recorded) → **finding** (imagery searched, detector run, outcome incl. "no capture" and
"nothing found").

### Lifecycle of one incident

1. Lightsail listens to AIS continuously and **documents every alert and incident** in PostGIS,
   reported or not. This history is what later incidents are cross-referenced against (repeat
   offenders, other vessels dark nearby in space and time).
2. Each new incident is analyzed against that history and scored, with the reasons recorded. Below
   threshold, it stays as history only.
3. **Worth reporting →** start the evidence chain: Lightsail publishes `incident.json` and wakes
   the GPU with a job.
4. The GPU server takes the latest capture of the area, even one from before the event, and
   publishes a finding labelled with its capture time relative to the event (e.g. "9 h before;
   no capture after the incident yet"). A before-image shows the vessel while it still had AIS,
   which also tells us how the detector sees it.
5. Every job writes an outcome, including `nothing_found`, `no_capture` and `failed`.
6. GPU server shuts the instance down when its queue is empty; a hard max-runtime watchdog shuts
   it down regardless.
7. Lightsail updates the incident status and `index.json`.

Open: scenes acquired after the first job (next satellite pass) are not picked up unless
something triggers another job. See gaps.

### Proposed design points (need sign-off)

- **Split of search duties (agreed).** Lightsail listens to AIS and decides an incident is worth a
  look. The GPU server does the scene search, download, detection and matching for the area and
  time it's given.
- **One writer per S3 object.** Lightsail owns `incidents/<id>/incident.json` and `index.json`; the
  GPU owns `incidents/<id>/findings/<scene>.json` and the images. No read-modify-write races.
- **Job payload.** Search area, time window, the subject vessel and its properties (MMSI, name,
  length, type, last position, sog/cog), other involved parties, and AIS tracks of all vessels in
  the area over the window, so the GPU matches detections without reaching into Lightsail's DB.
- **Shut down when idle, not after each job.** Several incidents can queue while the instance is
  up; it stops after N minutes with an empty queue.
- **Job handoff: push vs pull.** As specified, Lightsail calls an API on the GPU instance. This
  needs a reachable address (public IP changes per start unless an Elastic IP is attached), a
  security group open only to Lightsail's static IP, and an auth token. Alternative: Lightsail
  drops the job in SQS or S3 and the GPU pulls it on boot; no inbound port, no address or
  readiness wait. To decide.
- **Viewer may need no machine.** A static site can be served straight from S3 (+ CloudFront).
  Lightsail is the alternative if we want it behind our own server.

### Evidence rules

Evidence uses two independent sources: **optical** (Sentinel-2 or Landsat) to see the object, and
**SAR** (Sentinel-1) to validate that it is a real hard target and not a wake, cloud or whitecap.

1. **Pick the closest pair.** Among the available optical captures (Sentinel-2 *and* Landsat), take
   the one closest in time to a SAR pass over the area.
2. **Validate only if the gap allows it.** A ship moves during the gap between the two captures.
   SAR validation counts only if `speed × gap` stays within the matching tolerance (position and
   length must agree).
3. **Stopped ships are the exception.** If the ship is known to be stationary (near-zero last
   speed, loitering, alongside another vessel), a longer gap is acceptable.
4. **Otherwise drop SAR validation** and report optical only.

Evidence grades shown on the incident:

| Grade | Meaning |
|---|---|
| **Confirmed** | Optical and SAR agree on position (after allowing for movement) and length |
| **SAR only** | Real hard target; no usable optical |
| **Optical only** | Seen, not validated; could be a false target |
| **None** | No capture, or nothing found |

Timing caveat: Sentinel-2 and Landsat both pass around 10:00–11:00 local time; Sentinel-1 passes
at different hours (e.g. Hormuz ~02:00 and ~14:00 UTC, Bab el-Mandeb ~02:00 and ~15:00 UTC). Landsat adds chances, not a
different time of day, so typical optical–SAR gaps are several hours. Rough estimate of both
sources inside the window: 15–20% of incidents (assumes independence; not yet measured).

## Stages

Each stage ends with something we run and check before moving on.

| # | Stage | Machine | Done when | Status |
|---|---|---|---|---|
| — | Scaffold, `config.yaml`, PostGIS schema, `initdb` | — | — | Done |
| 0 | **Detector test** — run the Ai2 detectors on recent scenes over Hormuz / Gulf of Oman and the southern Red Sea; burn detections with labels (length, type, heading) onto the image; include at least one optical + SAR pair close in time; then log AIS live through a satellite pass and match detections to named ships | GPU | Labelled detection images reviewed; measured detection rate by ship length, unmatched rate, time and memory per scene. Go / adjust / fall back. | Next |
| 1 | **Listener** — aisstream → PostGIS, sentinels → null, ~1 point / 30 s / vessel, `--record` + `replay` | Lightsail | Recorded session replays into the same rows every time | |
| 2 | **Going-dark rule** — class A, > 2 kn, reporting regularly, then silent > 2 h; coverage confidence separates dark from out-of-range | Lightsail | Replayed silent vessel fires an alert; one leaving coverage doesn't | |
| 3 | **Incidents → S3** — scoring with recorded reasons; publish `incident.json` + `index.json` | Lightsail | Incident JSON appears in S3 | |
| 4 | **Dispatch** — on a new incident, start the GPU instance and send the job | Lightsail | A new incident boots the GPU and delivers a job | |
| 5 | **GPU job server** — starts on boot, finds the optical and SAR scenes, pairs them per the evidence rules, runs detectors from stage 0, matches to the job's parties, grades the evidence, uploads findings + images with capture time relative to the event, idle shutdown + watchdog | GPU | Job in → results in S3 → instance stopped, with no manual step | |
| 6 | **Viewer** — static site reading S3: incident reel + detail page with crop and SVG overlay | Viewer | Incidents browsable from S3 | |
| 7 | **Packaging + ops** — containers or systemd per machine, restart policies, logs, cost alarm | All | Runs unattended | |

Later: encounter + loitering rules, Global Fishing Watch events as a second tip source, VIIRS
coarse cue, "follow vessel" live view.

## Feasibility

**Verdict (2026-10-11): sound.** Nothing found blocks a running app. The detectors are not the weak
point; the open question is how strong the evidence is, which stage 0 measures.

| Concern | Risk | Why |
|---|---|---|
| Detectors run at all | Low | Public code and weights, maintained (Ai2, updated Sep 2026), GPU instance available. |
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

**Resolution.** Each detector is trained on its own source at native resolution, so they work
with these sources; resolution limits minimum ship size and length accuracy. Estimates, to be
measured in stage 0 against AIS-reported lengths:

| Source | Pixel | Smallest reliable ship (est.) | Effect |
|---|---|---|---|
| Sentinel-2 | 10 m | ~20–25 m | Length ± ~10–20 m |
| Landsat | 15 m pan / 30 m | ~40–50 m | Large ships only; coarse length |
| Sentinel-1 | ~10 m pixel, ~20 m resolution | ~15–25 m | Metal hulls bright, small ships visible; length often overstated by sidelobes |

Target ships (tankers, cargo, 100 m+) are well within range on all three; small boats are missed
or marginal. Optical–SAR length agreement needs a wide, source-specific tolerance.

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

S1 passes (UTC): Gibraltar ~06/18, Bab el-Mandeb ~02/15, Hormuz ~02/14. Gibraltar rows are kept as
reference only; Suez / northern Red Sea and the Arabian Sea are not measured yet.

## Decisions

- Regions (2026-10-11): focus on the Middle East, where recent incidents are (Hormuz rated
  SEVERE after tanker strikes in July 2026; attacks off Yemen and a tanker seizure in the Gulf of
  Aden in Aug 2026, per UKMTO/JMIC). Gibraltar dropped for now. `config.yaml` to be updated at
  stage 1:

  | Region | Covers | Approx. bbox (lon, lat) |
  |---|---|---|
  | `suez_red_sea_north` | Suez Canal, Gulf of Suez, northern Red Sea | 32.0–35.5 E, 27.0–31.5 N |
  | `bab_el_mandeb_aden` | Southern Red Sea, Bab el-Mandeb, Gulf of Aden | 41.0–51.5 E, 10.5–16.5 N |
  | `hormuz_oman` | Eastern Persian Gulf, Strait of Hormuz, Gulf of Oman | 54.0–60.0 E, 22.5–27.5 N |
  | `arabian_sea` | NW Indian Ocean approaches | 55.0–66.0 E, 12.0–22.0 N |
- Machine split: Lightsail 24/7 for listening and incident logic; GPU instance on demand for
  imagery + detectors, self-shutdown; viewer reads S3.
- PostGIS as working store (on Lightsail); real AWS S3 as published output.
- Python workers; React/Vite/TS viewer.
- MarineTraffic ruled out (internal endpoint behind Cloudflare, ToS forbids automation).

## Open gaps

| Area | Gap | Needs | By stage |
|---|---|---|---|
| AIS coverage | aisstream relies on shore receivers; coverage along Yemen, Somalia, Iran and the open Arabian Sea may be thin or absent. Without coverage, "went dark" can't be told from "out of range" | Measure message density per region before stage 1; may need a satellite-AIS or GFW source for the Arabian Sea | 0 |
| Credentials | Landsat needs AWS keys (requester-pays); S1 needs a free Copernicus account | `COPERNICUS_USERNAME` / `COPERNICUS_PASSWORD`, AWS keys in `.env` | 0 |
| GPU instance | Instance type, RAM vs 15 GB shm request, GPU usable from Docker or natively, disk for scenes (S1 GRD ~1.7 GB, S2 L1C ~0.7 GB) | Check | 0 |
| Lightsail | Plan size (RAM/disk) for PostGIS with 72 h of tracks for three regions; static IP | Check | 1 |
| AIS stream | aisstream now sends `SubscriptionConfirmation` (`CompressionEnabled`) first; volume looked low on 2026-10-11 (16 msgs / 15 s vs ~190 / 20 s earlier); no history; shore receivers only | Handle in listener; watch volume | 1 |
| Rules / scoring | Thresholds in `config.yaml` are guesses; no labelled real dark events | Reference set (e.g. GFW gap events) | 2 |
| Output | S3 buckets not created; `incident.json` / `finding.json` schemas not defined | AWS access; schemas | 3 |
| Search area | 24 h after at 12 kn ≈ 530 km drift vs 60 km cap | Policy: shorter window, or accept lower odds | 4 |
| Wake + handoff | IAM for Lightsail to start/stop only the GPU instance; push API vs SQS/S3 pull; GPU boot-to-ready time | Decision + IAM policy | 4 |
| Later imagery | First job usually sees only a pre-incident image; post-incident scenes arrive with the next pass (hours) and catalog delay | Decide: one-shot, or re-send the job later (e.g. fixed re-checks) | 4 |
| Cost guard | A stuck GPU instance runs up cost | Watchdog + billing alarm | 5 |
| Identification | Matching rule (length, heading, projected position) not defined | Define from stage 0 results | 5 |
| SAR validation | Real rate of optical + SAR pairs and their time gaps per region; matching tolerance; what counts as "stopped" | Measure on catalog data; tune in stage 0 | 0 |
| Viewer | S3 static hosting (+ CloudFront) vs Lightsail; public or authenticated | Decision | 6 |
