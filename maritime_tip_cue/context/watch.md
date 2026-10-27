# Hot-spot watch (detector, windows, per-scene run)

## ai2_runner
Runs inside the ai2-detectors venv (`config.yaml` `ai2.root`), never imported by the app. It wraps the rslp
predict pipeline for two reasons: it records the detector windows (name, CRS, pixel bounds) to `windows.json`,
because the scratch layout alone does not say which window a layer belongs to, and it narrows the detector's
scene window to the hot-spot box, so a 22 km site costs a fraction of a full frame. A Sentinel-2 tile cropped to
a 22 km box took about as long as the full tile (measuring many ships dominates), a radar frame went from 21 to
8 min. Cropped and full runs agreed on 97-99 % of hulls; every difference lay 8-25 km inside the window (the
model tiles the scene differently), so the crop is not an edge cut. The Windows worker settings in
`config.yaml` `ai2.env` switch the data-loader workers off, which the Windows build of the pipelines needs.
The runner reports CUDA visibility, PyTorch memory peaks, and NVIDIA utilization and memory every two seconds;
these distinguish framework initialization from sustained GPU work. Shadow's 11 Oct 2026 S2 run logged
`GPU available: True, used: True`, 10-38% utilization, about 900 MiB GPU memory, and 175 seconds total. The
container uses the same worker settings but a pinned PyTorch 2.7.0 runtime, compared with Shadow's 2.9.1.
Detector project management reads checkpoints from `/opt/ai2-models` and writes run metadata under the same
writable image path. The attempt ledger preserves
diagnostics after scratch cleanup. The forkserver re-imports the runner in child processes,
so the run-spec path comes from inherited environment and only the original `MainProcess` rewrites `sys.argv`
or starts the pipeline; otherwise child processes can recursively treat a pipeline name as a file path.

## detector
Sentinel-1 needs the frame and one earlier frame of the same orbit direction 30-90 days before; among the
candidates the one covering most of the scene wins, ties go to the age closest to 60 days. Both are downloaded
as VV and VH GeoTIFFs (about 4 GB per frame) and deleted with the scratch after each scene.

The worker container installs detector packages into its active Python environment and includes the pinned
`rslearn_projects` tree at `ai2.root`. A host detector virtualenv may not exist in the container, so the runner
uses the worker process interpreter when the configured venv executable is absent. The detector changes its
working directory to `ai2.root/rslearn_projects`; paths for the spec and scene work must be absolute to keep all
scratch and outputs under the persistent `/app/data` mount.

## s2 windows
The pair check reads the detector's own pixels, not a second download: 13 bands of the detector window are read
for a 2 km box around each cluster of hulls (hulls of one scene within 500 m share a box, which keeps every
hull 500 m from the edge), the 20 m and 60 m sets are repeated up to 10 m. The L2A scene classification of the
same tile and day is cut to the same grid. All of it is stored per scene (`s2_windows/*.npz`) before the scratch
goes, so verdicts can be recomputed without the detector (`--reanalyse`). The hull passed to the pair check is the
corrected Ai2 box unrounded; the check was frozen on unrounded values. A box inside the A or B rectangle of a
pair or possible_pair record is absorbed (role `duplicate` or `hull_b_box`) and not counted again.
A scene processed on its capture day has no L2A item on the STAC yet: its windows get no `scl`, the index entry says
`cloud_mask: false` and the log says so (before 2026-10-27 this was silent). Only the cloud handling of the water ring
around hull A is skipped; the cloud-as-hull-B pairs have another cause (see feasibility), so the L2A is not waited for.

## s1 objects
Ai2 radar boxes are often misplaced by tens of metres and one tanker may carry two boxes, so boxes are grouped
by the bright footprint they share (smoothed VV dB above the local sea median by 12 robust sigma, opened with a
3 px square). Objects shorter than 150 m are not checked. A 104 x 104 px VH/VV chip of each object is stored
(`chips/*.npz`) with the Ai2 boxes around it. The radar blur used in the width test is estimated from the
narrowest third of all objects of the pass, so objects of every frame of a datatake are evaluated together; the
frames of one datatake are one pass. An object without a usable outline has verdict `unmeasured` here (the radar
rule itself only returns pair or single). Radar objects are not matched against Sentinel-2 hulls.

