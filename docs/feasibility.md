# Feasibility

**Verdict (2026-10-11): sound.** Nothing found blocks a running app. The detectors are not the weak
point; the open question is how strong the evidence is, which stage 0 measures.

| Concern | Risk | Why |
|---|---|---|
| Detectors run at all | Low | Public code and weights, maintained (Ai2, updated Sep 2026), GPU instance available. |
| Detectors find ships | Low–medium | Sentinel-2: precision 77%, recall 79% (published). S1 and Landsat unpublished. Ships under ~25 m mostly missed at 10 m pixels; cargo ships and tankers are fine. |
| An image exists | Medium | ~2 in 3 tips get a capture in the window (table below); the rest are "no capture". |
| **Which ship is ours** | **Main concern** | Detectors give no identity. Inferred: unmatched ship of compatible length near the projected position. Works for captures a few hours after the event; a day later or in a busy strait there can be several candidates. Vessels absent from the tip's `context_vessels` look like false candidates. |

## Detectors

Ai2 `rslearn_projects` covers all three sensors with the same API: `POST /detections` with a scene
ID → GeoJSON with position, length, width, heading, speed, type. No training. GPU; the Docker
images ask for 15 GB shm. Weights download from public GCS.

| Sensor | Image | Scene source | Notes |
|---|---|---|---|
| Sentinel-1 GRD (SAR) | `ghcr.io/allenai/sentinel1-vessel-detection` | Copernicus Data Space (account) | IW, VV+VH; also uses one image ~60 days earlier |
| Sentinel-2 L1C | `ghcr.io/allenai/sentinel2-vessel-detection` | Public GCS (current to 2026-10-09) | All 13 bands, baseline ≥ N0400 |
| Landsat 8/9 L1 | `ghcr.io/allenai/landsat-vessel-detection` | AWS requester-pays | All bands B1–B11 |

**Resolution.** Each detector is trained on its own source at native resolution; resolution limits
minimum ship size and length accuracy. Estimates, to be measured in stage 0 against reported
lengths:

| Source | Pixel | Smallest reliable ship (est.) | Effect |
|---|---|---|---|
| Sentinel-2 | 10 m | ~20–25 m | Length ± ~10–20 m |
| Landsat | 15 m pan / 30 m | ~40–50 m | Large ships only; coarse length |
| Sentinel-1 | ~10 m pixel, ~20 m resolution | ~15–25 m | Metal hulls bright, small ships visible; length often overstated by sidelobes |

Target ships (tankers, cargo, 100 m+) are well within range on all three; small boats are missed
or marginal. Optical–SAR length agreement needs a wide, source-specific tolerance.

