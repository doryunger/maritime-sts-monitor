## match
- One archive query per scene: reports within `ais_match.search_window_min` of capture inside the hot-spot bbox padded by `search_radius_km`; the box comes from the run's stored `hot_spot_settings` (imported runs have none, so `hotspots.load()` by site name).
- Reports repeat across 30-min snapshots, so the query is `select distinct` on the report fields. Per ship the report just before and just after capture are kept; satellite rows get a fresh ship_id per serving, so a ship seen terrestrially and by satellite is two candidates. Satellite and unnamed candidates are recorded but never chosen.
- Position at capture: closest report if its speed is at most `stationary_max_speed_kn` (or null); interpolation when a report exists on both sides; otherwise dead reckoning along course (heading when course is null) from the nearest report. Report age is the age of the nearest report even when interpolating.
- AIS heading 511 (unavailable) is treated as null; for moving ships course stands in for it.
- Limits (`max_report_age_min`, `length_tolerance_m`, `heading_tolerance_deg`) are applied only when not null; the measures are stored either way in `ais_matches` so the limits can be set from them.
- Sentinel-2 hulls have two positions, so each hull is its own slot with its own distance and length. A Sentinel-1 pair has one outline: both slots sit at its centre and the distance limit is `max_distance_km` plus half the outline length, as the ships can lie anywhere along it. Sentinel-1 length is the pair rule's `length_m`, heading the own Ai2 box heading.
- One-to-one per scene is greedy: all passing (hull, slot, ship) edges sorted by distance, a ship and a slot used once. Not globally optimal, but ships that close together are rare in the data and greedy keeps the rule explainable.
- `identities` holds only resolved ships (489 at the time of writing), so imo, mmsi and flag are often null even for a chosen named ship.

## stats
- Chance baseline: per scene and source the same number of random points as hulls, drawn uniformly in the hot-spot box with seed `baseline_seed` plus the scene index, kept `baseline_clearance_km` from every hull slot and from land; the nearest named terrestrial candidate within the search radius is measured. Points with none within the radius are counted separately, not given a distance.
- `--stats` selects the hulls QA marked ok (not moving); with `--all-checked` every checked stopped hull, singles included, so its tables describe all ships, not only STS ones.
