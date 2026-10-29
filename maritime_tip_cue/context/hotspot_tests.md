## Area plan and catalogue

`hotspot-tests/areas.json` versions scan bounds separately from the S3 live hot-spot list. Test area IDs start with
`test_` so the shared Postgres job and review tables can store them without colliding with live sites. The first
catalogue pass queues the area's own `lookback_days` window (10 days) for a new test area; otherwise the normal
first-seen baseline would record scenes without scheduling them. The window is a per-area plan field, not the live
7-day `catalogue.lookback_days` in `rules/sts_graph.json`: a discovery version is a digest of the site and all rules, so
editing that file would re-queue every live site. `lookback_days` is deliberately not part of the site in the manifest,
so changing it does not create a new discovery version. Area version and bounds are part of the site in the manifest, so a
geometry change creates a new discovery version and run provenance.

## Worker outputs

`site.pipeline == "hotspot_test"` is the explicit pipeline boundary. `watch.jobs.execute` does not query AIS, the
worker skips both regular pair and sanctioned publishers, and the manager validates attempt artifacts in the test
prefix. The worker writes one pair-only GeoParquet per UTC date and sensor, including an empty Parquet when the
scene completed but produced no pair labels. Full detector/run state and images stay in the test artifact and
review prefixes for later audits; only pair and possible-pair cards are shown in the test review.

## Grid scan

`maritime_tip_cue/grid_scan.py` samples `hotspot-tests/grid.json` (533 Sentinel-2-sized tiles of sea between the four live hot spots that had S1 or S2 imagery in a 29-day window of 2026-10-29) and queues one scene per tile through the same `catalogue.record_channel` and job path as a catalogue scan. Rules are in `rules/grid_scan.json`; state is `processing.grid_scans` (one row per queued scene) and `processing.grid_visits` (one row per tile and round).

- Tiles are not in `areas.json`: `catalogue.scan` loads every area there and would queue each tile's whole lookback window. The planner builds a `test_grid_<tile>` site dict itself, so the manifest, worker and review page treat it like any test area.
- The tile id is a label. Zone-edge tiles carry the MGRS id of the neighbouring zone's tile for the same ground, because the footprint was built as a 109.8 km UTM square and named from its centre; the scene search uses the bounding box, never the id. Duplicate ids were resolved with the MGRS letter formula (zone, column letter from zone mod 3, row letter from northing mod 20 plus 5 for even zones).
- The planner runs on the control host, not on EC2: it needs Postgres and the catalogue lock, and the worker only drains the jobs it queues, so no worker image change was needed. It inserts a `processing.scans` row per run because `observations.scan_id` is a foreign key; the row is created as `ok` (a `running` row would be marked interrupted by the next catalogue scan) and `catalogue status` skips rows whose summary has `grid_scan`.
- A scan is resolved from `review.scenes` (`counts.checked`, `counts.verdicts`), not from the job status, so a superseded or retried job still resolves. Hits are the automatic run verdicts in `hit_verdicts`; a first-date hit closes the tile as `candidate` for review and no second date is taken. A second date is a different calendar day and scene. A tile with no usable scene in 60 days is closed `no_imagery` for the round.
- Sources: S1 first (3.7 to 6.1 pairs per live S1 scene against 1.0 to 1.65 per S2 scene, see feasibility), S2 only when the tile has no S1 scene in the window. `min_hulls_for_second_date` is `tbd`: it would close tiles with a near-empty first scene as `quiet`.
- A round is complete when every tile has a visit; the next round starts as soon as no tile is left and none is waiting for a result, and draws a new random order with new dates.
- The app reads the same state through `grid_status.py` (a light module with no catalogue or STAC imports, because the app image has no catalogue dependencies): `/api/hotspot-tests/grid` returns tile status and per-scan results with the review run number, and the hotspot-test page draws it as an SVG map (tile status is derived from the scene results directly, not from the planner's visit decisions, so a finished scene shows as scanned with or without pairs within the 30 s poll; `decision` carries the planner's closing outcome) from `hotspot-tests/grid_land.json` (Natural Earth 50 m land clipped to the grid area and simplified to 0.04 degrees, 14 KB). The page polls every 30 s while visible.
