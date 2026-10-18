# STS pair check

Decides whether a detected tanker is one ship or two ships side by side (a possible ship-to-ship transfer),
from Sentinel-2 (spectral) and Sentinel-1 (radar). All parameters are in `rules/sts_graph.json`; numeric floors
that only guard a division stay in code. Fixtures in `data/sts_fixtures` hold the expected verdicts of the
proven proof of concept and `tests/test_sts_fixtures.py` reproduces them to 1e-6.

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
single. A hull faster than the stopped limit is `moving`; a window with no water peak is `unmeasured`.

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
