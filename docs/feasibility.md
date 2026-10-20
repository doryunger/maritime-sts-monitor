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
scene. Stationary threshold (< 1 kn) and match distance (1 km) read from the rules graph (now `rules/sts_graph.json` `ais_match`).
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

The 1 km match distance is now the AIS match distance (`rules/sts_graph.json` `ais_match`). The
stationary S1–S2 pairing values (30 h, 30 m) left with the tip-era rules (tag `tip-era-final`).

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

Landsat was dropped on 2026-10-19; the app uses Sentinel-1 and Sentinel-2 only.

## Columnar storage for alerts and findings (measured 2026-10-12)

Option for stages 4 and 6: keep alerts, findings and vessel positions as GeoParquet partitioned by
date (`date=YYYY-MM-DD/`), query them with DuckDB, and serve the map either as tiles generated
from those files or as query results. No database server, which matches the "files in S3"
decision.

Measured on the AIS archive's parquet files (DuckDB 1.5.6 with the spatial extension):

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

## AIS archive limits for enrichment (recorded 2026-10-12)

The AIS archive is public vessel-map tiles read every 30 min, cargo and tanker
types only (AIS ship types 7 and 8), 30 days kept. What that means for matching hulls:

- **A sample, not full traffic.** ~13% of vessels drop out between snapshots, so a hull with no AIS
  match is not automatically a dark ship.
- **No MMSI or IMO** in the rows: name, flag, type and dimensions only; IMO comes from the
  archive's `identities` view where it was resolved.
- **Report time** comes from each tile's `Last-Modified` (cached up to 30 min by the provider's
  CDN), so a position can be up to 30 min older than its snapshot.
- **Satellite-only rows** (`[SAT-AIS]`) get a new anonymous id on every serving and carry no
  identity or size; they can't be tracked or matched by length.
- **Small craft are not stored**, so a tanker alongside a bunker barge shows in imagery only.

Saved-run match coverage (32 pair findings, runs 1-5) is coverage only, not match accuracy: there is no vessel-identity
ground truth to say whether a selected name is correct. Counts are reported per hot spot and source as findings with
both slots matched / one slot matched / neither matched:

| Hot spot | Source | Both | One | Neither | Findings |
|---|---:|---:|---:|---:|---:|
| fujairah_outer | S1 | 1 | 8 | 1 | 10 |
| fujairah_outer | S2 | 0 | 2 | 0 | 2 |
| off_sohar | S1 | 0 | 13 | 5 | 18 |
| johor_east_scs | S1 | 0 | 1 | 0 | 1 |
| malaysia_eopl | S1 | 1 | 0 | 0 | 1 |

The imbalance is a reason to retain candidate evidence for QA, not evidence that one specific rule caused the misses.
At off_sohar, 181 satellite candidate rows were seen and 21 were within 300 m; one was older than the current 60-minute
gate and 20 exceeded the distance gate. Satellite candidates remain excluded because they do not provide stable vessel
identity or dimensions. These saved-run counts do not establish a general threshold across the hot-spot list.