## run
A scene is finished when its `scenes` row has status `detected` (detector done, windows and chips extracted) or
`checked`; a rerun skips detection and keeps the stored detections, so changing a hot spot's shore distance later
does not re-filter old scenes. A failed check leaves status `detected` with the error text; a failed detection sets
`failed` and is retried (attempts counted). Radar verdicts are recomputed only for passes with an unchecked scene
or with `--reanalyse`. After each scene's detection and S2 check, its hashed temporary workspace is removed and
the removal is verified before the loop can start the next scene. This also runs after failures; if removal fails,
the job stops instead of accumulating more scene data. Sentinel-2 band windows, Sentinel-1 chips, state and
artifacts remain under the run folder for review and resume. Tiles of one Sentinel-2 datatake overlap, so a hull
in the overlap can appear in two scenes; no cross-scene merge is done yet.

## state
Run state lives in Postgres (`review` schema, see [store.md](store.md)); `state.Store` is bound to one run
(`Store.create`, `Store.open(run_no)`, `Store.for_attempt`) and keeps the old method names. Pair-check inputs derived
from the detector (`dets`, `index`, `objects`, `windows`) sit as one jsonb document in `scenes.data`, because they are
read back whole and never queried; the raw detector features are rows in `raw_detections`. `hulls.record` keeps the
full check record as jsonb next to the queryable columns. The scene status and its results are written in one
transaction, so a run killed mid-scene leaves the scene unmarked and it is redone. Rows go in as one batch per scene
(`executemany` in pipeline mode), because the worker on EC2 writes over the network. NaN and infinity become null
before storing.

`save_checked` upserts the hulls of a scene instead of deleting and re-inserting them: a re-check (`recheck`, a rules
change) must not cascade away the review flags that reference a hull. A hull that a re-check no longer produces is
deleted, unless it carries a flag; then it stays with `role = 'duplicate'`, which keeps it out of the `findings` view
and the app while the flag survives.

A run resumes by number: `watch.run --run N`. The CLI is `python -m maritime_tip_cue.watch.state summary <run_no>` and
`qa-import <run_no> <flags.json>` (flags matching no hull are skipped and counted). The old `import <folder>` of
pre-database files and the `--db`/`--out` options of `watch.run` are gone; those files are history.

