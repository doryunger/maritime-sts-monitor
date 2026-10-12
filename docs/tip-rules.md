# Tip rules

How vessel data becomes a tip. This logic runs **upstream**, in whatever produces tips; this repo
only receives the result ([tip contract](../README.md#input-the-tip)). It is kept here so the whole
reasoning chain can be read in one place.

The rules and their parameters live in [`rules/tip_graph.json`](../rules/tip_graph.json). This file
holds the assumptions behind them and how to read the graph; values are not repeated here.

## Assumptions

1. **Snapshots, not a stream.** Vessel data arrives as periodic snapshots per area, each giving
   every vessel's last known position and the minutes since that report. The only stable key is
   the provider's vessel id; there is no MMSI, IMO or navigational status.
2. **Stale is normal.** In the 2026-10-11 sample, 29% of vessels were over 3 h old. Silence is
   judged against the vessel's own reporting history, never against an absolute age.
3. **Missing is not dark.** A snapshot's coverage depends on area and zoom; a vessel can be absent
   because of the request, not because it switched off. Leaving the area or entering port must be
   ruled out before "going dark" fires.
4. **One rule is an alert, not a tip.** Alerts raise an incident's score; history (repeat
   offender, nearby alerts) boosts it; only an incident at or above the threshold becomes a tip.
5. **External reports outrank inference.** A UKMTO/JMIC report creates an incident directly.
6. **Suspicion triggers closer watching.** Some alerts open a focus watch: a small box around the
   last position, polled more often and grown at the vessel's last speed, until it reports again or
   the watch expires.

## Reading the graph

| Node kind | Meaning |
|---|---|
| `source` | Where data comes from: snapshots, external reports |
| `attribute` | A vessel field a rule reads (from the snapshot, or `zone` from a port/anchorage map) |
| `rule` | One check; `layer` A = within a snapshot, B = against the previous one, `pairs` = two vessels, D = report matching |
| `modifier` | History cross-reference (layer C) that boosts an incident |
| `watch` | Focus watch settings |
| `incident` | Scored collection of alerts; holds the promotion threshold |
| `output` | The tip |

| Edge relation | Meaning |
|---|---|
| `condition` | rule → attribute: what must hold for the rule to fire (`op`, `value`, `unit`). A rule fires when all its conditions hold |
| `raises` | rule or source → incident, with the alert's `weight` (or `direct` for reports) |
| `boosts` | modifier → incident |
| `opens` | rule → focus watch |
| `feeds` | focus watch → snapshot (its polls are processed like any snapshot) |
| `promotes` | incident → tip |

`basis` says how solid a value is (a string, or per field when a node mixes them): `decision`
agreed choice, `observed` seen in the sample data, `ais_spec` from the AIS standard, `guess`
placeholder, `tbd` not set yet (`null`). Every `guess` and `tbd` needs tuning on recorded
snapshots; none of the alert weights are set yet.

## What a tip must carry

The tip is the only thing this repo sees, so the producer passes on what the evidence search needs:
the event type, time and position, the subject vessel with its track and size, other involved
vessels, and if possible every vessel known in the area over the window (`context_vessels`).
Without sizes and context vessels, matching relies on position alone. See
[evidence rules](evidence-rules.md).
