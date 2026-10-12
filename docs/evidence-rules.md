# Evidence rules

How this repo looks for a tip's vessel in satellite imagery and decides how strong the evidence is.
Input is a tip ([contract](../README.md#input-the-tip), [how tips are made](tip-rules.md)).

The steps, sensors, pairing rules, grades and their parameters live in
[`rules/evidence_graph.json`](../rules/evidence_graph.json). This file holds the assumptions behind
them and how to read the graph; values are not repeated here.

## Assumptions

1. **Detectors find ships, not identities.** Identity is inferred: a detection of compatible
   length and heading near where the vessel should be at the capture time.
2. **Any nearby capture is useful.** A capture before the event shows the vessel while it was still
   reporting and how the detector sees it; it is labelled with its offset from the event.
3. **Optical shows, radar confirms.** Optical (Sentinel-2, Landsat) shows the object; radar
   (Sentinel-1) shows a real hard target, not a wake, cloud or whitecap. Radar also works at night
   and through cloud, so it can be the only evidence.
4. **Cross-checking needs a stationary ship.** Optical and radar pass hours apart. A moving ship
   travels tens of km in between, so a pair only counts for a moving ship if its movement in the gap
   stays within the match distance; a stationary ship allows a longer gap.
5. **The radar model needs an empty reference.** Sentinel-1 detection compares the capture with one
   image of the same spot 30–90 days earlier, so fixed structures cancel out. The reference is not
   evidence; it is fixed by how the model was trained.
6. **Known neighbours explain detections.** Vessels in `context_vessels` are matched first, so a
   ship that is accounted for is not taken for the subject.
7. **Detector output needs cleaning.** Stage 0 showed detections on inland water and one ship
   detected twice; detections on land are dropped and overlapping ones merged before matching.
8. **"Nothing" is a result.** No capture, nothing found and failure are recorded outcomes.

## Reading the graph

| Node kind | Meaning |
|---|---|
| `input` | The tip |
| `step` | One processing step and its parameters |
| `sensor` | A source of captures, its `role` (optical/radar) and its own thresholds |
| `reference` | The Sentinel-1 reference image rule |
| `state` | How the subject is treated for pairing: `stationary` or `moving` |
| `outcome` | A recorded result with no grade: `no_capture`, `nothing_found`, `failed` |
| `grade` | Evidence grade, `rank` 1 = strongest |

| Edge relation | Meaning |
|---|---|
| `feeds` | Order of steps; `when` gives the branch condition |
| `searches` | Which sensors the scene search queries |
| `classifies` | Tip → state, from the subject's last speed |
| `pairs` | Optical sensor ↔ radar sensor for a given `state`: the largest time gap at which their matches confirm each other |
| `grants` | Which match results give which grade (`requires` all, `excludes` any) |

`basis` says how solid a value is (a string, or per field when a node mixes them): `decision`
agreed choice, `model` fixed by Ai2's model or API, `stage0` derived from the stage 0 detector test
([feasibility](feasibility.md)), `guess` placeholder, `tbd` not set yet (`null`). Still `tbd`: the
per-sensor length tolerance and the stationary pairing gap.
