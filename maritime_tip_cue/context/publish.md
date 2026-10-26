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
date, hot spot, source, scene, run and hull ids, QA status (`confirmed`, `corrected`, `unreviewed` or, after a sync, `rejected`), the source's
measurements as JSON, and vessel 1 and 2 (the same numbering as the app: S2 hull A and B or shape step, S1 longer and
shorter light line), each with position and AIS name, IMO, MMSI, flag, match distance and report age, null when
unmatched. `vessel_1_is_sanctioned` and `vessel_2_is_sanctioned` are always boolean; missing or null archive values, including unmatched slots, are false; `ais_covered` false means the archive had no ships at that time, so "no name" is not evidence.

Captures: one PNG per pair, the same crop as the QA card (`review.crop_half_m`) with the card's fixed stretch. When both
vessel positions project into the crop, the numbered markers are drawn on it as the review app shows them but 1.6 times
larger (`publish.MARKER_SCALE`, from 2026-10-26: the review-app size was too small to read on the published image): a dot
on the vessel and a numbered pill 26 px away from the other vessel, yellow when that vessel has an AIS match and white
when it has none; otherwise the plain crop is published. There is no separate unmarked copy and no GeoTIFF. The Parquet
row has one S3 path, `capture_png`. File names carry the finding id and the AIS names.

Pair heading (`heading_deg`, `heading_source`): one axis angle per pair for rotating the viewer icon, degrees clockwise
from north in [0, 180), so it carries no bow/stern direction. It replaces the bearing between the two markers, which
changes with where each marker is placed. The Ai2 box heading is never used: on Sentinel-1 it disagreed with our own
outline by more than 20 degrees on 23 of 39 pairs (pair w0_38514_14173: Ai2 0.6, outline 54). Sentinel-1 takes the
angle of the minimum-area rectangle of the object pixels, `rect_angle_deg` (`pixel_rect`), for every pair, a map angle
counter-clockwise from east in the scene's projected grid converted as 90 minus the angle. The two bright vessel lines
of a pair are built parallel to that axis, so both vessels share it and the axis mean equals it. The edge-fitted
`single_box.angle_deg` is not used: rendered over the radar crops (2026-10-25) it sat 31 and 11 degrees off a visibly
vertical blob on w0_45305_278721 and w0_44824_278642 where the rectangle followed it, and it exists for only 39 of 85
pairs, so using it would mix two methods. Sentinel-2 takes `check.fix_heading` (`fitted`) only when the pixel fit moved
it away from the Ai2 heading by more than 0.01 degrees; a fit that left it at the Ai2 value is treated as no fit. Hull B
reuses hull A's heading because no separate B heading is measured. Check on 47 local Sentinel-2 pairs: the line from
hull A to hull B is 76 to 90 degrees from the heading on 44 of them; hulls 45364_-278058 (20.5), 45544_-278613
(62.9) and 45054_-279186 (76.6) deviate. A pair with no heading of ours gets null. `rect_angle_deg` is stored in every
Sentinel-1 record from this version on; older records get it recomputed from their saved chips when backfilled.
Both vessels' angles are doubled before averaging, so 179 and 1 give 0.

