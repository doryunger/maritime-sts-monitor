# STS pair check

Decides whether a detected tanker is one ship or two ships side by side (a possible ship-to-ship transfer),
from Sentinel-2 (spectral) and Sentinel-1 (radar). All parameters are in `rules/sts_graph.json`; numeric floors
that only guard a division stay in code. Fixtures in `data/sts_fixtures` hold labelled hulls and passes
with their expected verdicts, and `tests/test_sts_fixtures.py` reproduces them to 1e-6.

## ai2point
Ai2 builds a detection from `int(centroid)` in its UTM pixel grid, so the geojson point is the pixel corner and
the true centre is half a pixel east and north. The shift is applied once, where Ai2 points are read. The fixtures
hold hulls that are already corrected.

## s2window
Water is not assumed from a map: the window's own NDWI histogram gives the water peak, the water set is clipped
(spectral angle, norm, excess) so hull pixels hiding in it do not move the reference, and every threshold is
relative to that window's water (angle, excess z, robust sigma). A hull pixel is one whose angle from the water
spectrum, or whose mean red-edge-to-SWIR excess, passes the water's own 99th percentile times a margin. Near
a hull the reference is re-estimated on an annulus of local water, because glint and haze differ across a window;
cloud pixels (SCL) are dropped from it and too few clear pixels makes the hull unmeasured instead of guessed.
B11 is 20 m native, so its width is corrected by a blur in quadrature and the wider of B08 and corrected B11 is used.

## s2pair
Hull A is the Ai2 box refined from the pixels it touches: the starting ridge is the brightest across-position
near the box, the heading and length are fitted to the blob, and a very elongated ridge fixes the heading. The
fitted length gives the expected beam only when the heading moved a lot (the Ai2 box may lie across the ship).
A pair is called from three signals: the half-peak width of hull A in beams (`pair_width_ratio`), a second hull B
blob in a strip beside hull A (size, elongation, axis and plateau tests, with a halo test that rejects a dim
skirt of A), and one-sided excess along most of the hull in the 10 m bands. Weaker evidence gives `possible_pair`.
Unmeasured cases (cloud, hull running out of the window, profile never reaching water) are never turned into a
single. Estimated speed is retained with the detection but does not veto the pair check or create a separate verdict;
a window with no water peak is `unmeasured`. The stopped-speed veto remains executable only when loading the old
frozen rules snapshot, so historical fixture results can still be reproduced.
Hull shape (`shape_step`, `pair_split.shape_*`): both edges of hull A are traced per 5 m row where the brightest
10 m band falls to half its peak above the sea, and each edge is fitted as a straight line with at most one
one-sided step. Every limit is a ratio or a multiple of that hull's own edge noise (sea noise over edge steepness,
floored at the pixel quantisation), never metres: step size in beams, step significance (how much better the step
explains the edge than a line, in noise units), continuity (share of stepped rows clearly outside the base edge)
and raggedness of the stepped edge. A second vessel widens one side over a continuous stretch with a straight edge;
bow and stern taper is symmetric and halo stays under 0.3 beams; a cloud or haze beside the hull gives a step wider
than a hull with a ragged edge. The shape test turns weak evidence into `pair` and vetoes a `pair` whose hull B is a
ragged over-wide step. Weak evidence left over is reported as `single` (`weak_pair_verdict`), with `weak_evidence`
kept in the record: Sentinel-2 has no `possible_pair`. Tried and dropped (2026-10-19, 192 labelled hulls): edge
parallelism and symmetry tests (weak: a second vessel is often shorter or slightly angled), and the pixel-value
histogram of the whole object (no separation: one ship at 10 m is already multimodal, accommodation, deck and
hull). The histogram of the stepped strip against hull A in the same rows does separate (a second vessel is nearly
as bright and has the same value distribution, a halo is dimmer), but is not needed by the current limits.

## s1object
Radar outline of one object: dB chips of VH and VV, the object's brightness scale is a high percentile of the
pixels near the Ai2 point, and the outline is the connected set of pixels within a fixed drop below it. Side-lobe
streaks are one to two cells wide whatever the ship, so an opening of fixed width removes them without cutting
hull ends (a relative opening did). The minimum-area rectangle gives length and width. The brightest quarter of
the object's own pixels is split into two light lines by a two-means across the hull; fixed and random starts,
fixed seed, for repeatability.

## s1pair
Ratios only, no absolute widths or gaps: length of the measured outline in the tanker range, outline width minus
the pass's own radar blur in beams of its own length, separation of the two light lines over outline width, and
overlap of the two lines along the hull. The blur is the median excess width of the narrowest third of the
objects of the pass, so it is computed over all objects of the pass together. VH is used. All four must hold
for `pair`; anything else is `single`.
The outline is rebuilt from the radar pixels around the detection (grown from the local peak, thin rays opened
away); the Ai2 box only seeds it. Fill is the share of the outline's own fitted rectangle that it covers: a vessel or
a pair alongside is a filled box, while radar streak crosses from a bright reflector and smudge leave a sparse,
cross-shaped or ragged outline (fill 0.51-0.59 against 0.59-0.84 for pairs). A clearly boxy outline (fill at least
`fill_min_boxy`) is allowed a lower light-line overlap, since two hulls of different length overlap less. Tried and
not used (2026-10-19): edge straightness and centreline symmetry of the binary outline (pairs are bigger and
scatter more in pixel units, no separation), width variation along the hull (weak), spill outside the box (about 0
for everything, the opening already removes thin rays). The 150-400 m length window stays the only absolute limit. The old possible band (`width_min_beams_possible`, `overlap_min_possible`) now only sets `weak_evidence`; the
verdict is `single` (`weak_pair_verdict`), as for Sentinel-2.

## s1shape

The single-box veto traces half-power excess above a local sea median, not midpoint dB and not the binary
15 dB outline. A logarithmic midpoint traces a wider halo and can turn a short bright head into a false step.
Both body edges share one fitted slope; all finite row residuals contribute, so trimming cannot hide a
second return. Local sea noise over edge gradient and pixel quantisation define edge uncertainty.

Ai2 length supplies the longitudinal reference; only its central body is traced. The measured width is
perpendicular to the fitted sides, but the displayed full box length remains an Ai2 prior rather than an
independent pixel-derived vessel length. The pass blur belongs to the old outline extraction and must not be
subtracted from a different intensity contour. Observed SAR dimensions are not exact steel-hull dimensions.

A good one-beam fit alone cannot veto a pair: the half-power boundary can follow only the brighter hull.
Every sampled central row is therefore also searched for a separately supported peak outside the fitted body.
Any such peak blocks the veto. Missing central coverage, unknown dimensions or multiple own detections cannot
supply single-hull evidence. Very close or unresolved weak returns remain a sensing limitation; this is a
conservative veto of the existing pair rule, not a replacement two-vessel classifier. Parameters and measured
basis live in `rules/sts_graph.json` under `s1_single_box`; old frozen graphs without that group retain their
historical behavior. Measurements and rejected alternative box models are in the feasibility audit.
