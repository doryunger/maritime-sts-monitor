# Use cases

The suspicious activities seen so far in recorded vessel snapshots, what imagery has to show to
confirm each one, and the data and imagery sources behind them. These are the cases this repo has
to handle; they come from a proof of concept in `vessels-aggregator` (2026-10-11/12), not from a
deployed producer.

How a finding becomes a tip is in [tip rules](tip-rules.md); how a tip is checked against imagery
is in [evidence rules](evidence-rules.md). Where a use case maps to an existing rule in
[`rules/tip_graph.json`](../rules/tip_graph.json), the rule id is given.

## Status

| # | Use case | Snapshot signal | Imagery question | Evidence so far | `tip_graph` rule | Status |
|---|---|---|---|---|---|---|
| 1 | [Frozen position](#1-frozen-position) | Identical coordinates over many fresh reports | Is the vessel at its reported point? If not, where is it? | KRONOS: no ship within 4 km on Sentinel-1 | *new* | Strong lead |
| 2 | [GNSS interference zone](#2-gnss-interference-zone) | Impossible speeds, identical false values, frozen positions while "underway" | Are the vessels where they claim? Where are they really? | Red Sea cluster; satellite check pending (pass 2026-10-13) | `mass_displacement`, `position_jump`, `implausible_data` | Strong lead |
| 3 | [Position not confirmed by imagery](#3-position-not-confirmed-by-imagery) | Any reported position | Is there a ship of matching size at the projected point? | 89% of checks confirmed; the rest explained except KRONOS | evidence side | Working (PoC) |
| 4 | [Self-declared affiliation](#4-self-declared-affiliation) | Destination field used as a message | None; context for scoring | ~160 vessels, Red Sea / Gulf of Aden | `threat_signalling` | Candidate |
| 5 | [Concealed destination and risky flag](#5-concealed-destination-and-risky-flag) | `FOR ORDERS`, landlocked or high-risk flag | None; raises the score of 1–2 | RU/IR crude tankers 40% `FOR ORDERS` vs 17% | modifier | Candidate |
| 6 | [Meeting at sea, loitering](#6-meeting-at-sea-and-loitering) | Stopped pairs offshore, long stops | Two hulls side by side? Still there? | 4 close stopped pairs; 309 long stops, mostly anchorages | `ship_to_ship`, `stopped_at_sea`, `loitering` | Needs an anchorage map |
| 7 | [Going dark](#7-going-dark) | Vessel stops appearing | Is it still there without AIS? | Disappearance is mostly capture coverage | `going_dark` | Blocked |

## The use cases

### 1. Frozen position

**Signal.** A vessel reports exactly the same coordinates in every fresh report (`report_age_min`
≤ 30) for hours. A real GPS fix moves by metres from report to report even at anchor, so identical
coordinates mean a fixed or spoofed position, not a real one.

**Baseline.** 11 of 1,284 vessels with ≥ 10 fresh reports (0.9%). Two kinds:

| Kind | Vessels | Reading |
|---|---|---|
| Frozen while reporting speed | ARAMON 10 kn, ATHENS VOYAGER 12.5 kn, AL JMELIYAH 14.8 kn, HELEN 11.6 kn, GHINAH 10.8 kn (all Red Sea) | Physically impossible: the receiver is locked on a spoofed point while the ship moves (see use case 2) |
| Frozen, stationary | KRONOS (RU crude tanker, 248 m), GERAN (RU tanker, Port Said anchorage, `FOR ORDERS`), ARTUNIS, SERRANO | Position typed in or spoofed; round values (ARTUNIS 37.5833 = 37° 35') point to manual entry |
| Frozen by design | ENERGEAN POWER (FPSO) | Moored production unit; exclude FSO/FPSO |

**Lead: KRONOS.** Destination BATUMI, reported at exactly 41.686245, 29.170073 (Black Sea, ~50 km
north of the Bosphorus), speed 0, in all 38 fresh reports over 22 h. Sentinel-1
`S1D_IW_GRDH_1SDV_20261012T041348_20261012T041413_004977_00964D` (2026-10-12 04:14 UTC) shows open
sea and no ship-sized target within 4 km of that point; georeferencing error there < 30 m.

**What this repo needs.** The reported position is the lie, so searching near it only proves
absence. The search has to start from the last trusted position (before the freeze) and cover the
area reachable since, at the vessel's class speed. Candidates are unmatched detections of the
vessel's length in that area; the vessel's other passes before and after confirm or drop them.
This is whole-scene detection, which the Ai2 detectors already do.

### 2. GNSS interference zone

**Signal.** In one area, several unrelated vessels report impossible values at the same time:
bulk carriers at 38–91 kn, three different ships at exactly 38.7 kn, frozen positions while
reporting 10–15 kn (use case 1). Identical false values across ships point to a spoofed signal,
not a broken receiver.

**Seen in.** Red Sea 18–22° N, 37–39° E (DONATELLO 53–77 kn in every run, NJ CALLISTO 46–63 kn,
ANGELICA MAERSK, COSCO SHIPPING ROSE, TOPTERIX); off Israel (MSC BRUNELLA 34 kn); off Dubai (KPS
ORKA SULTAN 91.5 kn, one run).

**Imagery question.** Same as use case 1 for each vessel in the zone. A zone-level answer is
useful on its own: while interference is active, every reported position there is untrusted and
other rules should down-weight it.

**Test case.** Sentinel-1 passes the Red Sea zone on a 12-day cycle with four passes (~03:15 UTC
descending ×3, ~15:xx ascending ×1); first pass with corrected snapshot times: 2026-10-13 ~03:15
UTC.

### 3. Position not confirmed by imagery

**What it is.** The evidence step for every other case: project the reported position to the
capture time and look for a ship of compatible length there. PoC detector: threshold on the image
patch (CPU), not the Ai2 models.

**Results** (all 77 Sentinel-1 scenes over the capture area, 2026-10-11 18:20 to 2026-10-12):

| | Checks with data | Confirmed |
|---|---|---|
| Stationary vessels | 60 | 57 (95%) |
| Moving vessels | 81 | 68 (84%) |
| All | 141 | 125 (89%) |

Unconfirmed: mostly one scene off Mumbai where the snapshot's cache delay exceeded the 30 min
allowance, a few ~110 m ships at 10–12 kn (below the threshold detector), and KRONOS.

**Lessons for matching** (apply to any detector):
- **Report time.** Snapshot positions come from map tiles cached up to 30 min. Report time must
  be computed from the tile's own time, not the fetch time; before that fix, moving ships were
  found 20–30 min of sailing ahead of their projected position.
- **SAR Doppler shift.** A moving ship appears displaced along the satellite track by ~112 s ×
  its speed toward the radar, up to ~750 m at 13 kn. The match radius has to grow with speed.
- **Search along course, not in a circle.** When the report time is uncertain, the vessel is
  somewhere on a stretch of its course line, not around one point.
- **SAR georeferencing.** Fitting the GCPs including mountain heights put sea positions 1–3 km
  off; sea-level GCPs only (< 30 m). Relevant when reading GRD windows directly instead of Ai2's
  full reprojection (stage 3, whole-scene processing gap).

### 4. Self-declared affiliation

**Signal.** About 160 vessels use the destination field as a message rather than a port, almost
all near the Red Sea and Gulf of Aden: `CHINESE CREW&OWNER`, `ARMED GUARDS ONBOARD`,
`VSL NO LINK ISRAEL`, `RUSSIA OFFICER&GUARD` (a crude tanker), `FOOD FOR IRAN`.

**Use.** Shows who is about to transit the risk area and what they claim. A claim that changes, or
contradicts the flag or the vessel's history, adds to an incident's score. No imagery question.

### 5. Concealed destination and risky flag

**Signal.** `FOR ORDERS` / `TBA` / `TBN` instead of a port, and flags from landlocked or
high-risk registries.

| Group (crude tankers) | Share with a concealed destination |
|---|---|
| RU / IR flag | 40% |
| High-risk registries | 24% |
| Other flags | 17% |

Landlocked flags on very large crude carriers: Malawi (7 ships, all crude tankers, average 308 m),
Eswatini (PING SHUN), Zimbabwe (MIMAS, 335 m). Others often used to evade scrutiny: Equatorial
Guinea, Cameroon, Sierra Leone, Comoros, Guinea-Bissau, San Marino.

**Use.** A score modifier, not a tip on its own: normal trading also uses `FOR ORDERS`. It raises
the priority of use cases 1, 2 and 6 for the same vessel.

### 6. Meeting at sea and loitering

**Signal.** Two stopped vessels within 0.3 nm offshore (4 pairs in the first 21 snapshots, none
tanker–tanker); vessels under 1 kn for 10 h or more (309, mostly cargo and tankers).

**Blocker.** Almost all long stops are at anchorages. Without a port/anchorage map (`zone`) neither
rule can tell a meeting or loitering from normal waiting. Snapshots also miss small craft (only
cargo and tanker types are stored), so a tanker meeting a bunker barge is not visible in the data;
imagery would show it.

### 7. Going dark

**Signal.** A vessel stops appearing in the snapshots.

**Blocker.** In the recorded data ~13% of vessels drop out between consecutive snapshots because
the capture is a sample (zoom 5 shows a subset of vessels), not because they switch off. Until
coverage is measured, absence is not evidence. Imagery could still answer "is it there without
AIS", but only once the snapshot side can flag a real disappearance.

### Dropped

| Idea | Why |
|---|---|
| Stale report alone | 26–32% of vessels are over 3 h old at any time; normal, not suspicious |
| Disappearance alone | Mostly capture coverage (use case 7) |
| Cloned identity (same name, two ids) | All pairs found had different dimensions: common names |
| Two vessels at one exact position | Never seen |

## Data behind the use cases

### Vessel snapshots

| Item | Value |
|---|---|
| Collector | `vessels-aggregator` on the existing Lightsail host |
| Source | Public vessel map tiles (zoom 5, ~5° S–45° N, 18–95° E), read from the page's own requests in a headed browser |
| Cadence | Every 30 min |
| Stored | Cargo and tanker types only (AIS ship type 7 and 8), as GeoParquet; queried with DuckDB |
| Retention | Raw + CSV + parquet 3 days; parquet copies 30 days (for imagery checks) |
| Fields | Provider vessel id, name, flag, type and sub-type, length, width, dwt, position, speed, course, heading, rate of turn, destination, report age, tile time |

Limits that matter here:
- **A sample, not full traffic.** Zoom 5 shows a subset; ~13% of vessels drop out between
  snapshots. An unmatched detection in imagery is not automatically a dark vessel.
- **No MMSI or IMO.** Vessels can't be matched directly against sanctions or shadow-fleet lists;
  only name, flag and dimensions.
- **Report time.** Computed from each tile's `Last-Modified` (tiles are cached up to 30 min by the
  provider's CDN). Snapshots before 2026-10-12 09:34 UTC lack it and can be up to 30 min off.
- **Satellite-only rows** (`[SAT-AIS]`) carry a new anonymous id on every serving and can't be
  tracked.

### Imagery

| Source | Product used | Access | Over the Red Sea hotspot | Delay after capture |
|---|---|---|---|---|
| Sentinel-1 (radar) | GRD | Planetary Computer, no account (Copernicus needs one) | 4 passes / 12 days, ~03:15 and ~15:xx UTC | ~6 h |
| Sentinel-2 (optical, 10 m) | L2A in the PoC; Ai2 needs L1C | Planetary Computer / public GCS, no account | Every few days, ~07:50–08:15 UTC, usually clear | ~1 day |
| Landsat 8/9 (optical, 30 m) | C2 L2 in the PoC; Ai2 needs L1 | Planetary Computer, no account (AWS L1 is requester-pays) | Several / 8 days, ~07:45–07:55 UTC | ~2 days |

Together, about one capture of the Red Sea hotspot a day. Because a vessel can only be checked when
a capture exists, the snapshot side keeps 30 days of positions, so late optical scenes and earlier
passes can still be matched.

Tried and dropped:

| Source | Why |
|---|---|
| NISAR (NASA-ISRO L-band radar) | Access works (Earthdata token; GCOV is UTM, 20 m, chunked, cheap to window-read), but acquisitions target land: Red Sea frames cover the coasts and the Saudi interior, not the open sea. Data ~2 days late |
| VIIRS night-time boat detection | Paid subscription; ~750 m, lit vessels only |

## What this means for this repo

- **Tip types.** `event.type` has no value for spoofing. Use cases 1 and 2 need one (e.g.
  `position_spoofing`), with the last trusted track point kept separate from the frozen/false
  ones.
- **Search area for spoofing tips.** Start from the last trusted position, not the reported one,
  and grow by class speed over the time since; this can exceed the current 60 km cap.
- **New tip rule.** `frozen_position` (layer B: identical coordinates over N fresh reports,
  FSO/FPSO excluded) is not in `rules/tip_graph.json` yet.
- **New evidence outcome.** "Not at reported position" (capture covers the point, no ship of
  matching length there) is evidence in its own right, distinct from `nothing_found` around a
  projected position.
- **Matching.** Report-time accuracy, along-course search and the SAR Doppler allowance (use case 3)
  belong in the identification rule that `PLAN.md` still lists as undefined.

## Test cases for stage 0 / 3

| Case | Scene | Expect |
|---|---|---|
| KRONOS absent at frozen point | `S1D_IW_GRDH_1SDV_20261012T041348_20261012T041413_004977_00964D` | No detection at 41.686, 29.170; candidates of ~250 m elsewhere in the scene to follow up |
| Moving ships found ahead of their AIS (cache delay) | `S1D_IW_GRDH_1SDV_20261012T023834_20261012T023859_004976_009642`, `…T023859_20261012T023924_004976_009642` (northern Gulf) | GFS PRECIOUS, ZHONG GU DA LIAN, XING SHENG detected 22–24 min of sailing ahead of their reported positions |
| Red Sea interference zone | First Sentinel-1 pass over 18–22° N, 37–39° E after 2026-10-13 | ARAMON, ATHENS VOYAGER, AL JMELIYAH, HELEN, GHINAH: not at their frozen points; candidates along their routes |

The PoC code (`imagery/verify.py`, threshold detector, Planetary Computer reads) and the full
findings log (`context/raw_vessels_aggregator.md`) live in `vessels-aggregator`.
