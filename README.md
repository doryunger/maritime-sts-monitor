# maritime-tip-cue

Watches known **ship-to-ship (STS) transfer hot spots** in free satellite imagery. On every new
Sentinel-1 or Sentinel-2 pass over a hot spot it detects the ships, checks each tanker for a second
hull alongside (a possible STS transfer), and then uses AIS to name the vessels it sees, or to show
that they were not broadcasting. AIS alone can also point to a possible STS place and time outside
the hot spots; that is a weaker lead, checked on the next pass.

AIS collection lives in the separate `vessels-aggregator` repo; this repo only reads its archive.
The staged work is in [PLAN.md](PLAN.md); measured feasibility data is in
[docs/feasibility.md](docs/feasibility.md).

## Flow

```
hot-spot list (S3, shared) ──> scheduler: new S1 / S2 passes over each hot spot (STAC)
                                   │
                                   v
                               GPU job, per pass and hot spot
                                 1. read the hot-spot crop of the scene
                                 2. detect ships, drop those near shore
                                 3. STS pair check per tanker (one hull or two)
                                 4. AIS enrichment: archive positions near the pass time,
                                    projected and matched -> vessel names, or "no AIS"
             S3 <── sts/<DD-MM-YYYY>/<hot_spot>/<source>.parquet ─┘
viewer ──reads──> S3

AIS STS candidate pairs (vessels-aggregator sts_pairs.py) ──> weaker lead: place and time
                                                               checked on the next pass
```

Layers: **hot spot** (a site where STS is known to take place) → **pass** (one scene over it) →
**finding** (one STS pair seen at that pass: pair verdict, AIS identity, crop).

## Hot spots

One list for all repos, kept on S3; each repo reads it from there. Until it moves, the proof-of-
concept list is `rules/sts_pairs.json` in `vessels-aggregator`:

| Site | Area | Notes from the PoC (15 Jul – 15 Oct 2026) |
|---|---|---|
| `malaysia_eopl` | EOPL / Singapore Strait east, 103.5–106.0 E, 0.8–1.8 N | S1 on 27 days, S2 38 clear days; 74% coastal AIS; shore distance 2 km |
| `johor_east_scs` | Johor east / South China Sea, 103.5–106.0 E, 1.8–2.8 N | S1 27 days, S2 35 clear days; 28% coastal AIS, mostly satellite AIS |
| `fujairah_outer` | Outer Fujairah / Khor Fakkan, 56.3–57.2 E, 24.8–25.8 N | S1 60 days, S2 40 clear days; 76% coastal AIS |
| `off_sohar` | Gulf of Oman off Sohar, 56.6–57.6 E, 24.2–24.9 N | S1 36 days, S2 41 clear days; 44% coastal AIS |

Per site: bounding box and minimum distance from shore (default 10 km); detections nearer to land
are dropped.

## Processing

1. **Pass search**: Sentinel-1 GRD and Sentinel-2 L1C scenes covering each hot spot since the last
   run, via STAC.
2. **Detection**: Ai2 detectors (one per sensor) on the hot-spot crop. Output per ship: position,
   length, width, heading, type. Detections on land or within the site's shore distance are dropped.
3. **STS pair check** (`maritime_tip_cue/sts/`, parameters in `rules/sts_graph.json`): per tanker,
   `pair`, `possible_pair`, `single`, `moving` or `unmeasured`. Sentinel-2 uses the hull's spectral
   width and a second-hull search beside it; Sentinel-1 uses the radar outline and two light lines.
4. **AIS enrichment**: from the `vessels-aggregator` archive, the AIS positions in and around the
   hot spot near the pass time, projected to the capture time, matched to each hull by position,
   length and heading. Result per hull: named (name, flag, type, size; IMO where the aggregator's Equasis lookup
   resolved it; the archive has no MMSI), or no AIS match. A
   pair with one or both hulls unmatched is a stronger lead, since a ship there without AIS is
   hiding. Matching parameters go in a rules graph, `null` / `basis: "tbd"` until measured.
5. **AIS-only leads**: STS candidate pairs from AIS (`sts_pairs.py`) are recorded as weak leads and
   checked on the next pass over that place.
6. **Publish** the STS findings per date, hot spot and source (see Output).

## Output

Public-facing results live in S3, one parquet per source, and the folder tree reflects the findings:

```
sts/<DD-MM-YYYY>/<hot_spot>/s1.parquet
sts/<DD-MM-YYYY>/<hot_spot>/s2.parquet
```

- `<DD-MM-YYYY>` is the UTC capture date; `<hot_spot>` is the site name from the hot-spot list.
- A file is written only when that source has STS findings (`pair` or `possible_pair`) at that hot
  spot on that date. No findings at a hot spot means no hot-spot folder; no STS anywhere on a date
  means no date folder. Passes with no STS, `single`, `moving` and `unmeasured` hulls are not
  published.
- One row per STS finding: scene id, capture time, hull position and geometry, size, verdict and its
  measurements, AIS matches per hull (MMSI, IMO, name, distance, reasons) or no AIS, and the crop
  image path. Crops with the detections and projected AIS positions drawn on sit next to the parquet.
- Listing `sts/` gives the dates with STS; listing a date gives its hot spots. The viewer, a static
  site, reads the tree directly, with no separate index.

## Where it runs

| Machine | Uptime | Runs |
|---|---|---|
| **Scheduler host** (existing Lightsail) | 24/7 | Looks for new passes over the hot spots, starts the GPU instance and hands it the jobs |
| **AWS GPU instance** (existing) | Per batch | Job server started on boot: crop, detection, pair check, AIS enrichment, upload; shuts down when its queue is empty, with a max-runtime watchdog |
| **ubuntu3** | 24/7 | The `vessels-aggregator` AIS archive (GizmoSQL), queried for enrichment |
| **S3** | — | Hot-spot list, STS findings tree, crops |
| **Shadow cloud PC** | Dev only | Editing, git, the stage 0 detector bench |

## Sources

| Source | Access | Cost |
|---|---|---|
| Sentinel-1 GRD | Planetary Computer (free) or AWS requester-pays | Free / pennies |
| Sentinel-2 L1C | Public GCS / Earth Search STAC | Free |
| AIS | `vessels-aggregator` archive on ubuntu3 | — |

## Decisions

- **Hot-spot driven, tip path dropped** (2026-10-19). Work is driven by new passes over known STS
  hot spots; the earlier design, where a tip naming one vessel started a search, is dropped.
- **One shared hot-spot list on S3** (2026-10-19), read by every repo; the PoC list in
  `vessels-aggregator` is the starting content.
- **AIS by querying the archive** (2026-10-19): this repo reads the `vessels-aggregator` archive
  directly by time and area around each pass; it does not collect AIS.
- **Sentinel-1 and Sentinel-2 only** (2026-10-19); Landsat dropped for now.
- **Output tree** (2026-10-19): `sts/<DD-MM-YYYY>/<hot_spot>/<source>.parquet`, written only where there
  are STS findings.
- **AIS-only STS pairs are weaker** than an imagery pair verdict: they add a place and time to check.
- **Shut down when idle**, not after each job.
- **No database server of our own**: findings are files in S3. The PostGIS scaffold is removed.
- Python workers; React/Vite/TS viewer.

## Code

`maritime_tip_cue/sts/`: the Sentinel-1 / Sentinel-2 STS pair check, driven by `rules/sts_graph.json`
and tested against `data/sts_fixtures` (`tests/`). The rest (`config.py`, a PostGIS schema and `initdb`) is
scaffolding from the earlier AIS-stream design, to be removed.
