# Plan

The working plan for this app. Update it as stages finish and decisions are made.

## Goal

Collect periodic snapshots of the raw data of every vessel in our regions, analyze each snapshot
on its own and against earlier snapshots and history, raise alerts on discrepancies, score them
into incidents, then look for the vessel and anyone involved in free satellite imagery (optical + SAR) with an
off-the-shelf ship detector. Each incident is published to S3: one bucket of incident JSON, one
bucket of images the JSON references. The viewer reads only S3.

## Where each component runs

| Machine | Uptime | Runs |
|---|---|---|
| **Lightsail** (existing, cheap) | 24/7 | Hourly run: vessel data collector, incident report intake, DuckDB, snapshot analysis, incident scoring, waking the GPU on a new incident, sending the job, publishing incident JSON |
| **AWS GPU instance** (existing) | Only per job | Job server that starts on boot; finds the scene(s) for the job's area and time, downloads, runs detectors, matches to the parties in the job, uploads results to S3, then shuts itself down |
| **Viewer host** (Lightsail, or none) | 24/7 | Static incident viewer reading S3 |
| **S3** | — | Incidents bucket (JSON) + images bucket |
| **Shadow cloud PC** | Dev only | Editing, git. Can't run Docker (nested virtualization blocked) |

```
                      LIGHTSAIL (24/7)                                  AWS GPU (on demand)
vessel data ──> collect round ──> S3 GeoParquet (rounds) + DuckDB (state)
reports ─────> intake ─────────> DuckDB (reports)
              analyze: per-snapshot + vs previous ─> alerts ─> incidents ─> S3 incident.json
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

1. Lightsail collects snapshots continuously, analyzes each one, and **documents every alert and
   incident** in DuckDB, reported or not. This history is what later incidents are cross-referenced against (repeat
   offenders, other vessels dark nearby in space and time).
2. Each new incident is analyzed against that history and scored, with the reasons recorded. Below
   threshold, it stays as history only.
3. **Worth reporting →** start the evidence chain: Lightsail publishes `incident.json` and wakes
   the GPU with a job.
4. The GPU server takes the latest capture of the area, even one from before the event, and
   publishes a finding labelled with its capture time relative to the event (e.g. "9 h before;
   no capture after the incident yet"). A before-image shows the vessel while it was still reporting,
   which also tells us how the detector sees it.
5. Every job writes an outcome, including `nothing_found`, `no_capture` and `failed`.
6. GPU server shuts the instance down when its queue is empty; a hard max-runtime watchdog shuts
   it down regardless.
7. Lightsail updates the incident status and `index.json`.

Open: scenes acquired after the first job (next satellite pass) are not picked up unless
something triggers another job. See gaps.

### Proposed design points (need sign-off)

- **Split of search duties (agreed).** Lightsail analyzes the snapshots and decides an incident is worth a
  look. The GPU server does the scene search, download, detection and matching for the area and
  time it's given.
- **One writer per S3 object.** Lightsail owns `incidents/<id>/incident.json` and `index.json`; the
  GPU owns `incidents/<id>/findings/<scene>.json` and the images. No read-modify-write races.
- **Job payload.** Search area, time window, the subject vessel and its properties (MMSI, name,
  length, type, last position, sog/cog), other involved parties, and tracks of all vessels in the
  area over the window (from the snapshots), so the GPU matches detections without reaching into Lightsail's DB.
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

### Snapshot analysis

**Inputs (decided 2026-10-11):** two sources.

- **Raw vessel data:** Lightsail triggers a round per ROI; each round returns every vessel in that
  ROI as rows. No coverage or box logic on our side: what the round returns is the snapshot.
- **Incident reports** (e.g. UKMTO / JMIC): position and time of attacks, seizures and suspicious
  approaches. A report confirms what vessel data can only hint at; it creates or raises an
  incident directly and names the area and time for the evidence chain.

aisstream is dropped (no coverage in our regions; see Decisions). Every raw round and report is
stored as-is so analysis can be replayed and rules re-tuned on real data.

#### What a round contains (from the 2026-10-11 sample, Arabian Sea)

| Field | Meaning / handling |
|---|---|
| `SHIP_ID` | Provider's vessel id. **The only stable key** (no MMSI, IMO or call sign in the data) |
| `SHIPNAME`, `FLAG` | Identity attributes; tracked per `SHIP_ID` for changes |
| `LAT`, `LON` | Last known position |
| `ELAPSED` | Minutes since that position was reported. Position time = fetch time − `ELAPSED`. Values seen: 1 to 1395 (~23 h) |
| `SPEED` | Tenths of a knot (`128` = 12.8 kn) |
| `COURSE`, `HEADING` | Degrees; `HEADING` can be null |
| `ROT` | Rate of turn; out-of-range values occur (`708`) and must be nulled |
| `DESTINATION` | Free text. Used as a message: "CHINESE CREW&OWNER", "ARMED GUARD ONBOARD", "CNOWNER&CREW>GUARD", "EGSOK ARMED GUARD", "OWNER&CREW INDONESIA" |
| `SHIPTYPE`, `GT_SHIPTYPE` | Category (7 cargo, 8 tanker) and detailed type code |
| `LENGTH`, `WIDTH`, `DWT`, `L_FORE`, `W_LEFT` | Size, deadweight, antenna offsets |
| **`[SAT-AIS]` rows** | Anonymous satellite positions: no name, flag or size; `SHIP_ID` is an opaque token; `TYPE_NAME` and `STATUS_NAME` instead. Many sit next to a named vessel (e.g. `APOLLO ENERGY` and a `[SAT-AIS]` tanker ~0.8 km apart, same course and speed) |

Not in the data: MMSI, IMO, call sign, draught, navigational status for named rows. So the
identity checks work on name / flag / size per `SHIP_ID`, and draught-based checks are dropped.
Rows in the sample are ordered by DWT, largest first, which suggests a per-request row cap; see
gaps.

#### How a round is captured (`raw_vessels_aggregator`, gitignored)

`run_flow.py` opens the map page in Edge (Playwright attached over CDP),
records every data tile the page loads, merges rows by `SHIP_ID` (latest wins) and writes
`<time>_raw.json` + `<time>_vessels.csv` via `naval_tip_cue.positions`. The view is set by the map
URL (centre and zoom, default `centerx:53.4/centery:25.4/zoom:5`); the page loads the tiles for that
view by itself, no manual panning. A ROI is therefore one or more fixed map URLs.

Sample round `20261011T160036Z` (one capture):

| Measure | Value |
|---|---|
| Vessels | 3,070 (705 `[SAT-AIS]`, 23%) |
| Extent | lat −4.3 to 56.5, lon 20.6 to 92.9 (whole western Indian Ocean and Middle East) |
| Report age (`ELAPSED`) | median 24 min, p90 792 min, max 1,439; 896 rows (29%) older than 3 h, 352 (11%) older than 12 h |
| Estimated state | underway 1,442, stale 896, at anchor 345, stopped 283, slow 72, unknown 32 |
| Type labels | Named and `[SAT-AIS]` rows use different labels ("Cargo" vs "Cargo Vessel"); normalize |

Consequences for the design:
- **Stale is normal:** 29% of vessels are over 3 h old in any round, so going dark must be judged
  against the vessel's own reporting history, not an absolute `ELAPSED` threshold.
- **Completeness depends on zoom:** 3,070 vessels for that extent at zoom 5 is well below real
  traffic, so zoomed-out tiles likely return a subset (largest ships first). A vessel can be missing because of
  zoom, not because it went dark. Rounds need a fixed area and zoom so they are comparable.
- **Automation:** the hourly run opens the ROI's fixed URLs in a real browser; running that
  browser on headless Linux (Lightsail) is untested.

#### Key processing functions (one round)

| # | Function | Does |
|---|---|---|
| 1 | `fetch_round(roi)` | Request the ROI, store the raw response with fetch time |
| 2 | `normalize(row, fetch_time)` | Typed vessel record: speed in knots, position time from `ELAPSED`, sentinel and out-of-range values nulled, `is_sat_anon` flag |
| 3 | `resolve_identity(records, state)` | Named rows keyed by `SHIP_ID`. `[SAT-AIS]` rows attached to a named vessel when position (after projecting to the same time), course, speed and type agree, giving that vessel a fresher position; unattached ones are kept as anonymous tracks, linked round to round by projected position |
| 4 | `diff(state, records)` | Join against per-vessel state → **new**, **gone**, **present** with per-attribute deltas. One join per round, not a scan of past rounds |
| 5 | `check_snapshot(records)` | Layer A checks on the round alone |
| 6 | `check_changes(diff)` | Layer B checks on new / gone / present |
| 7 | `check_pairs(records)` | Vessel-to-vessel checks: encounters, clusters |
| 8 | `update_state(diff)` | Move state to the newest round; append track points |
| 9 | `cross_reference(alerts, history, reports)` | Layers C and D; score; open or raise incidents |
| 10 | `schedule_focus(alerts)` | Open, grow, and close focus ROIs for vessels with open alerts |

#### Focus rounds

Hourly rounds cover each ROI. When a vessel raises an alert, Lightsail also follows it closely:

1. **Open a focus ROI** around the vessel's last known position (e.g. a 50 km box).
2. **Every 10–15 min**, request only that box: one small request, few vessels, separate from the
   hourly rounds.
3. **Grow the box with time** at the vessel's last speed: a silent tanker at 12 kn can be ~40 km
   away after 2 h, so the box widens to keep it inside.
4. **Each focus round** goes through the same processing and answers: has it reported again
   (alert closes as false alarm), is it still silent (alert strengthens, may become an incident),
   or did something appear near its last position (`[SAT-AIS]` contact, another vessel stopping
   beside it)?
5. **Stop** when the alert closes or a maximum duration passes; the vessel is then only covered by
   the hourly rounds.

Focus rounds are stored like any other round (`roi=focus-<alert_id>`), so they're replayable.

#### Storage

No database server. One process runs once an hour on Lightsail and is the only writer, so
state lives in a single DuckDB file and history lives as GeoParquet in S3, queried by DuckDB
directly (spatial extension for distances, pairs and spatial joins).

Sizes assume ~2,000 rows per round, 4 ROIs, hourly (≈ 190k rows/day); to be re-estimated from
real rounds.

| Layer | Where | What | Size and retention |
|---|---|---|---|
| **Raw** | Private S3 bucket | Each round as received: `raw/roi=<roi>/date=<yyyy-mm-dd>/<fetched_at>.json.gz` | ~60 KB per round, ~2 GB/year; kept indefinitely for replay |
| **Rounds (history)** | Same private bucket | Normalized rows as GeoParquet: `rounds/roi=<roi>/date=<yyyy-mm-dd>/…parquet`. Hourly files, merged into one file per ROI per day by a daily job | Columnar and compressed, a fraction of the raw size; kept indefinitely |
| **Round index** | Same private bucket | STAC catalog of rounds (stac-geoparquet): one item per round with ROI footprint, time, row count, links to raw and Parquet | Small |
| **State + analysis** | `state.duckdb` on Lightsail, copied to S3 after each run | Current state and analysis tables (below) | Small (MBs to low GBs) |
| **Published** | S3 incidents + images buckets | `incident.json`, findings, images, `index.json` | Only reported incidents |

Tables in `state.duckdb`:

| Table | One row per | Holds |
|---|---|---|
| `vessels` | `SHIP_ID` (named or `[SAT-AIS]`) | Current attributes (name, flag, type, size, DWT, destination), last position and position time, first / last seen round |
| `recent_positions` | Vessel × new position report, last ~48 h | What multi-round checks need without reading Parquet (loitering, encounters, behaviour change) |
| `attribute_changes` | Change | `SHIP_ID`, attribute, old → new, round. Name, flag, size, destination |
| `sat_links` | `[SAT-AIS]` id × round | Named vessel attached to, distance, confidence |
| `alerts`, `incidents`, `incident_alerts` | — | Every alert and incident, reported or not; history for cross-reference |
| `reports`, `report_vessels` | — | Incident reports and the vessels matched to them |
| `findings` | GPU result | Read back from S3 after each GPU job |

Longer look-backs (repeat offenders, a vessel's past tracks) query the Parquet history in S3.
The existing PostGIS scaffold (`naval_tip_cue/db/schema.sql`, psycopg, `initdb`, `DATABASE_URL`)
is replaced at stage 1.

**A. Within the round**

| Check | Signal in this data |
|---|---|
| Stale position | `ELAPSED` large for a vessel that should be reporting (underway, large ship) |
| Threat signalling | Keywords in `DESTINATION` (crew nationality, armed guard, "no contact") |
| Stopped at sea | Speed ≈ 0 in open water, away from ports and anchorages |
| Implausible data | Position on land, impossible speed, `ROT` out of range, zero or impossible size |
| Heading ≠ course | Large mismatch while moving (drift, towing, or bad data) |

**B. Against the previous state**

| Check | Signal in this data |
|---|---|
| Going dark | `ELAPSED` keeps growing over rounds while the position stays frozen; or the vessel is in the gone set and its last position, course and speed don't explain an ROI exit or port arrival |
| Back on | Position refreshes after a long `ELAPSED` gap: report gap, displacement, implied speed |
| Appeared from nowhere | New `SHIP_ID` mid-ROI with no history |
| Identity change | Same `SHIP_ID` with new name, flag or size |
| Destination change | Especially to a threat-signalling text |
| Position jump | Implied speed between position times is impossible (spoofing) |
| Mass displacement | Many vessels shifted at once (GPS jamming) |
| Behaviour change | Sudden stop, drift, sharp course or speed change |
| Loitering | Lingering off route over many rounds |

**Pairs (function 7):** two vessels close and slow over several rounds (ship-to-ship transfer);
named vessel shadowed by an unattached `[SAT-AIS]` track.

**C. Cross-reference with history:** repeat offenders, other alerts nearby in space and time,
earlier names and flags of the same `SHIP_ID`. This turns alerts into scored incidents.

**D. Reports against vessel data:** for each report, find the vessels at that place and time
(position times, not fetch times), the subject and who else was near, and link their alerts.

Every check needs thresholds tuned on recorded rounds.

## Stages

Each stage ends with something we run and check before moving on.

| # | Stage | Machine | Done when | Status |
|---|---|---|---|---|
| — | Scaffold, `config.yaml`, PostGIS schema, `initdb` (PostGIS part superseded by DuckDB) | — | — | Done |
| 0 | **Detector test** — run the Ai2 detectors on recent scenes over Hormuz / Gulf of Oman and the southern Red Sea; burn detections with labels (length, type, heading) onto the image; include at least one optical + SAR pair close in time; then record snapshots through a satellite pass and match detections to named ships | GPU | Labelled detection images reviewed; measured detection rate by ship length, unmatched rate, time and memory per scene. Go / adjust / fall back. | Next |
| 1 | **Collectors** — hourly run: vessel data → raw + GeoParquet rounds in S3, STAC round index, state in DuckDB; incident reports → DuckDB; `replay` from stored snapshots | Lightsail | Stored snapshots replay into the same rows every time | |
| 2 | **Snapshot analysis** — layer A and B checks, starting with disappeared, identity change and position jump; history cross-reference; reports matched to vessels; scoring | Lightsail | Replayed snapshots fire the expected alerts; a vessel leaving through the area edge doesn't | |
| 3 | **Incidents → S3** — scoring with recorded reasons; publish `incident.json` + `index.json` | Lightsail | Incident JSON appears in S3 | |
| 4 | **Dispatch** — on a new incident, start the GPU instance and send the job | Lightsail | A new incident boots the GPU and delivers a job | |
| 5 | **GPU job server** — starts on boot, finds the optical and SAR scenes, pairs them per the evidence rules, runs detectors from stage 0, matches to the job's parties, grades the evidence, uploads findings + images with capture time relative to the event, idle shutdown + watchdog | GPU | Job in → results in S3 → instance stopped, with no manual step | |
| 6 | **Viewer** — static site reading S3: incident reel + detail page with crop and SVG overlay | Viewer | Incidents browsable from S3 | |
| 7 | **Packaging + ops** — containers or systemd per machine, restart policies, logs, cost alarm | All | Runs unattended | |

Later: the remaining snapshot checks, Global Fishing
Watch events, VIIRS coarse cue, "follow vessel" live view.

## Feasibility

**Verdict (2026-10-11): sound.** Nothing found blocks a running app. The detectors are not the weak
point; the open question is how strong the evidence is, which stage 0 measures.

| Concern | Risk | Why |
|---|---|---|
| Detectors run at all | Low | Public code and weights, maintained (Ai2, updated Sep 2026), GPU instance available. |
| Detectors find ships | Low–medium | Sentinel-2: precision 77%, recall 79% (published). S1 and Landsat unpublished. Ships under ~25 m mostly missed at 10 m pixels; cargo ships and tankers are fine. |
| An image exists | Medium | ~2 in 3 incidents get a capture in the window (table below); the rest are "no capture". |
| **Which ship is ours** | **Main concern** | Detectors give no identity. Inferred: unmatched ship of compatible length near the projected position. Works for captures a few hours after the event; a day later or in a busy strait there can be several candidates. Vessels missing from the vessel data look like false candidates. |

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
- Round interval: **one round per ROI per hour** (2026-10-11). Checks use position times from
  `ELAPSED`, so timing within the hour is still known; "several rounds" in pair and loitering
  checks means hours.
- Adaptive interval (focus rounds): see "Focus rounds" under Snapshot analysis. Detection latency
  is set by the interval and the source's data age, not by processing (seconds per round).
- Inputs: raw vessel data snapshots + incident reports. Vessel data use is private; terms of use
  flagged and accepted by the owner (2026-10-11). **aisstream dropped** (2026-10-11): 10 min over
  the four regions gave messages only from the UAE coast; zero from Suez, Bab el-Mandeb/Aden and
  the Arabian Sea (Suez-only re-check: 0 in 90 s vs 27 vessels for a Gibraltar control).
- Machine split: Lightsail 24/7 for snapshots and incident logic; GPU instance on demand for
  imagery + detectors, self-shutdown; viewer reads S3.
- Storage (2026-10-11): no database server. DuckDB file for state and analysis on Lightsail;
  rounds as GeoParquet in S3 with a STAC index; real AWS S3 for published output. PostGIS dropped:
  one hourly single-writer process doesn't need it.
- Python workers; React/Vite/TS viewer.

## Open gaps

| Area | Gap | Needs | By stage |
|---|---|---|---|
| Round automation | Opening the ROI URLs on a schedule; running a real browser on Lightsail (headless Linux) | Test on Lightsail | 1 |
| Round completeness | Vessel count per round depends on zoom; pick ROI views and zoom that return all vessels, and verify by comparing counts across zoom levels | Measure | 1 |
| Vessel data source | Fields known from the sample. Still open: per-request row cap (no known cap; sample is DWT-ordered, so check whether dense ROIs return a round-number count and split the ROI if so), how fresh rounds can be, ROI size limits. `[SAT-AIS]` ids expected stable across rounds (verify on the first recorded rounds); terms of use | Details from you | 1 |
| ROI exits | A vessel sailing out of the ROI or into port is in the gone set but isn't dark | Rule from last position, course and speed + port list | 2 |
| Credentials | Landsat needs AWS keys (requester-pays); S1 needs a free Copernicus account | `COPERNICUS_USERNAME` / `COPERNICUS_PASSWORD`, AWS keys in `.env` | 0 |
| GPU instance | Instance type, RAM vs 15 GB shm request, GPU usable from Docker or natively, disk for scenes (S1 GRD ~1.7 GB, S2 L1C ~0.7 GB) | Check | 0 |
| Lightsail | Plan size (RAM/disk) for the hourly run and `state.duckdb`; static IP; S3 access for the private bucket | Check | 1 |
| Reports source | Which report sources, format and access (feed, web page, PDF advisories); terms for automated use | Details from you | 1 |
| Rules / scoring | Thresholds in `config.yaml` are guesses; no labelled real events | Reference set (e.g. UKMTO incidents, GFW gap events) | 2 |
| Output | S3 buckets not created; `incident.json` / `finding.json` schemas not defined | AWS access; schemas | 3 |
| Search area | 24 h after at 12 kn ≈ 530 km drift vs 60 km cap | Policy: shorter window, or accept lower odds | 4 |
| Wake + handoff | IAM for Lightsail to start/stop only the GPU instance; push API vs SQS/S3 pull; GPU boot-to-ready time | Decision + IAM policy | 4 |
| Later imagery | First job usually sees only a pre-incident image; post-incident scenes arrive with the next pass (hours) and catalog delay | Decide: one-shot, or re-send the job later (e.g. fixed re-checks) | 4 |
| Cost guard | A stuck GPU instance runs up cost | Watchdog + billing alarm | 5 |
| Identification | Matching rule (length, heading, projected position) not defined | Define from stage 0 results | 5 |
| SAR validation | Real rate of optical + SAR pairs and their time gaps per region; matching tolerance; what counts as "stopped" | Measure on catalog data; tune in stage 0 | 0 |
| Viewer | S3 static hosting (+ CloudFront) vs Lightsail; public or authenticated | Decision | 6 |
