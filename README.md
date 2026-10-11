# naval-tip-cue

Given a **tip** (one or more vessels involved in an event at a known place and time), search free
satellite imagery for them, detect ships in the captures, match the detections to the tip's
vessels, grade the evidence and publish the result.

Where tips come from is out of scope. Anything that produces a valid tip can drive the app: an
alerting system, an incident report typed in by hand, a replayed past event.

The staged work to build it is in [PLAN.md](PLAN.md); measured feasibility data is in
[docs/feasibility.md](docs/feasibility.md).

## Flow

```
tip.json ──> intake: validate, store ──> job ──> GPU job server
                                                  1. search area + time window
                                                  2. find optical + SAR scenes
                                                  3. download, crop, detect ships
                                                  4. match detections to the tip's vessels
                                                  5. grade evidence
             S3 <── tip, findings, images, index ─┘
viewer ──reads──> S3
```

Three layers: **tip** (input, as received) → **job** (one search run for a tip) → **finding** (one
scene searched: detections, matches, grade, or an outcome such as "no capture").

## Input: the tip

One JSON document per tip. The more of it is filled in, the smaller the search area and the more
reliable the match. Field names are a proposal; the schema is fixed in stage 1.

| Field | Required | Used for |
|---|---|---|
| `tip_id` | yes | Stable id from the producer; re-sending the same id updates the tip, never duplicates it |
| `source` | yes | Who produced it and on what basis (free text + optional references, e.g. a report URL) |
| `event.type` | yes | `dark`, `encounter`, `loitering`, `attack`, `seizure`, `suspicious_approach`, `other` |
| `event.time` | yes | UTC; centre of the imagery time window |
| `event.position` | yes | lat/lon of the event, or of the subject's last known position |
| `event.uncertainty_km` | no | How far off the position may be; widens the search area |
| `subject` | yes | The vessel we are looking for (vessel object, below) |
| `parties` | no | Other vessels involved (e.g. the other ship in an encounter) |
| `context_vessels` | no | Other vessels known to be in the area over the window, with tracks. Lets the matcher explain detections that are *not* the subject |
| `search` | no | Overrides: `before_hours`, `after_hours`, area polygon, sensors |

Vessel object:

| Field | Required | Used for |
|---|---|---|
| `id` | yes | Producer's id for the vessel; any stable string |
| `name`, `mmsi`, `imo`, `flag` | no | Display and cross-reference only |
| `type` | no | cargo, tanker, fishing, …; matched against the detector's type |
| `length_m`, `width_m` | strongly advised | Main matching feature; without them a match rests on position alone |
| `track` | yes (≥ 1 point) | `[{time, lat, lon, speed_kn, course, heading}]`. The last point before the event sets the projected position and how fast the search area grows |

Validation: required fields present; times UTC ISO 8601 and not in the future; lat −90..90, lon
−180..180; speed 0..50 kn; course and heading 0..359; sizes > 0. An invalid tip is rejected with the
list of reasons and stored as rejected. Missing optional fields are accepted and noted on the job,
since they lower match confidence.

## Processing

1. **Time window**: `[event.time − before_hours, event.time + after_hours]`, default −6 h / +24 h
   (`config.yaml`, `imagery`).
2. **Search area**: around the subject's last track point, radius = last speed × time from that
   point to the capture, plus `uncertainty_km`, clamped to 5–60 km (`config.yaml`). Computed per
   capture time, so an early capture gets a small box.
3. **Scene search**: Sentinel-2 L1C, Landsat 8/9 L1 and Sentinel-1 GRD scenes that cover the area
   inside the window, via STAC. A capture before the event is used too: it shows the vessel while
   still reporting and how the detector sees it.
4. **Detection**: Ai2 detectors (one per sensor) on the crop. Output per ship: position, length,
   width, heading, speed, type.
5. **Matching**: project each tip vessel's track to the capture time; candidates are detections
   of compatible length and heading near the projected position. Context vessels at their own
   projected positions are matched first and removed, so they aren't mistaken for the subject.
6. **Evidence grade** (rules below).
7. **Publish** the finding, images and updated index.

### Evidence rules

