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

## detector
Sentinel-1 needs the frame and one earlier frame of the same orbit direction 30-90 days before; among the
candidates the one covering most of the scene wins, ties go to the age closest to 60 days. Both are downloaded
as VV and VH GeoTIFFs (about 4 GB per frame) and deleted with the scratch after each scene.

## s2 windows
The pair check reads the detector's own pixels, not a second download: 13 bands of the detector window are read
for a 2 km box around each cluster of hulls (hulls of one scene within 500 m share a box, which keeps every
hull 500 m from the edge), the 20 m and 60 m sets are repeated up to 10 m. The L2A scene classification of the
same tile and day is cut to the same grid. All of it is stored per scene (`s2_windows/*.npz`) before the scratch
goes, so verdicts can be recomputed without the detector (`--reanalyse`). The hull passed to the pair check is the
corrected Ai2 box unrounded; the check was frozen on unrounded values. A box inside the A or B rectangle of a
pair or possible_pair record is absorbed (role `duplicate` or `hull_b_box`) and not counted again.

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
or with `--reanalyse`. Scratch of a scene
(several GB) is deleted as soon as the windows or chips are written. Tiles of one Sentinel-2 datatake overlap, so
a hull in the overlap can appear in two scenes; no cross-scene merge is done yet.

## state
`state.db` (default `<out>/state.db`, `--db` overrides) holds the run state; the schema is in `state.py`. Pair-check inputs
derived from the detector (`dets`, `index`, `objects`, `windows`) sit as one JSON document in `scenes.data`, because they
are read back whole and never queried; the raw detector features are rows in `raw_detections`. `hulls.record` keeps the
full check record as JSON next to the queryable columns. The scene status and its results are written in one
transaction, so a run killed mid-scene leaves the scene unmarked and it is redone. Files from before the database
are loaded once with `python -m maritime_tip_cue.watch.state import <run folder>` (old files are left in place).

## report
`report.py` is the data layer of the runs app (`app/`): `collect` builds a run's document (run, scenes, listed hulls,
AIS parties, fixture comparison) from `state.db` and the stored windows and chips, so it works on a finished run
without the detector, and `render` draws one hull's raw or overlay tile on request. The listed singles are the
already-flagged singles first, then a seeded random sample up to 24, so a review never drops out of the list when a
reanalysis changes the singles. The shore reason in the fixture comparison uses the run's stored hot-spot settings.
Presentation constants live in `report.py`: crop and upscale are `rules/sts_graph.json` `review`
(300 m half side) with the scale raised to 6 (360 px tiles); Sentinel-2 true colour maps L1C digital number 0-2550
to 0-255 (`review.s2_stretch_max`); Sentinel-1 VH is shown as 10 log10 of the stored power between 22 and 50 dB
(uncalibrated units, sea sits at 22-26 dB, ships reach 45-55 dB), the same range for every chip so tiles compare.
Raw and overlay come from the same crop code, so they have the same pixel size. The Sentinel-1 overlay rebuilds the
object outline from the chip (white rectangle = footprint, red = the two parallel arms of the pair rule) next to the
Ai2 boxes (yellow own, cyan others); Sentinel-2 draws hull A and B (red) and the Ai2 box (cyan). The singles sample
is a seeded random draw over sorted hull ids, so the same seed gives the same list.

Fixture comparison matches a fixture object to the nearest run hull of the same date within 60 m (Sentinel-1 by the
pass date in the fixture name, Sentinel-2 only for fixture sets named `<region>_<date>`; the undated sets `known`,
`s1`, `s2`, `s3` have no capture date and are not compared). A fixture object without a hull is explained from the
raw detections: none within 60 m (`no_detection`); a raw detection that no kept detection lies within 40 m of was
dropped, outside the hot spot box or by shore distance (`outside_hot_spot`, `shore_distance` with the measured shore
distance); a kept detection without a hull was not checked (`not_checked`, an object under the minimum length). The
shore distance threshold of the run is not stored, so the reason is derived from the box and the measured distance,
not from the threshold. The fixture verdict is the one the fixture rules give, which ignore speed, so a stopped-ship
fixture that the run calls `moving` counts as a difference.

## qa
QA is done in the runs app (`app/context/app.md`): one card per listed hull with a flag (OK, wrong verdict with the
correct one, bad geometry, other; unmeasured hulls take single, single moving or pair directly) and a note. A Save
writes `state.db` `qa_flags` (one row per hull, upserted) and rewrites the committed `reviews/<run>.json`, since
`state.db` is gitignored; `state.py qa-import <run> <reviews/<run>.json>` reloads it into a fresh database.
QA changes the findings through the `findings` view: its `verdict` is the reviewer's correct verdict for
`wrong_verdict` (`unresolved` while none is picked), the labelled one for unmeasured hulls, `excluded` for
`bad_geometry` and `other` (rigs, barges, broken outlines), else the run verdict, kept as `run_verdict`. AIS
matching and publishing read `findings`; the cards keep the run verdict, since that is what is judged, and show
"after QA" where it changed. The view is created only if missing, so opening a run that is still being written
needs no write lock.
First use (fujairah_outer 12-19 Oct 2026, 64 hulls): 11 of 18 pairs OK, 4 wrong, 2 rigs or barges, 1 small
craft alongside; all 10 unmeasured were single ships.

## scratch path
On the Windows dev PC long paths are off (no admin to enable them), and the detector's tile store nests the
scene id twice under `work/scratch/tiles/sentinel1/`; with a run folder name like `johor_east_scs_week` the path
passed 260 characters and Sentinel-1 ingest failed with WinError 3. `config.yaml` `ai2.scratch_root` puts the
per-scene work folder under a short root keyed by a hash of the scene id; left empty (Linux) it stays in the
scene folder.


## registry
Runs are numbered, not named: `data/runs/registry.db` maps run number to folder and creation time, and a new run
gets the next number and the folder `data/runs/<number>`. `watch.run --run <n>` resumes one; `--out` still
registers an existing folder (runs launched before numbering). `registry adopt` renames a registered folder to its
number once its last invocation has ended (an unfinished one is left alone, since a queued resume names it).