## Sentinel-2 hull shape test (measured 2026-10-19)
Set: 192 labelled stationary S2 hulls, 36 pairs and 156 non-pairs: the QA of runs 1 and 2 (fujairah_outer,
9 and 12-19 Oct 2026, with the user's recheck of 44971, 45616/45617, 46095/46096) and the user-labelled S2
fixtures. Mostly one hot spot; the limits are fitted on these labels.

| Feature (per hull) | AUC pair vs non-pair | Pair median | Non-pair median |
|---|---|---|---|
| step significance, edge-noise units | 0.91 | 29.6 | 4.2 |
| step size, beams | 0.89 | 0.63 | 0.15 |
| continuity of the stepped stretch | 0.88 | 1.00 | 0.10 |
| raggedness of the stepped edge, noise units | 0.84 | 0.95 | 0.34 |
| edge parallelism, noise units | 0.75 | 2.0 | 0.5 |
| symmetry (other edge shift), noise units | 0.64 | 1.2 | 0.4 |
| whole-object pixel histogram bimodality, best band | 0.49-0.59 | | |
| stepped strip vs hull A histogram, KS B08 | 0.25 (lower for pairs) | 0.38 | 0.78 |

| Verdicts | Pairs called pair | Non-pairs called pair | Non-pairs called possible |
|---|---|---|---|
| rules before the shape test | 29 / 36 | 4 / 156 | 4 |
| with the shape test | 36 / 36 | 1 / 156 | (possible dropped for S2) |

Re-check of runs 1 and 2 against the QA: 18 of 18 confirmed S2 pairs called pair, one false pair left
(49844_-275454, a ship with a haze trail beside it). The 6 S2 `moving` hulls the user judged stationary are a
separate error (Ai2 speed 3.4-7.4 kn near cloud, no wake) and are not touched by this test.

## Sentinel-1 outline shape (measured 2026-10-19)
Set: 52 labelled pairs and 306 non-pairs: QA of runs 2, 3 and 5 (fujairah_outer, off_sohar, malaysia_eopl) and the
S1 fixtures of Hormuz 2026-10-04 and Fujairah 2026-10-16. Against the 92 wide non-pairs (at least 1.4 beams), AUC:
light-line separation 0.94, fill 0.83, width variation 0.78 (lower for pairs), overlap 0.73, width in beams 0.70,
edge straightness 0.60, centreline 0.56.

| Rule | Pairs called pair | False pairs |
|---|---|---|
| before (width >= 1.65 beams, overlap >= 0.72, separation >= 0.28, width <= 3.0) | 47 / 52 | 1 / 306 |
| shape (width 1.55-2.5 beams, separation >= 0.26, fill >= 0.6, overlap >= 0.72 or >= 0.45 with fill >= 0.7) | 51 / 52 | 0 / 306 |

Held out per source (fit on the other four): Hormuz 17/17 (was 15), off_sohar 17/17 and 0 false (was 15 and 1),
Fujairah fixture 7/8, EOPL 1/1, run 2 9/9 with 3 false when its labels are left out (the fill limit then drops to
0.58): the fill margin is thin. Re-check of runs 2-5 against the QA: 27 of 27 reviewed pairs called pair, no false
pair; the 3 hulls left in the possible band (run 2) are all non-pairs.

## Sentinel-1 thin second return audit (measured 2026-10-20)

Run 4 hull `w0_49165_21194` at Johor was called single and corrected to pair in QA. Its extracted bright lines are
286 m and 160 m long, with separation 0.263 and overlap 0.745. The outline rule still rejects it: fitted length
406 m exceeds 400 m, width 1.45 beams is below 1.55, and fill 0.485 is below 0.60. The raw crop has two adjacent
elongated returns, but the lower-right return is diffuse, so the line measurements alone do not prove a pair.

A retrospective QA cue requiring length 150-430 m, width at least 1.3 beams, separation at least 0.26, overlap at
least 0.72, fill at least 0.45, and two extracted lines at least 100 m long flags 13 of 1,267 stored S1 singles or
possible pairs across runs 2-5: 8 Fujairah, 4 off Sohar, 1 Johor, 0 EOPL. Seven were already reviewed: one corrected
pair (this case), three singles, and three ambiguous cases involving suspected structures or a smaller adjacent
vessel. Six had no QA flag at the time of the audit. This is a small, selected sample, so the cue is suitable for
prioritizing QA, not for changing the automatic pair verdict yet.

The six unreviewed crops were then inspected raw and with overlays. Most showed one elongated return or clutter;
`w0_47515_271699` was visually ambiguous but not a clear pair. A cross-track VH profile of the Johor case had a
broad plateau with only a roughly 1-2 dB notch. Similar shallow shoulders or irregular profiles appeared in the
unreviewed crops and reviewed singles, so a simple two-peak valley test did not separate these cases. The two-line
cue remains a QA priority signal; none of the six was assigned a verdict from this inspection.

## Sentinel-1 lower brightness cutoff sweep (measured 2026-10-20)

The outline includes pixels at or above the local peak minus `outline_drop_db`; increasing the drop from 15 dB
admits dimmer pixels. Stored chips for 1,298 S1 objects across 12 passes in runs 2-5 were evaluated in memory at
15, 17, 19 and 21 dB, leaving every other rule unchanged and writing no run data.

| Drop below peak | Reviewed pairs called pair | Reviewed singles called pair | Pair calls across runs 2-5 |
|---:|---:|---:|---:|
| 15 dB | 30 / 31 | 0 | 30 |
| 17 dB | 24 / 31 | 1 | 28 |
| 19 dB | 20 / 31 | 0 | 21 |
| 21 dB | 8 / 31 | 0 | 9 |

The Johor corrected pair stayed `single` at every cutoff. Its width grew from 1.45 beams at 15 dB to 2.08 at
17 dB, but measured light-line separation fell from 0.263 to 0.121, below the 0.26 pair threshold. At 19 and
21 dB the fitted length also exceeded the 400 m maximum. Lowering the brightness cutoff therefore did not fix this
case and lost previously detected reviewed pairs in every other hot spot. The active 15 dB rule was kept.

## EOPL and Johor expansion survey (prepared 2026-10-20)

The boundary and coverage map, SVG, GeoJSON, summary and reproducible `build_map.py` are outside the app repo at
`C:\Users\Shadow\projects\maritime-sts-analysis\2026-10-20-malaysia-survey\`.
The map was visually checked against local coastlines. Current boundaries were read from the shared S3 list.
Coverage polygons come from the recorded detector prediction windows in runs 1-5: pixel bounds multiplied by
their signed resolutions, transformed from their recorded CRS to WGS84, then clipped to each configured box.
They show nominal processed extent, not usable water, cloud-free area or independent observations.

| Hot spot | S1 scenes | S2 scenes | Capture dates | After-QA pair hull records |
|---|---:|---:|---|---:|
| malaysia_eopl | 4 | 0 | 16-17 Oct 2026 | 1 |
| johor_east_scs | 3 | 0 | 16-17 Oct 2026 | 2 |
| fujairah_outer | 6 | 13 | 9-19 Oct 2026 | 38 |
| off_sohar | 3 | 8 | 13-19 Oct 2026 | 27 |

Counts are scene records, not independent passes; pair hull records are not unique STS events or proof of
cargo transfer. No S2 observations were processed for either Malaysian site. These short Malaysian samples
do not establish low activity. The added waters have no processed observations in these runs.

The draft survey boxes are EOPL `[103.0, 0.3, 106.5, 1.8]` and Johor `[103.5, 1.8, 106.5, 3.3]`
(west, south, east, north). Each extends selected existing edges by 0.5 degrees, approximately 55 km.
These are exploratory search envelopes, not measured activity boundaries. Their subsequent use for live
catalogue tracking is recorded below. Land and the existing shore-distance exclusion still apply. The map
predates the live catalogue baseline; it contains only the earlier processed observations.

## AIS collection coverage audit (2026-10-20)

The local collector's five enabled zoom-8 capture views jointly contain all four current/survey boxes,
including the 5 km padding applied by `ais_match.search_radius_km`. Exact polygon containment passes for
expanded EOPL, expanded Johor, Fujairah and Sohar with zero uncovered area. No collector region edit is
needed for this geometry. This establishes configured geographic coverage, not completeness of AIS reports.

Checking one view at a time would falsely report a Johor gap: `singapore_johor` reaches only 3.275864 N,
but the overlapping `south_china_sea_north` view covers the rest of the expanded 3.3 N boundary and the
query margin. Likewise, `hormuz_fujairah` covers Sohar's northern edge. Coverage must use the union of
enabled views. The collector does not automatically read this repo's S3 hot-spot list; its capture view
configuration is independent. This repo's AIS matcher uses the run's stored boundary snapshot, which
already records the expanded EOPL box for run 6 and will record Johor's when its queued run starts.

The audit evaluated the local collector configuration and its viewport geometry helper, using signed
Web Mercator latitude bounds and the monitor's own AIS-padding function. `audit_ais.py`, `ais-coverage.json`
and the visually checked `ais-coverage.png` are alongside the survey artifacts in the external analysis
directory above. They are audit artifacts, not a runtime dependency between the two repos.

Live verification subsequently succeeded using this PC's existing SSH key and the host from the ignored
AIS archive URI. The deployed viewport and all five enabled view definitions match the local configuration
exactly. Thus the configured live collection geometry also covers every current/survey box and query margin.

## Always-on host placement audit (2026-10-20)

These measurements describe ubuntu3 before migration. The current `GIZMOSQL_URI` points to the replacement
host and must not be used to locate ubuntu3. Source connection metadata is preserved privately in the
external migration directory described below. No endpoint, key material or credentials belong in Git.
The read-only audit script `inspect_ubuntu3.py`, baseline `ubuntu3-audit.json` and
`ubuntu3-capture-sample.json` are in the external survey analysis directory above; its original URI-based
target lookup requires the frozen source metadata before reuse against ubuntu3.

| Observation | Measured value |
|---|---:|
| CPUs visible to Linux | 2 |
| RAM visible to Linux | 911.3 MiB |
| Available RAM at initial idle snapshot | 373.1 MiB |
| Swap allocated / initially occupied | 2,048 / 522 MiB |
| Lowest available RAM during capture sampling | 256.4 MiB |
| Highest total swap occupancy during sampling | 759.5 MiB |
| Scheduled capture cgroup peak RAM | 529.9 MiB |
| Highest sampled capture cgroup swap occupancy | 217.7 MiB |
| Root disk available / used | 11 GiB / 72% |
| AIS archive size | 189 MiB |

The capture sample covers nine observations over 40 seconds during the existing 12:15 UTC cron job;
the job was not triggered for this audit. It is not a day-long capacity study or a concurrent-query test.
The initial load average was zero. Swap occupancy increased during capture; occupied swap alone is not
an ongoing swap-rate measurement. OOM history could not be established from the kernel journal.

The live machine also hosts two unrelated wake controllers and a media app. The GizmoSQL container has
a 450 MiB RAM cap and 800 MiB combined RAM/swap limit. The capture has a 600 MiB RAM cap and 300 MiB swap
cap. Those RAM caps alone exceed total physical memory when reached together, before the OS, other apps
or a tracker. Successful recent capture logs do not establish capacity for simultaneous database queries.

The shared memory pressure motivated the dedicated AIS/control host selected below. The measured migration
validation includes a new capture overlapping archive queries; sustained production query peaks remain
unmeasured. Heavy imagery processing remains assigned to the EC2 worker.

DuckDB's configured memory limit is not a complete process-memory ceiling; its documentation describes
allocations outside the buffer manager ([DuckDB OOM guidance](https://duckdb.org/docs/current/guides/performance/oom)).
For a burstable Lightsail host, sustained CPU capacity must also be checked rather than inferred from idle
CPU alone ([AWS CPU burst capacity](https://docs.aws.amazon.com/lightsail/latest/userguide/monitoring-cpu-burst-capacity.html)).
CPU burst metrics and representative concurrent-query peaks remain unmeasured.

## Dedicated control host preflight (2026-10-20)

At initial preflight, SSH to the user-supplied dedicated Lightsail host succeeded. Measured resources differ from the earlier
1 GiB / 40 GB candidate: Linux reports 1,906 MiB RAM, two CPUs and a 58 GiB root filesystem with 56 GiB
available. Initial available memory was 1,514 MiB, without swap. The OS is Ubuntu 24.04.4 LTS
with Python 3.12.3 and Git. Docker and project checkouts were absent at preflight; their deployed state is
recorded in the migration verification below.

Windows OpenSSH rejected the downloaded key's broad ACL. A separate copy in the user's `.ssh` directory,
restricted to the current Windows account, connected successfully. The original Downloads file was not
modified. Connection settings are in ignored `data/secrets/control_host.json`; endpoints and key material
are excluded from these notes and Git. This host is selected for the always-on AIS/control workload;
heavy scene processing remains on the EC2 worker, which stops after processing.

## AIS deployment migration verification (2026-10-20)

AIS collection and GizmoSQL now run on the dedicated Lightsail host. The source repository, untracked data,
reference files, logs, service state and browser cache were transferred; the Python environment was rebuilt
from the source's exact 15 package pins. Initial verification matched all 1,891 transferred files; the final
quiescent repository sync matched all 1,287 repository files with no missing, extra or changed hashes.

Before cutover, authenticated TLS queries on the new host matched the source: 2,964,489 rows, 273 captures
and 499 resolved identities. A complete new-host capture and analysis cycle then succeeded, adding 10,588
rows and one capture. Concurrent client queries also succeeded. The resulting archive has 2,975,077 rows,
274 captures and 499 identities. Latest-capture counts inside the four current/survey boxes are EOPL 1,255,
Johor 290, Fujairah 403 and Sohar 196; these are AIS rows, not imagery findings.

The original hourly capture schedule (`15 * * * *`, server UTC) is enabled on the new host. Docker and cron
are enabled at boot, and GizmoSQL retains the source image digest and `unless-stopped` restart policy.
The host has 2 GiB swap, matching the source. The four-daily catalogue tracker and EC2 orchestration are
separate implementation work and were not deployed as part of this migration.

The user opened the original archive TCP port in Lightsail. The archive retains authentication and TLS,
with a certificate for the new host. Both PC clients now use the new endpoint; certificate copies are in
ignored `data/secrets/` directories. The aggregator client supports `GIZMOSQL_TLS_ROOT_CERT` so its tracked
legacy certificate need not be replaced with deployment material. Connection settings stay in ignored
`.env` and `data/secrets/control_host.json`. The new host's private deployment settings, reproducible archive
starter and package freeze are in `~/.config/sts-ais/`; do not copy private settings into Git.

The old AIS capture cron, GizmoSQL container, repository/data, GizmoSQL state and AIS browser cache were
removed from ubuntu3 only after validation. Its three unrelated running containers and two host/security
cron entries were checked unchanged. The old archive port no longer has a listener.

Private migration inventories, before/after validation, complete hash manifests and
`source-final-backup.tar.gz` are retained outside the app repository at
`C:\Users\Shadow\projects\maritime-sts-analysis\2026-10-20-ais-migration\`.
The backup is 331,410,246 bytes and contains source repository data and GizmoSQL state; the source venv
and reconstructible browser cache are excluded. Its SHA-256 is
`ecd6018229adb94727f0fc9cefeaa885ce82cb09122e1835f0d0e884360074b6`.

## Catalogue tracker and handoff verification (2026-10-20)

The dedicated Lightsail host now runs the catalogue tracker through a persistent systemd timer at 00:05,
06:05, 12:05 and 18:05 UTC. The user selected baseline-only initialization and the expanded Malaysian boxes;
the shared hot-spot object was updated conditionally, preserving the other regions and shore exclusions.
Its previous and updated documents are retained outside the repo with the validation artifacts.

| Hot spot | Baseline S1 records | Baseline S2 records | New records on repeat scan |
|---|---:|---:|---:|
| malaysia_eopl | 4 | 1 | 0 |
| johor_east_scs | 4 | 1 | 0 |
| fujairah_outer | 6 | 9 | 0 |
| off_sohar | 5 | 7 | 0 |

Both live scans succeeded on all eight channels. The 37 eligible baseline records produced no historical
jobs; the second scan produced no duplicate jobs. Scene identities and counts are in the JSONL scan log;
SQLite retains observations, baseline markers, outbox and scan history. The enabled timer's next activation
was verified through systemd. Service inactivity between polls is expected for a oneshot service.

The retained integration suite passed 54 tests: 15 tracker cases and 39 worker cases, covering all four sites,
both sensors, first-run failures, late S1 slices, new arrivals, rule/boundary versions, duplicate suppression,
S3 write retries, corrupt/missing receipts, artifact verification, claims, explicit resume and queue draining.
Real GeoParquet exports were checked for all hull roles, empty schemas and WKB coordinate order.

A separate real-S3 test exercised four isolated jobs through immutable dispatch, exclusive claims, uploads,
success receipts and tracker reconciliation. It uploaded 20 artifact objects and checked downloaded Parquet
contents; all 32 objects in that validation namespace were deleted and the prefix was verified empty.
Detector execution was mocked for that S3 test. No actual EC2 GPU job, instance wake or shutdown was run.

Existing detector fixtures were also run: five passed and `test_s2_scores` failed on one single classified
as pair. Repeating that test with the previously committed rule graph produced the identical failure and
confusion counts. Detection rules were not changed to hide the pre-existing classifier failure.

Tests, S3 validation script/report, live scan summaries and deployment bundle are retained outside the app
repo under `C:\Users\Shadow\projects\maritime-sts-analysis\2026-10-20-catalogue\`.
Use `tests/` there for the tracker/worker suite and `s3_handshake.py` for the isolated S3 check; the passing
S3 report is in `s3-handshake-47a8e8a1b48d4a8b8a236834cc805858/report.json`. Runtime behavior and recovery are
documented in [the component context](../maritime_tip_cue/context/catalogue.md).

## Sentinel-1 missed pair similarity audit (measured 2026-10-20)

The user corrected run 6 EOPL hull `w0_45473_19759` to pair. Rebuilding its stored chip reproduces the stored
measurements. Its bright lines are 352 m and 191 m long, separated by about 57 m across the fitted axis.
The combined outline measures 473 by 108 m. This is a case where strong line evidence is overridden by the
combined outline checks:

| Measurement | This case | Current pair requirement | Result |
|---|---:|---:|---|
| Outline length | 472.67 m | 150-400 m | Fails maximum |
| Width after blur correction, in estimated beams | 1.00 | 1.55-2.50 | Fails minimum |
| Light-line separation / outline width | 0.530 | At least 0.260 | Passes |
| Light-line overlap | 0.950 | At least 0.720, or boxy exception | Passes |
| Outline fill | 0.544 | At least 0.600 | Fails |

Width in beams is `6 * (outline_width - pass_blur) / outline_length`. An oversized outline length therefore
penalizes both the length gate and normalized width; sparse outline extensions also reduce fill. The diagnostic
plot shows a longer main return beside a shorter return, with outline extensions beyond the extracted bright
lines. This supports investigating per-vessel evidence rather than relaxing all three combined-outline gates.
The bright line lengths are radar measurements, not independently established vessel dimensions.

The inventory covered 1,672 stored S1 objects in registered runs 1-8, deduplicated to 1,396 by region, scene and
hull ID, choosing the latest run's measurements. The selected records contain 1,362 stored singles, 31 pairs and
3 unmeasured objects. Latest available QA was retained across reruns. Run 8 was processing during the snapshot;
later detections are absent. Different IDs or adjacent scene slices can still refer to the same physical vessel,
and reruns with different boundaries are not a controlled validation dataset.

The broad review signal selects stored singles that pass the existing light separation and overlap predicates,
including the boxy-overlap exception, but fail at least one length, width or fill gate. The exact pattern selects
those failing only maximum length, minimum width and fill, as in the corrected EOPL case. No thresholds were fitted.

| Hot spot | S1 objects | Broad review signal | Exact failure pattern | Exact pattern QA |
|---|---:|---:|---:|---|
| malaysia_eopl | 166 | 12 | 1 | This corrected pair |
| johor_east_scs | 213 | 18 | 7 | One earlier corrected pair, six unreviewed |
| fujairah_outer | 726 | 26 | 0 | No exact matches |
| off_sohar | 291 | 12 | 0 | No exact matches |

The broad signal contains 2 reviewed pairs, 7 reviewed singles, 3 other QA exclusions and 56 unreviewed cases.
The exclusions include suspected platforms and a smaller adjacent vessel; they are not all confirmed non-pairs.
Visual inspection of the exact eight and selected controls from every region shows that similar failed gates can
also come from oversized merged returns or structures. These candidates must not all become automatic pairs.

An exploratory screen requiring both extracted bright lines to fall within the existing 150-400 m outline range
reduces the broad group to 24: EOPL 4, Johor 5, Fujairah 10, Sohar 5. It retains both reviewed pairs but also one
reviewed single and all three other exclusions; 18 are unreviewed. Five of the six new exact-pattern Johor cases
have a bright line longer than 400 m. Applying an outline length range to each bright line is an unvalidated
experiment, not a proposed production threshold or a demonstrated precision gain.

Recommendation: surface the conflicting evidence as a review reason first, preserving the automatic verdict.
For a detector change, test two independently supported elongated ridges, each with its own orientation,
longitudinal support and extent, against a single-ridge explanation. The current extraction forces the two lines
to share the outline angle: drawing two parallel lines is therefore not independent proof of two parallel hulls.
Measure the weaker return's continuity and support along the stronger one, and fit each return's shape without
letting dim tails define the whole object's length. Validate against reviewed singles and suspected structures
as well as corrected pairs in all regions; new acceptance thresholds remain undetermined. Earlier brightness
cutoff sweeps and a simple cross-track valley test already failed to separate the earlier Johor case reliably
(see the preceding thin-return and cutoff audits), so repeating those as an automatic fix is not supported.

Read-only scripts, object inventory/CSV, diagnostic plots and `review.html` are outside the repo at
`C:\Users\Shadow\projects\maritime-sts-analysis\2026-10-20-pair-review\`. The gallery contains raw and overlay
images for all 68 broad candidates (136 images, zero render failures), the eight exact-pattern cases first,
existing QA and links back to the app. Unreviewed singles may be absent from the app's sampled card list;
the gallery still shows their stored images. No new QA labels, classifier rules or automatic verdicts were changed
by this audit.

## Sentinel-1 false pair w0_46174_276956 (measured 2026-10-21)

Run 8, Fujairah, Sentinel-1 capture 2026-10-19 14:16 UTC: `w0_46174_276956` at
25.041182 N, 56.620814 E. The original outline-only verdict reproduces as `pair`; the user's saved
`wrong_verdict` correction made the `findings` verdict `single` before the geometry fix below.
The raw chip and reconstructed overlay were visually checked. One elongated return has a brighter head;
the two-line fit divides its bright pixels into lines of 166 m and 66 m. Fitting two lines does not establish
two separate hulls, and the pair rule does not require two Ai2 detections (this object has one).

| Pair check | Measured | Active requirement |
|---|---:|---|
| Fitted length | 229.823 m | 150–400 m |
| Width after pass blur correction, in estimated beams | 1.853 | 1.55–2.50 |
| Light separation / fitted width | 0.269766 | >= 0.26 |
| Along-line overlap | 0.810637 | >= 0.72 |
| Outline fill | 0.650533 | >= 0.60 |

Every check passes the ordinary pair branch; the boxy exception is unnecessary. The estimated beam is
length / 6, and pass blur is 28.012 m, leaving 70.980 m corrected radar width. The Ai2 box is only
179 by 30 m. This discrepancy explains why the outline-based rule can interpret the single return as wide.
The immediate classification failure is that this single return satisfies the empirical two-line shape test.

AIS lookup did run. `ais_candidates` records VISHVA VIKAS (Cargo, AIS size 189 by 32 m), eligible for both
slots and assigned once to slot b, with report age 9.059 min. Its stored candidate distances are 55.746 m to
slot a and 7.355 m to slot b; LIBERTY HARVEST is rejected at over 3.5 km. No second eligible named ship is
recorded. AIS enriches the radar verdict rather than determining it; absence of a second identity cannot alone
prove a single vessel. The displayed 56 m comes from `ais_matches.dist_a_m`; S1 currently stores no
`dist_b_m` even for a selected slot b, so this display is not the selected slot's actual distance.

The missing 1/2 markers are a display consequence of QA, not a missing search: `HullCard.tsx` displays them
only when `final_verdict` is `pair`. This record is already `single` after QA, so markers and the numbered AIS
layout are hidden while the original PAIR badge and historical match remain. Recomputing `vessel_points`
returns both valid points inside the crop, at fractions (0.529, 0.522) and (0.546, 0.422).

Sensitivity was evaluated in memory on all 2,168 stored S1 hull snapshots in runs 2–9, across every shared hot
spot, and separately on 261 recomputed S1 fixture objects. No S2 rules are affected. Counts include repeated
observations across runs; these are not independent vessels. QA labels are available for only part of the data.

| Site | Stored hulls | Baseline pair calls | QA pairs detected / labelled |
|---|---:|---:|---:|
| fujairah_outer | 1,064 | 19 | 18 / 18 |
| off_sohar | 449 | 26 | 18 / 18 |
| malaysia_eopl | 308 | 2 | 2 / 2 |
| johor_east_scs | 347 | 3 | 3 / 4 |

Each proposed threshold below rejects the target. Cells show total pair calls removed / confirmed QA pairs
lost, with every other parameter held fixed. Only the target is a reviewed single removed in these sweeps;
other removed calls without labels cannot be claimed as fixes.

| Proposed minimum | Fujairah | Sohar | EOPL | Johor |
|---|---:|---:|---:|---:|
| Separation 0.27 | 2 / 1 | 1 / 0 | 0 / 0 | 0 / 0 |
| Separation 0.28 | 2 / 1 | 3 / 1 | 0 / 0 | 1 / 1 |
| Width 1.86 beams | 7 / 6 | 6 / 3 | 1 / 1 | 3 / 3 |
| Width 1.90 beams | 7 / 6 | 7 / 3 | 1 / 1 | 3 / 3 |
| Overlap 0.82 | 2 / 1 | 2 / 1 | 0 / 0 | 1 / 1 |
| Fill 0.66 | 4 / 3 | 4 / 1 | 2 / 2 | 0 / 0 |

The separate Fujairah fixture has 197 objects, eight labelled pairs, seven detected at baseline;
Hormuz has 64 objects, 17 labelled pairs, all detected at baseline. Separation 0.27 loses one currently detected
Fujairah fixture pair and no Hormuz pair. These fixture observations can overlap run observations and must not
be summed with them. Fixtures supply no separate EOPL, Johor or Sohar set; those sites are covered by runs.

The scalar sweeps do not justify changing those thresholds: even the nearest tested separation increase fixes
this case at the cost of a QA-labelled pair. These labels require geometric review rather than being treated
as independently verified hull counts. This example alone does not justify a vessel-type exclusion. The user's existing QA
Single correction is retained. Reproducer and exact sweep output are outside the repository at
`C:\Users\Shadow\AppData\Local\Temp\sts_target_sensitivity_20261021.py` and `.txt`; temporary visual verification
is `C:\Users\Shadow\AppData\Local\Temp\sts-target-audit.png`. Run the reproducer from the repository root with
`.venv/Scripts/python`; it reads local databases and fixtures without changing verdicts.

## Sentinel-1 independent shape evidence (measured 2026-10-21)

Follow-up to the preceding false-pair audit: testing another separation cutoff is insufficient. The current
S1 extraction forces its two bright-pixel lines to share the outline angle, and therefore cannot use their
apparent parallelism as evidence of separate hulls. All measurements below are read-only experiments;
no automatic verdicts or QA labels were changed. Existing QA Pair labels are reference labels, not independently
verified counts. Their disagreements with shape evidence require review rather than automatic relabelling.

The experiment selected every current pair call and every QA-labelled S1 object in runs 2–9: 214 run records
from the 2,168-record inventory, plus all 261 S1 fixture objects. A veto cannot promote an unselected single,
so those singles are unaffected by the candidate veto experiments. Three selected objects have no rebuilt
outline. The local cross-section experiment still accounts for all 475 objects, with zero load failures.
Run and fixture counts include overlapping observations and must not be added as independent vessels.

| Site | Selected run records | Automatic pairs | QA-labelled pairs currently detected |
|---|---:|---:|---:|
| fujairah_outer | 67 | 19 | 18 |
| off_sohar | 50 | 26 | 18 |
| malaysia_eopl | 53 | 2 | 2 |
| johor_east_scs | 44 | 3 | 3 |

Independent PCA axes of each top-quarter bright-pixel group expose the target's weak second shape: the two
spans are 166.2 and 66.2 m; principal-axis elongations are 7.62 and 2.48. But demanding long, narrow bright
clusters alone also rejects QA-labelled pairs: the strongest quarter can omit a dimmer hull. Requiring that
a rejected object have both shorter/longer span below 0.45 and minimum elongation below 3 still rejects three
Fujairah, two Sohar and one Johor labelled pairs, plus one pair in each fixture set. This is a measurement
failure to investigate, not evidence that those labels are necessarily correct or necessarily wrong.

Free-orientation one-versus-two Gaussian shape fits were evaluated at brightness quantiles 0.25, 0.40, 0.50,
0.60, 0.75 and 0.85, using full-outline pixels rather than just the brightest quarter. The target consistently
splits longitudinally: component centres differ by 88–126 m along the outline and 14–24 m across it.
At the lower four quantiles it remains one connected elongated shape; the higher quantiles isolate bright
end regions. Independent model axes and raw pixels were visually checked. A matched-complexity lateral-versus-
longitudinal two-component comparison also favours longitudinal structure for the target at all six levels,
but several labelled pairs favour it too. These likelihood models explain intensity structure, not vessel count.

A separate experiment rotates the full chip into the outline frame, samples at 10 m and finds simultaneous
across-track peaks per longitudinal row. Peak separation is at least 20 m, Gaussian smoothing is 0.5 or
1 pixel, and minimum intervening-valley depth is 0.5, 1, 2 or 3 dB. With 0.5-pixel smoothing and 0.5 dB depth,
the target has two peaks in only 3 of 23 rows (13.0%), with 30 m continuous support. At 1-pixel smoothing and
3 dB it has none. Requiring at least 14% dual-peak rows is a retrospective screen, not a calibrated rule:

| Site | Calls removed | QA Pair labels rejected | QA Single labels corrected | Unreviewed removed |
|---|---:|---:|---:|---:|
| fujairah_outer | 3 | 2 | 1 | 0 |
| off_sohar | 3 | 0 | 0 | 3 |
| malaysia_eopl | 0 | 0 | 0 | 0 |
| johor_east_scs | 1 | 1 | 0 | 0 |

The same screen rejects one of seven currently detected Fujairah fixture pairs and none of the 17 Hormuz
fixture pairs. There are no separate Sohar, EOPL or Johor fixture sets; the run records cover those regions.
Combining independent elongated-group support (span ratio at least 0.45 and minimum elongation at least 3)
OR dual-peak support (at least 14%) retains all currently detected fixture pairs. It still rejects the target,
run 8 Fujairah `w0_44663_283894` and run 7 Johor `w0_49715_35557` (both labelled Pair), and unreviewed run 9
Sohar `w0_47112_272571`. These exploratory values were evaluated after seeing the target and are not held-out
validation. They must not be presented as established production thresholds.

Stored AIS assignments cannot resolve the conflicts. Baseline pair calls have two / one / zero distinct
selected named identities as follows: Fujairah 1 / 16 / 2; Sohar 0 / 21 / 5; EOPL 1 / 0 / 1; Johor 0 / 1 / 2.
The local-row screen's rejected QA-labelled pairs have one identity (run 2 Fujairah `w0_45398_278273`,
LOULOULIGHT II, tanker 229 by 32 m), or none (the two conflicting cases above). The target has one cargo-vessel
identity, as recorded in the preceding section. Requiring two identities would not rescue these labels and
would discard many other pair calls; missing identities are not evidence of a single vessel.

The full-silhouette experiment and implemented single-box veto are described below. A general two-box
classifier remains unresolved. The existing S1 fill/minimum rectangle is only a coarse footprint; the new
model follows intensity-derived edges rather than forcing a split of bright points. S2 thresholds are not
assumed valid for SAR.

Reproducers, raw measurements and plots are outside Git under
`C:\Users\Shadow\AppData\Local\Temp\`: `sts_geometry_20261021.py`, `sts_geometry_multilevel.py`,
`sts_geometry_directional.py`, `sts_local_ridges_20261021.py` and `sts_ais_fusion_20261021.py`, with their
same-stem output directories and `.txt` logs. Run from the repository root with `.venv/Scripts/python`.
`sts_geometry_gallery/review.html` contains raw crops and independent fits, existing QA and capture times,
including the three local-row conflicts and controls from all four hot spots. Its `raw_contact.png`,
`sts_geometry_multivis/multilevel_axes.png` and `sts_local_ridges_20261021/row_ridge_contact.png` were visually
checked. No conflicting label was changed by inspecting these plots.

## Sentinel-1 single-box veto (validated 2026-10-21)

Implemented `sts/s1shape.py` as a conservative veto of the existing pair rule. It samples VH in linear power,
subtracts a local sea median per row, traces half-peak boundaries over the central Ai2-referenced body, and
fits both sides with one shared slope. A noise-consistent parallel box whose perpendicular width agrees with
one Ai2 beam supplies single-hull evidence. All parameters are in `rules/sts_graph.json:s1_single_box`.
The length reference is Ai2's estimate, not a newly measured physical hull length; no old outline-blur correction
is applied to the new contour. The fitted box is drawn green, replacing the forced red pair lines when it
explains a single hull. The app exposes its width, width ratio, edge residual and measured body support.

The target fits a 32.57 m wide body against Ai2's 30.14 m width; its Ai2 length reference is 178.99 m and the
traced side support spans 140 m over 15 native-resolution rows. Joint edge residual is 1.143 noise units.
All central rows must have finite coverage and strong signal. Multiple own detections or unknown dimensions
cannot trigger the veto. A separately supported outside peak in any central row also prevents the veto:
without that guard, the brighter hull's half-power trace could hide a dimmer adjacent hull.

The alternative one-versus-two rectangle search was also measured on the 475 selected records. Unconstrained
two-box fits improve the target by putting a short extra box around its bright head. Requiring elongated,
plausibly proportioned components favours one box for the target but also loses many labelled pairs. This is
why the deployed change uses positive evidence for a single box plus the outside-return guard, rather than
requiring a general two-box classifier to succeed on every pair.

The final production implementation rebuilt every available S1 object in runs 2–9, grouped by run/site/pass
so the old pass-blur calculation was preserved, plus both complete fixture sets. The disabled-veto baseline
reproduced every stored verdict. Enabled-versus-disabled results:

| Site or fixture | Objects | Pair calls before | Pair calls after | QA Pair labels newly vetoed |
|---|---:|---:|---:|---:|
| fujairah_outer | 1,064 | 19 | 18 | 0 / 18 |
| off_sohar | 449 | 26 | 26 | 0 / 18 |
| malaysia_eopl | 308 | 2 | 2 | 0 / 2 |
| johor_east_scs | 347 | 3 | 3 | 0 / 4 |
| Fujairah fixture | 197 | 7 | 7 | 0 / 8 |
| Hormuz fixture | 64 | 17 | 17 | 0 / 17 |

Of 2,429 observations, only the requested target changes Pair to Single. There are zero processing errors,
zero baseline mismatches and zero nonfinite geometry records. Missing shape measurements skip the veto:
45 Fujairah, 16 Sohar, 29 EOPL, 21 Johor, seven Fujairah fixture and one Hormuz fixture. Labels and observations
can overlap across runs/fixtures; this is retrospective validation, not an independent holdout. All four
shared hot spots have run data; the separate regional fixture limitations remain as described above.

Eighteen targeted production checks passed, including six resolvable weaker-hull simulations at 10%, 20%
and 35% brightness and 50/70 m separation, a single-hull control, missing coverage/dimensions and multiple
own detections. Unresolved 30 m weak returns remain a sensing limitation. Native 10 m and oversampled 5 m
measurements agree on the target. Edge limits 1.2 and 1.5 change only this target; 1.8 also removes unreviewed
Sohar `w0_47194_273954`, so the selected limit retains that uncertain case.

The four S1 fixture tests pass. The full suite has five passes and one pre-existing S2 score assertion failure:
fixture `s3` number 9 (`hormuz_fujairah`) is labelled single but called pair. It reproduces with HEAD's original
rules, so no S2 expectation or detector code was changed to hide it.

Run 8 was locally rechecked: S1 pairs 9 to 8, singles 329 to 330. All 33 existing QA flags are byte-for-byte
unchanged as database rows; the target is now Single both automatically and after QA. The local viewer was
restarted to load the new overlay. This does not rewrite immutable completed-job artifacts.

Final read-only validation scripts and results are under `C:\Users\Shadow\AppData\Local\Temp\`:
`sts_geometry_production_validate.py` and its directory (`report.json`, `rows.json`, `sensitivity.json`,
`target.json`, raw and overlay PNGs), `sts_single_box_production_checks_20261021.py`/`.json`, and
`sts_geometry_s2_baseline.py`/`.txt`. The run 8 SQLite backup before recheck is
`sts_geometry_run8_before_recheck_20261021.db`. The rejected rectangle experiment is
`sts_rectangle_priors_20261021.py` and its same-stem directory. Run scripts from the repository root with
`.venv/Scripts/python`. Geometry plots and the production overlay were visually checked.
