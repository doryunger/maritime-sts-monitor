## positions.py

Field decoding of the vessel rows, inferred from the data, not documented:
- `SPEED` is knots × 10 (a VLCC at "128" = 12.8 kn). `ELAPSED` is minutes since the last position, so
  `seen_utc` = fetch time − ELAPSED; pass `--fetched` when processing an old saved file.
- `SHIPTYPE` is the source's coarse colour class (7 cargo, 8 tanker, …); `GT_SHIPTYPE` is a finer
  code with no known mapping, kept raw.
- `[SAT-AIS]` rows are anonymised satellite fixes: `SHIP_ID` is an opaque token, no flag/size, but they
  carry `TYPE_NAME`/`STATUS_NAME`. They often sit a few km from a named terrestrial row of the same
  ship (same course/speed), so they are likely duplicates; `--no-sat` drops them.
- `state` swing rule: an anchored ship weathervanes with wind/current, so at near-zero speed its
  heading drifts far from the (noisy) course-over-ground; in the sample, ships stopped at an
  anchorage showed 61–165° course/heading gaps. Under way the two stay within a few degrees.
  Heading is null on many rows, in which case the rule can't fire and speed alone decides.
