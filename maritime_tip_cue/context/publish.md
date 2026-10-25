# Publish

## publish
`python -m maritime_tip_cue.publish <runs> [--upload]` writes the product from the runs' `findings` (verdict after QA,
`pair` only) to `data/publish/` and, with `--upload`, to S3; without it nothing leaves the PC. Each contributing run
records its parquet files in `outputs` and its capture files in `captures`.

Only ledgered runs are published: every run must be `done`, carry the `job_id` of a queue job, and that job must be in
Redis `completed` (mirrored from the SQLite ledger). One ineligible run stops the whole publish before anything is
written. Runs made before the ledger (1-9) need `--legacy`, which makes publishing them a deliberate choice.

Duplicates: one vessel at one time is one finding, wherever it was seen. Two findings of the same source are merged
when their captures are within `publish.dedupe_time_s` and their centres within `publish.dedupe_length_share` of the
longer hull; this catches S2 tiles that overlap and hot spots that overlap (fujairah_outer and off_sohar at
24.8-24.9 N). The finding is kept in the hot spot whose box holds it with the larger margin, the others go to
`also_in`. A vessel seen again on a later pass is a new finding.

Rows: point geometry (WKB, GeoParquet 1.0 `geo` metadata, OGC:CRS84 by default) at the pair centre, capture time and
date, hot spot, source, scene, run and hull ids, QA status (`confirmed`, `corrected` or `unreviewed`), the source's
measurements as JSON, and vessel 1 and 2 (the same numbering as the app: S2 hull A and B or shape step, S1 longer and
shorter light line), each with position and AIS name, IMO, MMSI, flag, match distance and report age, null when
unmatched. `vessel_1_is_sanctioned` and `vessel_2_is_sanctioned` are always boolean; missing or null archive values, including unmatched slots, are false; `ais_covered` false means the archive had no ships at that time, so "no name" is not evidence.

Captures: one PNG per pair, the same crop as the QA card (`review.crop_half_m`) with the card's fixed stretch. When both
vessel positions project into the crop, the numbered markers are drawn on it exactly as the review app shows them: a small
dot on the vessel and a numbered pill 16 px away from the other vessel, yellow when that vessel has an AIS match and white
when it has none; otherwise the plain crop is published. There is no separate unmarked copy and no GeoTIFF. The Parquet
row has one S3 path, `capture_png`. File names carry the finding id and the AIS names.

## Publishing from the worker

Every queue job now publishes its own pairs at the end of the run, after its review data is on S3
(`worker.publish_pairs` -> `publish.publish_run`): `sts/<DD-MM-YYYY>/<hot spot>/<source>.parquet` plus the plain and
numbered-marker PNGs under `captures/`. Images are PNG only (the GeoTIFF is gone), data is Parquet. Rows are
`unreviewed` until QA. A job with no pairs publishes nothing. A failed publish fails the attempt, so a succeeded job
always has its pairs on S3; the retry resumes from the run's state without detecting again.

Several jobs share one file per date, hot spot and source, so a publish merges instead of overwriting:
`publish_merge.merge_group` reads the existing Parquet, drops the rows of the same `job_id` (a rerun replaces its own
rows), upserts by `finding_id`, and writes back with `If-Match` on the ETag, retrying on a concurrent writer. A new
finding that is a duplicate (same source, within `dedupe_time_s` and `dedupe_length_share` of the longer hull) of an
already published one in any hot spot of that date is not added; the first published wins and gets the new one in
`also_in`. Images are uploaded only for kept findings, before the Parquet that points to them. A rerun that no
longer has pairs in a file it published to before leaves those old rows in place.

`python -m maritime_tip_cue.backfill <runs> [--upload]` does the same for queue runs finished before this existed:
review data under the receipt's attempt id, then `publish_run`, gated on the ledger's completed set. Runs 10-15 were
backfilled on 2026-10-22; runs 1-9 predate the ledger and keep their earlier publication.

Reproducing a finding: every published row carries `job_id`, `attempt_id`, `run`, `scene_id`, `hull_id`, `finding_id`,
`review_prefix` (the attempt's review folder on S3: chips and windows the QA card is drawn from, the run database and
`events.jsonl`), `rules_sha256` (the pinned rules in the job manifest, also under the attempt's artifacts),
`code_commit` (the image's `STS_GIT_COMMIT` build argument, or the repository head locally) and `detector_revision`
(the Ai2 detector revision baked into the image; empty for local runs). The scene itself is not stored: rerun the
job's manifest against a fresh download of `scene_id`. Review data is a QA aid, never a gate: a missing review upload
is recorded in the receipt and does not stop publication.

