# Handoff — first draft, session of 2026-10-11

State of work so far, written to pick up on another machine. Branch: `first-draft`.

## Done

| Commit | What |
|---|---|
| `eb01356` | `README.md`, `CLAUDE.md` (conventions adapted from geo-dataset-generator), `.gitignore` |
| `a8e95b2` | Python package scaffold, `config.yaml`, PostGIS schema, `python -m naval_tip_cue initdb` |

- **CLAUDE.md rules kept:** no comments in code (rationale goes in one `context/<component>.md`
  per component), concise replies, single commit author **Dor Yunger <dor.yunger@gmail.com>** with
  no Claude co-author line, run-don't-predict, visually verify geometry, secrets in `.env`.
- **aisstream key verified live:** 20 s over Gibraltar gave ~190 messages (PositionReport,
  StandardClassBPositionReport, ShipStaticData, StaticDataReport). Sentinel values seen in the
  wild: `TrueHeading 511`, `RateOfTurn -128` mean "not available".
- **MarineTraffic ruled out:** `getData/get_data_json_4/...` is an internal map endpoint behind
  Cloudflare (403 to scripts) and their ToS forbids automated access. Not to be used. Legit
  alternatives: aisstream (live, terrestrial), Global Fishing Watch API (free non-commercial,
  ready-made gap/encounter/loitering events, lagged by days), Kpler/MarineTraffic paid API.

## Setting up on a new machine

1. `git checkout first-draft`
2. Create `.env` (gitignored, never commit it):
   ```
   AISSTREAM_API_KEY=<key from aisstream.io>
   DATABASE_URL=postgresql://naval:<password>@localhost:5432/naval
   ```
3. Python: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
4. Database: Postgres 18 + PostGIS 3, role/db `naval`, `CREATE EXTENSION postgis`, then
   `.venv/bin/python -m naval_tip_cue initdb`. On the old machine this was a native apt install
   (`postgresql-18 postgresql-18-postgis-3`); **the target is a container instead — see Open.**

## Open: run everything in containers

Requested at the end of the session, not started (Docker install was stopped). Intended shape,
one `docker-compose.yml`:

- `db` — `postgis/postgis` image, volume for data
- `listen`, `engine`, `cue`, `api` — one Python image, different commands
- `web` — React build served by nginx (or Vite dev in development)
- detector services as their own containers (see below), called over HTTP by `cue`

Decide: Docker vs Podman, and whether the old machine's native Postgres gets removed.

## Plan (approved) — thin end-to-end slice

Decisions: a few configured regions (Gibraltar/Alboran, Bab el-Mandeb/S. Red Sea, Hormuz — in
`config.yaml`); PostgreSQL + PostGIS; Python workers + FastAPI + React/Vite/TS (same stack as
geo-dataset-generator); thin slice end to end first, then widen each layer.

```
aisstream WS ──> listen ──> vessel_state + track_points (72h rolling)
                 engine (every 60s): rules ──> alerts ──> correlate ──> incidents
                 cue: imagery search ──> crop ──> detector ──> AIS match ──> finding
                 api (FastAPI, read-only) ──> web (incident reel + detail page)
```

Three stored layers: **alert** (one rule fired) → **incident** (alerts cross-referenced and scored,
reasons recorded) → **finding** (imagery searched, detector run, outcome incl. "no capture" and
"nothing found"). Schema is in `naval_tip_cue/db/schema.sql`.

Steps (1 done):
1. ~~Scaffold, config, schema~~
2. **Listener** `listener/aisstream.py` — asyncio websockets, bboxes from config, reconnect with
   backoff, parse position/static messages, sentinels → null (heading 511, ROT -128, sog 102.3,
   lat 91, lon 181), downsample track points to ~1 per 30 s per vessel, batch writes.
   `--record file.jsonl` + `replay` command for deterministic tests.
3. **Rule: going dark** `rules/dark.py` — was reporting regularly, underway (>2 kn), class A, then
   silent > 2 h; coverage confidence from `coverage_cells` (message density) to separate "went
   dark" from "left receiver coverage". Close on reappearance with gap, displacement, implied speed.
4. **Correlate** `correlate/incidents.py` — score terms recorded as reasons: coverage confidence,
   another vessel dark/last seen nearby in space-time, repeat offender in history, implausible
   implied speed. Non-promoted alerts stay as history.
5. **Imagery search** `imagery/` — STAC search, area = last position buffered by
   min(sog × elapsed, cap), window [last_seen − 6 h, +24 h], capture closest in time, save crop.
6. **Detector** `detect/` — see next section. Then match detections to AIS interpolated to
   capture time: unmatched = dark vessel candidate; near the subject's projected position =
   candidate subject.
7. **API** `server/app.py` — `GET /incidents`, `GET /incidents/{id}`, static crops.
8. **Web** `web/` — reel of incidents with outcome badges; detail page with vessel info, alert
   timeline, reasons, crop with SVG overlay (search area, track, detections). No map library,
   no live data for now (future: "follow vessel" live view).
9. **Context docs** per CLAUDE.md.

Later: encounter + loitering rules, GFW events as second tip source, VIIRS coarse cue, systemd.

## Imagery + detectors (researched, not implemented)

Two imagery families: **optical** (Sentinel-2 10 m, Landsat 8/9 15–30 m) and **SAR**
(Sentinel-1 — Sentinel-1 *is* the SAR source; works through cloud and at night).

Open-source ship detectors with downloadable weights, **no training needed**:

| Imagery | Detector | License | Notes |
|---|---|---|---|
| Sentinel-2 L1C | Ai2 `rslearn_projects` — [sentinel2_vessels](https://github.com/allenai/rslearn_projects/blob/master/docs/sentinel2_vessels.md) | Apache-2.0 | Weights via `wget` from GCS; needs all bands (B01–B12, B8A), baseline ≥ N0400; Docker with FastAPI `POST /detections`; GeoJSON out + attributes (type, length, heading) |
| Landsat 8/9 L1 | Ai2 `rslearn_projects` — [landsat_vessels](https://github.com/allenai/rslearn_projects/blob/master/docs/landsat_vessels.md) | Apache-2.0 | Image `ghcr.io/allenai/landsat-vessel-detection`, same `/detections` API |
| Sentinel-1 GRD (VV+VH) | [xView3 first place](https://github.com/DIUx-xView/xView3_first_place) | MIT | Traced ensemble weights in [releases](https://github.com/BloodAxe/xView3-The-First-Place-Solution/releases/download/1.0/traced_ensemble.jit); image `ekhvedchenyateam/xview3:72` |
| Sentinel-1 | Ai2 [vessel-detection-sentinels](https://github.com/allenai/vessel-detection-sentinels) | Apache-2.0 | Faster R-CNN on VV+VH; weight download location unclear in README |
| Sentinel-1 | CFAR (classic, no ML) | — | Baseline; needs a land mask (Natural Earth 10 m) |

Not usable: the `yolo11n-obb` model in geo-dataset-generator (DOTA, sub-meter optical) — doesn't
transfer to 10 m optical or SAR.

Constraints found:
- The Ai2 models expect GPU (`--gpus all`; CPU described as very slow) and whole scenes, not crops.
  The old laptop has no NVIDIA GPU (Intel Iris Xe, 8 cores, 14 GB). Check the new machine with
  `nvidia-smi` — it decides whether these run locally or whether CFAR is the first detector.
- Ai2 Sentinel-2 pipeline pulls L1C from the public Google Cloud bucket by scene ID.
- Each detector fits naturally as its own container behind HTTP, called by `cue`.

Next step after containers: step 2 (listener).