Dropped: xView3 (needs SNAP-preprocessed input, up to 12 min/scene); geo-dataset-generator's
`yolo11n-obb` (sub-meter optical, doesn't transfer). SAR fallback: CFAR (CPU, needs land mask).

## Imagery availability

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

Both optical and SAR inside the window: roughly 15–20% of tips (assumes independence; not yet
measured).

## Stage 0 detector test (2026-10-11, in progress)

Run on the Shadow PC (RTX 2000 Ada 16 GB, 16 GB RAM, Windows 11), Ai2 code installed natively
(no Docker). Everything for the test lives outside this repo, since the app doesn't use it:
`C:\Users\Shadow\projects\ai2-detectors\` holds the venv, Ai2 clones, weights, the Windows
patches (`patches\`), test scripts (`scripts\`), run outputs (`runs\`) and the result images
referenced below (`images\`); its `README.md` lists what each is for.

### Setup on Windows

Ai2's code assumes Linux. Four local patches, kept in `ai2-detectors\patches\` (apply with `git apply` in each
clone). None is needed on the Linux deploy target:

| Symptom | Cause | Patch |
|---|---|---|
| Crash at start: `cannot find context for 'forkserver'` | `rslp/utils/mp.py` forces the Linux-only `forkserver` start method | Use `spawn` on Windows |
| Hang at the ingest step, 0% CPU for 10 min | `multiprocessing.Pool(32)` deadlocks on `join()` under `spawn` (py-spy: parent in `pool.join`, idle worker on the queue lock) | Worker count from `RSLP_APPLY_WORKERS` (default 32); run with `RSLP_APPLY_WORKERS=0 RSLEARN_NUM_DATA_LOADER_WORKERS=0` |
| `Distributed package doesn't have NCCL built in` | `rslearn` defaults every Lightning run to DDP; NCCL doesn't exist on Windows | Strategy `auto` on Windows (`rslearn/lightning_cli.py`) |
| `FileExistsError: [WinError 183]` saving `items.json` (S1) | `open_atomic` uses `os.rename`, which won't overwrite on Windows | `os.replace` (`rslearn/utils/fsspec.py`) |

Also needed: `pip install -r requirements-extra.txt` (the pipeline imports `beaker`), and `rslearn`
installed editable so the patch takes effect. PyTorch 2.11 + CUDA 12.8 wheels.

### Sentinel-2 results

| Run | Scene | Time | Peak GPU (whole card) | Detections |
|---|---|---|---|---|
| Ai2 example | `S2A_MSIL1C_20180904T110621_…_T30UYD` (Norfolk, UK) | 220 s | 3.6 GB | 13 candidates, 9 after the near-infrastructure filter |
| Hormuz | `S2C_MSIL1C_20261011T063811_N0513_R120_T40REP_20261011T084717` (Jask, Gulf of Oman, 0% cloud) | 175 s | 3.6 GB | 30 |

Time per scene: download ~45 s, materialize ~40–80 s, detection ~55 s (1.6 tiles/s), attribute
model ~10 s. The Hormuz scene (tile sensed 06:52 UTC, product generated 08:47 UTC) was on the public GCS bucket the same evening; first-availability delay not measured.

Output per ship: position, score, length, width, heading, speed, type (from a second, attribute
model), 128×128 px crop.

**Example scene, by eye** (`images\example_crops.jpg`,
`images\example_overview.jpg`): all 9 on water; 7 clearly vessels (a 20 kn boat
with a long wake, a 145 m cargo ship on the scene edge); 2 are 1–2 px dots, possibly small boats,
possibly noise (lowest scores, 0.81 and 0.91). Most of the scene is cloud, so misses aren't
measured here.

**Hormuz vs the demo vessel CSV** (16:00 UTC snapshot, ~9 h after the pass; only stopped/anchored
ships count as truth) (`images\hormuz_40REP_overview.jpg`,
`images\hormuz_40REP_spots.jpg`):

| CSV ship | State | Result | Distance | Length CSV → detected |
|---|---|---|---|---|
| AZHIN, tanker | at anchor | found | 0.23 km | 330 → 331 m |
| BLACK STORM, tug | stopped | found | 0.02 km | 29 → 38 m |
| DARYA MAHER, tug, Jask harbour | stopped | found | 0.76 km | 60 → 50 m |
| TRONE, tanker | at anchor | not checkable: in the scene's no-data edge | — | — |
| STAR NINA, cargo | at anchor | not there at 06:52 (empty water in full-res crop); anchored later | — | — |

3 of 3 checkable ships found. The CSV holds only 6 ships in this tile, so 27 of 30 detections have
no CSV counterpart; on the overview they are mostly 270–330 m tankers anchored off Jask plus small
craft.

Flaws seen:
- **Inland false detection:** a "57 m cargo" on a dam reservoir ~30 km inland (score 0.84).
  Needs a sea mask.
- **Duplicate:** one ~320 m tanker produced two detections ~100 m apart (328 and 313 m). Needs
  merging of overlapping detections before matching.

### Sentinel-1 results

**Data access without credentials.** The pipeline's scene-ID mode downloads from AWS
`sentinel-s1-l1c`, which is requester-pays (AWS keys, ~$0.30 per run for target + historical).
Instead, VV/VH GRD files were taken from Microsoft Planetary Computer (free, no account; same raw
format: uint16, 210 GCPs, EPSG:4326) and fed to the pipeline's local-file mode. The CLI also
requires `--score_threshold` (not in Ai2's docs); 0.7 is their API default. The model needs one
historical image of the same orbit direction 30–90 days earlier; chosen: same track, 60 days
before, full overlap.

| Run | Target / historical | Time | Peak GPU | Detections |
|---|---|---|---|---|
| Bab el-Mandeb | `S1C_IW_GRDH_1SDV_20261011T151835_…_0139B0` / `…20260812T151833_…_011C89` | 1,254 s | 8.5 GB | 46 |

Time breakdown: download 2 × ~1 GB, ~1 min; ingest ~7 min (each image reprojected from radar
geometry onto a 10 m UTM grid, 25k × 17k → 28k × 22k px, ~3.5 min per scene on one process);
materialize ~4 min; detection ~10 min (GPU at 100%); attribute model <1 min.

**Vs the demo vessel CSV.** 7 CSV ships in the footprint, all reported 37–41 min after the pass;
each was moved back to 15:18:35 by its speed and course (12–16 km) before matching
(`images\s1_bab_151835_overview.jpg`, `images\s1_bab_151835_spots.jpg`):

| CSV ship | Speed | Nearest detection | Length CSV → detected |
|---|---|---|---|
| GARDA, tanker | 11.3 kn | 0.42 km | 250 → 248 m |
| RC VENUS, cargo | 9.6 kn | 0.61 km | 229 → 222 m |
| POP, tanker | 10.5 kn | 0.30 km | 228 → 206 m |
| MYRA BAY, cargo | 10.5 kn | 0.37 km | 189 → 175 m |
| DA LIANG, cargo | 9.6 kn | 0.19 km | 179 → 178 m |
| SAFEEN PRISM, cargo | 12.6 kn | 3.05 km | 208 → 197 m |
| ADAM A, cargo, at anchor in port | 0 | 0.49 km, but a 28 m object | 170 → — |

- 5 of 5 moving ships matched within 0.6 km; length within 1–22 m (median ~9 m). Better than the
  expected SAR overstatement.
- SAFEEN PRISM: radar crop shows nothing at the projected point; the nearest detection is 3 km
  away with a matching length, likely the same ship off a straight-line course over 40 min.
- ADAM A: in a harbour among berths; several small detections there, none 170 m. Either missed
  in clutter or removed by the near-infrastructure filter. Port scenes need their own check.
- 39 of the 46 detections have no CSV ship to check against; on the overview they line the
  shipping lane.

### Sentinel-1 vs Sentinel-2 on stationary ships (Jask)

No same-day pair exists over the Jask anchorage; closest: S1
`S1C_IW_GRDH_1SDV_20261010T020602_…_0138DB` (2026-10-10 02:06 UTC, historical 2026-08-11) vs the
Hormuz S2 run (2026-10-11 06:52 UTC), **29 h apart**. S1 run: 1,549 s, 187 detections over the whole
scene. Stationary threshold (< 1 kn) and match distance (1 km) read from `rules/evidence_graph.json`.
Overlay: `images\jask_s1_on_s2.jpg` (red S2, cyan S1).

- **18 of 20** stationary S2 detections inside the S1 footprint have an S1 detection within 1 km,
  at 0.09–0.54 km. The 14 ships ≥ 170 m all matched at 0.09–0.29 km.
- The 2 misses: the inland-reservoir false detection from the S2 run (no radar target there, so the
  cross-check rejects it) and a 35 m service craft 1.6 km off (small craft move).
- Length, S1 minus S2, on the 14 large ships: −29 to +13 m, median −9 m (S1 reads slightly shorter).
- The S2 duplicate (two detections on one ~320 m tanker) matched one S1 detection: radar sees one
  ship.
- No anchored tanker was missing from S1, so no sign of the long-anchored ship being cancelled by
  the 30–90-day reference image.
- S1 also showed a cluster of detections inside Jask harbour and one on the shoreline.

Set from this: stationary pairing `max_gap_hours` 30 and `max_length_diff_m` 30 in
`rules/evidence_graph.json` (`basis: stage0`).

### Landsat vs Sentinel-2 (Hormuz, 12 min apart)

`LC09_L1TP_159042_20261011_20261011_02_T1` (06:40 UTC, 1% cloud) vs the Hormuz S2 tile (06:52 UTC).
Read from AWS `usgs-landsat` (requester-pays; key with an inline read policy on that bucket, in
`.env`). The pipeline now also runs a classifier built on OlmoEarth
(`landsat_vessel_classification_v2/olmoearth_base_layerdecay_20260908d`, not in Ai2's docs), which
needs `olmoearth_pretrain`; that package pins torch < 2.10 and pip replaced the CUDA build with a
CPU one, so torch 2.9.1+cu128 / torchvision 0.24.1+cu128 were force-reinstalled. Run: 768 s, peak
GPU 4.6 GB, 464 detections over the 185 km scene (27 inside the S2 tile, vs S2's 30).

- **Position:** 18 of 22 stationary S2 ships matched within 1 km, almost all at 0.01–0.09 km.
- **Small craft missed:** 6 of 7 moving S2 ships unmatched, all 16–88 m (also unmatched with no
  time shift); the one large moving ship (180 m) matched. Consistent with 15–30 m pixels.
- **Length unusable for large ships:** 330 m tankers read ~200 m; no Landsat length above 224 m;
  Landsat minus S2 on matches −137 to −9 m, median −91 m. The 15 m pan crop shows only ~10 px
  (~150 m) of bright hull, so the image, not the model, limits it.

Set from this in `rules/evidence_graph.json`: Landsat `use_length: false`, `min_ship_length_m: 100`,
and Landsat ↔ S1 pairs `compare_length: false` (`basis: stage0`): Landsat matches on position only.

### Still to run

- Systematic by-eye count of missed and false detections on a crop.
- Go / adjust / fall back decision.

## Columnar storage for alerts and findings (measured 2026-10-12)

Option for stages 4 and 6: keep alerts, findings and vessel positions as GeoParquet partitioned by
date (`date=YYYY-MM-DD/`), query them with DuckDB, and serve the map either as tiles generated
from those files or as query results. No database server, which matches the "files in S3"
decision.

Measured in `vessels-aggregator` (DuckDB 1.5.6 with the spatial extension):

| Step | Data | Time |
|---|---|---|
| Rule check over all stored snapshots (group by vessel, compare positions) | 43 parquet files, 92,528 rows, on the 1 GB Lightsail host | 0.08 s (+ 0.18 s startup) |
| Write rows as date-partitioned parquet | ~65k rows, 2 dates | 0.13 s |
| Read one date | 34,330 rows | 0.02 s |
| One vector tile (`ST_AsMVT`) for one date, straight from the parquet files | z5 tile, 64 KB | 0.03 s |

- Parquet is columnar: a query reads only the columns it uses, and DuckDB filters by the `date`
  partition before reading, so cost follows the dates asked for, not the archive size.
- One file per write (per job, per snapshot) and no appends: parquet can't be appended to; a day's
  files can be compacted later.
- Map display, two ways: tiles pre-generated per date into S3 next to `index.json` (fully static
  viewer), or a small endpoint on the intake host running DuckDB per tile request (no
  pre-generation, dates and filters chosen freely). DuckDB-Wasm can also read the parquet straight
  from S3 in the browser.
- Not measured yet: the same queries over S3 instead of local disk, and tile generation at higher
  zoom levels and over months of data.