## Redrawing published images
`python -m maritime_tip_cue.publish_redraw [--upload]` redraws the capture of every published pair row at the current marker
size and writes it over the key the row already holds (`capture_png`); the parquet files are never touched, so QA status
and identities stay as published. Without `--upload` it draws only; with it, each old image is first saved under
`data/publish/pair-captures-backup-<time>/`. It collects with `enrich=False`, so it does no identity refresh, sanctions
apply or Equasis request (all of which `collect_at` does by default and which dominate its time: about 4 s per run of
database round trips against under 0.02 s per image). Rows are matched to a run by scene, hot spot and hull among the runs
where that hull is currently a pair, because the `run` and `job_id` in older rows are the numbers at publish time (legacy runs
1-9 are now 34-42 and the job runs were renumbered), not the current run. Rejected rows and rows whose hull QA has since
turned into a single are skipped. First run, 2026-10-26: 137 of 138 images redrawn at marker scale 1.6 (the one skipped is
a pair QA turned into a single; the parquet still lists it as not rejected until `publish sync` runs). The same pass can
rebuild the sanctioned images: `publish_sanctioned <runs> --upload --no-enrich`.

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
`review_prefix` (the attempt's review folder on S3: chips and windows the QA card is drawn from; the run's state and events are
in Postgres under the attempt's run), `rules_sha256` (the pinned rules in the job manifest, also under the attempt's artifacts),
`code_commit` (the image's `STS_GIT_COMMIT` build argument, or the repository head locally) and `detector_revision`
(the Ai2 detector revision baked into the image; empty for local runs). The scene itself is not stored: rerun the
job's manifest against a fresh download of `scene_id`. Review data is a QA aid, never a gate: a missing review upload
is recorded in the receipt and does not stop publication.

## Syncing reviews
Sync applies reviews and nothing else: it adds, rejects, restores or re-labels a row only when the hull behind it has a
review flag. A published row nobody reviewed is left exactly as published, even when the current rules would now call
the hull a single, and an unreviewed pair is never added (new jobs still publish their unreviewed pairs at the end of
the job, as before: the automated process is the one exemption).

`python -m maritime_tip_cue.publish sync [--runs N ...] [--upload] [--dry-run]` brings the Parquet in line with the
reviews. A verdict used to change only Postgres: pair->single left the row, single->pair was never added (the worker
matched and captured only the hulls that were pairs at job end) and confirmed pairs stayed `unreviewed`.

Plan = desired state (`review.findings` verdict after QA and `review.qa_flags`) minus the rows read from S3 (all
Parquets under the findings prefix, matched by run, scene, hot spot and hull, never by job). Per file it prints
`added / rejected / restored / status_updated / unchanged`; a second sync prints `nothing to change`.
- Pair turned single or excluded: the row stays with `qa_status = 'rejected'` and `verdict` = `single` / `excluded`
  (any other non-pair verdict is written as is); every other column, the captures and the PNG paths stay. A later pair
  verdict returns it to `confirmed` / `corrected` / `unreviewed`. Confirmed (`ok`) and corrected (`wrong_verdict`,
  `is_pair`) use `QA_LABEL`. `qa_note` is not touched.
- Viewer contract (the viewer is in another repo): hide rows where `qa_status = 'rejected'`. New values: `qa_status`
  `rejected`; `verdict` `single`, `excluded` (and `pair`, as before) on rejected rows.
- A pair with no published row is added: AIS matching for only those hulls (`ais.match.select_hulls` +
  `run_match`, the code of `watch/jobs.py`, current rules), then the normal captures and row through `assemble`. Provenance
  columns are copied from another row of the same run, else only `attempt_id`. The captures need the run folder
  (`data/runs/<folder>`) on the machine that syncs. A pair that is a duplicate of a published, non-rejected finding
  (same rule as publishing) is counted as skipped before any AIS call or image is made; the merge applies the rule again.
- Edits go through `merge_group` with `job_id=None` and an `edits` map: rows are replaced by `finding_id` inside the
  If-Match / ETag retry loop, so other jobs' rows in a shared file are untouched. Images go up before the Parquet, for
  added findings only. A rejected row no longer counts as the twin of a new finding, so a rejected twin cannot hide a
  real pair seen from another tile.
- `--upload` is the only mode that changes S3 or records anything: it writes `review.outputs` / `output_scenes` /
  `captures` (with `scene_id`, `site`, `hull`) for the added findings; `n_rows` of an output is the run's rows in that
  file after the sync. Without it, S3 is only read: added rows and PNGs are written under `data/publish`, edits are
  reported but not applied, and a repeat shows the same plan. `--dry-run` prints the plan and writes nothing.
- Runs: default is every run with an output row or a pair after QA; a run the gate rejects (not done, or a job not in
  the ledger's completed set; legacy runs are exempt from the job requirement) is skipped with a line, while an explicit
  `--runs` list refuses everything, as publish does.
- Request flow from the app: the Runs page button inserts a `pending` row in `review.sync_runs`; `sts-sync.timer` (every
  30 s) runs `python -m maritime_tip_cue.publish_service run-pending` in a short-lived container. It first marks any row
  `running` longer than `publish.sync_abandon_hours` (rules, 3 h) failed with error `abandoned`, then claims the oldest
  pending row with `for update skip locked` (so overlapping runs cannot take the same one), runs `sync_report` with upload on
  and the row's `sync_id`, and stores `done` with the summary or `failed` with the error text. The summary is
  per file (`files`: added, rejected, restored, status_updated, unchanged) plus `totals`, `duplicates`, `runs_checked`,
  `rows_checked`, `runs_skipped`, `unattributed` and the printed `lines`. `outputs` and `captures` rows recorded by that
  sync carry its `sync_id`; the CLI leaves it null. A failed sync leaves S3 as the per-file merge left it (each file is an
  ETag-guarded replace) and a new request resumes. A service run killed mid-sync leaves its row `running` until abandoned,
  and the button stays disabled until then.
- Two edits in different files that depend on each other are seen on the next sync (rows in other files are not edited
  in the same pass).

## Sanctioned singles
Singles are the pair check's `single` hulls (rules `sanctioned.verdicts`; `moving` is already read as `single`). They go
through the same AIS match as pairs, then identity and the sanctions list, and a single whose matched vessel is
sanctioned is published to `sanctioned/<DD-MM-YYYY>/<hot_spot>/<source>.parquet` with its capture under the same
`captures/` tree. Nothing is built from AIS alone: no detection, no row. Satellite and unnamed AIS are never matched
(`ais.md`), so a row always names a terrestrial AIS vessel.

Order in a job (`watch/jobs.execute`): pairs are matched first, then singles (`match.match_singles`: `exclude_taken`
removes every ship already chosen by another hull of the scene, and the scored rule `sanctioned.single_match` applies,
see `ais.md`). Without `exclude_taken` the singles pass would hand a pair's ship to a single standing next to it and the
pair would lose its name on a re-match. `vessel_match_score` in the row is the score of the match. At publish (`publish_run`) one `collect_at` call
takes pairs and singles together, so identities are refreshed, Equasis is asked and the list applied once. A single is
kept only if a matched party is sanctioned (archive flag `is_sanctioned`, the vessel's identity IMO or the archive's
`sanctions_imo` found in our list); an unconfirmed name match is not published. The group `sanctioned` is read from
the running image's rules, not the job snapshot: queued jobs carry snapshots without it.

Equasis is asked about a single only when it is a suspect: the archive flagged it, or its AIS name equals a listed
name or alias (letters and digits, `name_min_chars`). Pairs are looked up first, as before. Measured on the 237 ships
the archive flagged between 10 and 26 Oct 2026 (all in our list, 237 of 237): 224 carry a listed name, 210 carry
exactly the list name or an alias; ships are often reflagged, so flag is not compared. The archive also gives
`sanctions_imo` for the ships it has resolved, which fills the match IMO without a lookup. In the four hot spots over
the same days 19 (malaysia_eopl), 45 (johor_east_scs), 23 (fujairah_outer) and 21 (off_sohar) flagged ships were inside
the boxes, plus 54, 25, 23 and 20 unflagged ships whose names are listed: many are generic names (ARTEMIS, ORION),
which is why a name alone does not publish.

Rows are the pair rows with one vessel: `vessel_*` columns without the 1/2 number, `vessel_sanctions_lists`,
`vessel_sanctions_programs` (from the list's `programs`), `vessel_imo`, `verdict` (`single`), capture PNG path.
`vessel_is_sanctioned` is always true. Heading: `heading_deg` and `heading_source` are the pair rule applied to the single's own hull record (an axis in [0, 180), no bow or stern; S1 `pixel_rect`, S2 `fitted`, null when the S2 fit left the Ai2 heading unchanged), and `vessel_ais_heading_deg` (0-360, the AIS heading, or course when the ship moves and has no heading; null when AIS gave neither) and `vessel_ais_speed_kn` come from the selected `ais_candidates` row. The two are kept apart because our axis has no direction and the Sentinel-1 box heading is unreliable (see the pair heading note above). The PNG is the plain crop with no marker: only pairs get markers (decision, user 2026-10-26). Duplicates, `also_in` and the
per-job replace are the pair merge, with `publish_merge.keep_identity` reading the unnumbered columns. A finding that QA
later turns into a pair is not removed from `sanctioned/` and `publish sync` does not touch these files.

The sanctioned publish runs after the pair publish and cannot fail the attempt: the worker's `config.local.yaml` is its
only config and its IAM policy is separate, so an old copy of either would otherwise fail every job whose pairs were
already published. The error goes to the receipt (`published.sanctioned.error`) and the same runs can be published later
with the CLI below, since their state stays in Postgres. `published.sanctioned.listed_imos` is the size of the list the
worker read; the list is in the ignored `data/sanctions/`, so the worker only has it if the host's mounted `data/`
does, and 0 there means every list-based decision silently found nothing.

`python -m maritime_tip_cue.publish_sanctioned <runs> [--match] [--upload]` builds the same files for existing runs;
`--match` first runs the singles AIS pass on them. Without `--upload` it writes to `data/publish/sanctioned/`.
