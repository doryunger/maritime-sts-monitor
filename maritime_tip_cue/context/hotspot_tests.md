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
