# Publish

## publish
`python -m maritime_tip_cue.publish <runs> [--upload]` writes the product from the runs' `findings` (verdict after QA,
`pair` only) to `data/publish/` and, with `--upload`, to S3; without it nothing leaves the PC. Each contributing run
records its parquet files in `outputs` and its capture files in `captures`.

Duplicates: one vessel at one time is one finding, wherever it was seen. Two findings of the same source are merged
when their captures are within `publish.dedupe_time_s` and their centres within `publish.dedupe_length_share` of the
longer hull; this catches S2 tiles that overlap and hot spots that overlap (fujairah_outer and off_sohar at
24.8-24.9 N). The finding is kept in the hot spot whose box holds it with the larger margin, the others go to
`also_in`. A vessel seen again on a later pass is a new finding.

Rows: point geometry (WKB, GeoParquet 1.0 `geo` metadata, OGC:CRS84 by default) at the pair centre, capture time and
date, hot spot, source, scene, run and hull ids, QA status (`confirmed`, `corrected` or `unreviewed`), the source's
measurements as JSON, and vessel 1 and 2 (the same numbering as the app: S2 hull A and B or shape step, S1 longer and
shorter light line), each with position and AIS name, IMO, MMSI, flag, match distance and report age, null when
unmatched; `ais_covered` false means the archive had no ships at that time, so "no name" is not evidence.

Captures: the same crop as the QA card (`review.crop_half_m`) as a GeoTIFF of the stored pixel values (S2 L1C bands
as uint16 digital numbers, S1 VV and VH linear power as float32) and a PNG with the card's fixed stretch and nothing
drawn. File names carry the finding id and the AIS names.