## report
`report.py` is the data layer of the runs app (`app/`): `collect` builds a run's document (run, scenes, listed hulls,
AIS parties, fixture comparison) from Postgres and the stored windows and chips, so it works on a finished run
without the detector, and `render` draws one hull's raw or overlay tile on request. The listed singles are the
already-flagged singles first, then the unflagged singles in a fixed per-hull order (sha1 of seed and hull id) up to
24. A fixed rank keeps the list stable while reviewing: flagging a listed single only moves it into the kept set. The
earlier `random.sample` redrew the whole remainder on every flag (2026-10-25: 6-17 of 24 singles changed on finished
host runs), so cards vanished on reload and the overview, which recomputes with the latest flags, disagreed with an
open page. The shore reason in the fixture comparison uses the run's stored hot-spot settings.
Presentation constants live in `report.py`: crop and upscale are `rules/sts_graph.json` `review`
(300 m half side) with the scale raised to 6 (360 px tiles); Sentinel-2 true colour maps L1C digital number 0-2550
to 0-255 (`review.s2_stretch_max`); Sentinel-1 VH is shown as 10 log10 of the stored power between 22 and 50 dB
(uncalibrated units, sea sits at 22-26 dB, ships reach 45-55 dB), the same range for every chip so tiles compare.
Raw and overlay come from the same crop code, so they have the same pixel size. The Sentinel-1 overlay rebuilds the
object outline from the chip (white rectangle = footprint, red = the two parallel arms of the pair rule) next to the
Ai2 boxes (yellow own, cyan others); Sentinel-2 draws hull A and B (red) and the Ai2 box (cyan). Image URLs carry the
run's cache stamp (`image_version`) so a recheck is not hidden behind the browser's one-hour image cache.
When the S1 single-box test supplies a single-hull explanation, its green fitted box replaces the forced red
arms. The card includes body width, width ratio, edge residual and measured side support; its full box length
uses the Ai2 reference. The extraction rationale is in [s1shape](sts.md#s1shape).
Each hull carries `confidence` and `confidence_threshold`: the S1 score is stored by the pair check (null when it has
no outline or the record predates the score), the S2 score is computed at display time from the stored check.

Fixture comparison matches a fixture object to the nearest run hull of the same date within 60 m (Sentinel-1 by the
pass date in the fixture name, Sentinel-2 only for fixture sets named `<region>_<date>`; the undated sets `known`,
`s1`, `s2`, `s3` have no capture date and are not compared). A fixture object without a hull is explained from the
raw detections: none within 60 m (`no_detection`); a raw detection that no kept detection lies within 40 m of was
dropped, outside the hot spot box or by shore distance (`outside_hot_spot`, `shore_distance` with the measured shore
distance); a kept detection without a hull was not checked (`not_checked`, an object under the minimum length). The
shore distance threshold of the run is not stored, so the reason is derived from the box and the measured distance,
not from the threshold. The fixture verdict is the one the fixture rules give; a pair fixture that the run calls
`single` counts as a difference.

## qa
QA is done in the runs app (`app/context/app.md`): one card per listed hull with a flag (OK, wrong verdict with the
correct one, bad geometry, other; unmeasured hulls take single or pair directly) and a note. A Save upserts
`review.qa_flags` (key: run and `scene__hull[__site]` id; `site` and `hull` hold the real hull key) in one transaction
and returns the row read back after commit. Flags are durable in Postgres; the `reviews/<run>.json` mirror is no longer
written (the tracked files stay as history), and `state.py qa-import <run_no> <reviews/<run>.json>` still loads one.
QA changes the findings through the `review.findings` view: its `verdict` is the reviewer's correct verdict for
`wrong_verdict` (`unresolved` while none is picked), the labelled one for unmeasured hulls, `excluded` for
`bad_geometry` and `other` (rigs, barges, broken outlines), else the run verdict, kept as `run_verdict`.
Legacy `moving` verdicts and QA labels are read as `single`; the original speed measurement stays in the hull record.
AIS matching and publishing read `findings`; the cards keep the run verdict, since that is what is judged, and show
"after QA" where it changed.
First use (fujairah_outer 12-19 Oct 2026, 64 hulls): 11 of 18 pairs OK, 4 wrong, 2 rigs or barges, 1 small
craft alongside; all 10 unmeasured were single ships.

## scratch path
On the Windows dev PC long paths are off (no admin to enable them), and the detector's tile store nests the
scene id twice under `work/scratch/tiles/sentinel1/`; with a run folder name like `johor_east_scs_week` the path
passed 260 characters and Sentinel-1 ingest failed with WinError 3. `config.yaml` `ai2.scratch_root` puts the
per-scene work folder under a short root keyed by a hash of the scene id; left empty (Linux) it stays in the
scene folder. The per-scene cleanup barrier removes that full workspace before advancing and reports free space
from the workspace filesystem.


## registry
Runs are numbered, not named: a run is a row of `review.runs` (`run_no` from `review.run_no_seq`, `folder`, `origin`,
`attempt_id`), and `registry.py` is its thin Postgres wrapper (`new_run`, `get`, `by_attempt`, `listing`, `folder_of`;
`registry list`). `folder` is the directory under `data/runs` (the machine's runs path, `config.yaml` `runs.path`)
that holds scene windows, chips and the image `cache/`: `<job_id[:12]>-<attempt_id[:8]>` for a queue job,
`legacy-run-<n>` for a run made on the PC (`watch.run`, experiments, and the PC runs imported by `pgimport`). Run
numbers are global: one sequence, so the PC and the host agree. `watch.run --run <n>` resumes one.

## EC2 lifecycle process

The one-scene runner's EC2 readiness and persistent SSH sequence is documented in
[`context/catalogue.md`](catalogue.md#ec2-execution). It waits for the job manager to record the durable result
after the worker command exits.

`watch.run.main()` returns zero even when per-scene failures set the invocation status to `failed`.
A future worker must inspect scene and invocation records before acknowledging jobs or declaring the batch
complete; subprocess exit status alone would lose retries. Stop only after results and job state are durable.