Optical (Sentinel-2 or Landsat) shows the object; SAR (Sentinel-1) confirms it is a real hard target
and not a wake, cloud or whitecap.

1. Among the optical captures, take the one closest in time to a SAR pass over the area.
2. SAR validation counts only if `speed × gap` between the two captures stays within the matching
   tolerance (position and length must agree).
3. A vessel known to be stationary (near-zero last speed, loitering, alongside another) allows a
   longer gap.
4. Otherwise drop SAR validation and report optical only.

| Grade | Meaning |
|---|---|
| **Confirmed** | Optical and SAR agree on position (after allowing for movement) and length |
| **SAR only** | Real hard target; no usable optical |
| **Optical only** | Seen, not validated; could be a false target |
| **None** | No capture, or nothing found |

Sentinel-2 and Landsat both pass around 10:00–11:00 local time; Sentinel-1 at other hours, so
typical optical–SAR gaps are several hours.

## Output

Public-facing results live in S3; one writer per object.

| Object | Holds |
|---|---|
| `tips/<tip_id>/tip.json` | The tip as received, validation notes, status |
| `tips/<tip_id>/findings/<scene>.json` | Sensor, scene id, capture time and offset from the event ("9 h before"), detections, matches with confidence and reasons, evidence grade, outcome: `found`, `nothing_found`, `no_capture`, `failed` |
| `tips/<tip_id>/images/…` | Crops with detections and the projected positions drawn on |
| `index.json` | All tips with status and best grade, for the viewer |

The viewer is a static site reading S3: a list of tips and a detail page with the crop and an SVG
overlay.

## Where it runs

| Machine | Uptime | Runs |
|---|---|---|
| **Intake host** (existing Lightsail) | 24/7 | Receives tips, validates, stores, starts the GPU instance and hands it the job, updates `index.json` |
| **AWS GPU instance** (existing) | Per job | Job server started on boot: scene search, download, detection, matching, grading, upload; shuts down when its queue is empty, with a max-runtime watchdog |
| **S3** | — | Tips, findings, images, index |
| **Viewer** | — | Static site from S3 (+ CloudFront), or Lightsail |
| **Shadow cloud PC** | Dev only | Editing, git. Can't run Docker (nested virtualization blocked) |

## Sources

| Source | Access | Cost |
|---|---|---|
| Sentinel-1 GRD | Copernicus Data Space, free account | Free |
| Sentinel-2 L1C | Public GCS / Earth Search STAC | Free |
| Landsat 8/9 L1 | AWS requester-pays | Pennies per scene |
| NASA VIIRS (later) | Earthdata account | Free |

## Decisions

- **Tip producer is out of scope** (2026-10-11). The app's contract is the tip; vessel-data
  collection lives in the separate `raw_vessels_aggregator` repo. aisstream dropped: no coverage in
  the focus regions.
- **Focus regions** (2026-10-11): Middle East, where recent incidents are. Used to pick stage 0
  test scenes and to measure imagery availability; tips outside them are still processed.

  | Region | Covers | Approx. bbox (lon, lat) |
  |---|---|---|
  | `suez_red_sea_north` | Suez Canal, Gulf of Suez, northern Red Sea | 32.0–35.5 E, 27.0–31.5 N |
  | `bab_el_mandeb_aden` | Southern Red Sea, Bab el-Mandeb, Gulf of Aden | 41.0–51.5 E, 10.5–16.5 N |
  | `hormuz_oman` | Eastern Persian Gulf, Strait of Hormuz, Gulf of Oman | 54.0–60.0 E, 22.5–27.5 N |
  | `arabian_sea` | NW Indian Ocean approaches | 55.0–66.0 E, 12.0–22.0 N |
- **Split of duties** (agreed): the intake decides nothing about whether a tip is worth it; every
  valid tip gets a job. The GPU does scene search, download, detection and matching.
- **Shut down when idle**, not after each job: several tips can queue while the instance is up.
- **No database server**: tips and findings are files in S3; the intake keeps a small local
  state file. The PostGIS scaffold is removed in stage 1.
- Python workers; React/Vite/TS viewer.

## Code

Currently only scaffolding from the earlier AIS-stream design (`config.py`, a PostGIS schema and
`initdb`); replaced in stage 1.
