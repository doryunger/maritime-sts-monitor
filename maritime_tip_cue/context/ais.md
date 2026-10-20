## match
- One archive query per scene: reports within `ais_match.search_window_min` of capture inside the hot-spot bbox padded by `search_radius_km`; the box comes from the run's stored `hot_spot_settings` (imported runs have none, so `hotspots.load()` by site name).
- Reports repeat across 30-min snapshots, so the query is `select distinct` on the report fields. Per ship the report just before and just after capture are kept; satellite rows get a fresh ship_id per serving, so a ship seen terrestrially and by satellite is two candidates. Satellite and unnamed candidates are recorded but never chosen.
- Position at capture: closest report if its speed is at most `stationary_max_speed_kn` (or null); interpolation when a report exists on both sides and both reports pass `max_report_age_min`; otherwise dead reckoning along course (heading when course is null) from the nearest report. Report age is the age of the nearest report even when interpolating; both bracket ages and their gap are retained.
- AIS heading 511 (unavailable) is treated as null; for moving ships course stands in for it.
- Limits (`max_report_age_min`, `length_tolerance_m`, `heading_tolerance_deg`) are applied only when not null; per-slot comparison measurements and candidate rejection reasons are stored in `ais_candidates` for QA. `ais_matches` contains only selected assignments for publishing and display.
- Sentinel-2 hulls have two positions, so each hull is its own slot with its own distance and length. A Sentinel-1 pair has one outline: both slots sit at its centre and the distance limit is `max_distance_km` plus half the outline length, as the ships can lie anywhere along it. Sentinel-1 length is the pair rule's `length_m`, heading the own Ai2 box heading.
- One-to-one per scene is a global assignment: maximize the number of eligible matched slots, then minimize total distance, then use report age as a tie-break. A single assignment pass covers every hull slot so one vessel's candidate set cannot consume a ship before another slot is considered.
- `identities` holds only resolved ships (489 at the time of writing), so imo, mmsi and flag are often null even for a chosen named ship.

The archive field `is_sanctioned` is copied from the nearest report into both SQLite AIS tables; null is false. It enriches the selected identity and does not affect assignment. Existing SQLite tables receive a non-null integer boolean column on open.

## stats
- Chance baseline: per scene and source the same number of random points as hulls, drawn uniformly in the hot-spot box with seed `baseline_seed` plus the scene index, kept `baseline_clearance_km` from every hull slot and from land; the nearest named terrestrial candidate within the search radius is measured. Points with none within the radius are counted separately, not given a distance.
- `--stats` selects the hulls QA marked ok (not moving); with `--all-checked` every checked stopped hull, singles included, so its tables describe all ships, not only STS ones.
- Coverage: each matched scene records in `ais_coverage` how many archive ships and reports fell in its search window
  and box. Below `ais_match.min_coverage_ships` the pair is "no archive data", never "no AIS": the archive has no
  reports over the Gulf of Oman before 2026-10-12, so run 1 (9 Oct) cannot be enriched. A matching run first clears
  the matches of hulls that are no longer pairs (after a re-check or QA), and only `pair` is matched.
- Two vessels per pair: vessel 1 is hull A (S2) or the longer light line (S1), vessel 2 the hull B blob, the stepped
  strip the S2 shape test found, or the other S1 light line. Each AIS slot is matched at its own vessel position, so
  a named ship lands on the vessel it sits on. Before this both S1 slots sat on the outline centre.

