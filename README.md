# naval-tip-cue

Listen to naval events and, on selected cues, search free open imagery for the object the event is
about.

## Levels

1. **Tip: AIS events.** A live AIS stream ([aisstream.io](https://aisstream.io)) feeds rules that
   raise events: a vessel going dark, two vessels meeting at sea, loitering.
2. **Coarse cue: VIIRS night lights.** Daily, 750 m. Confirms that a lit vessel was present near
   the event, not which one.
3. **Object cue: Sentinel-1 / Sentinel-2 / Landsat.** The capture closest in time to the event,
   searched around the last known position widened by how far the vessel could have moved. A
   ship detector runs on the crop.

No free source gives daily ship-resolving imagery, so "no capture in the window" is a normal
outcome, not an error.

## Sources

| Source | Access | Cost |
|---|---|---|
| aisstream.io | WebSocket, API key via GitHub sign-in | Free |
| Copernicus Data Space (Sentinel-1/2) | STAC / OData, free account | Free |
| Landsat (USGS) | STAC | Free |
| NASA VIIRS | Earthdata account | Free |

## Vessel position snapshots

`naval_tip_cue/positions.py` turns a vessel-positions JSON snapshot into a table, one row per
vessel, as CSV, JSONL or GeoJSON.

```
python -m naval_tip_cue positions snapshot.json [--format csv|jsonl|geojson] [--out file] [--no-sat] [--fetched ISO-TIME]
```

Input is JSON of the form `{"data": {"rows": [...]}}`, one row per vessel. Each row becomes:

| Output | From | Notes |
|---|---|---|
| `ship_id`, `name`, `flag`, `destination` | `SHIP_ID`, `SHIPNAME`, `FLAG`, `DESTINATION` | as given |
| `lat`, `lon` | `LAT`, `LON` | degrees |
| `speed_kn` | `SPEED` | raw value is knots × 10 |
| `course`, `heading`, `rot` | `COURSE`, `HEADING`, `ROT` | blank when missing (not 0) |
| `elapsed_min`, `seen_utc` | `ELAPSED` | minutes since last report; `seen_utc` = fetch time minus that |
| `type`, `type_code`, `gt_type_code` | `TYPE_NAME` / `SHIPTYPE`, `GT_SHIPTYPE` | 2 fishing, 3 tug/special, 4 high speed, 6 passenger, 7 cargo, 8 tanker, 9 pleasure |
| `length_m`, `width_m`, `dwt` | `LENGTH`, `WIDTH`, `DWT` | |
| `status` | `STATUS_NAME` | only on satellite rows |
| `is_sat` | `SHIPNAME == "[SAT-AIS]"` | anonymised satellite fix |
| `state` | derived, see below | estimate, not reported by the vessel |

### Estimated state

Named vessels carry no navigational status, and satellite rows only carry `status` as a raw
string. `state` is estimated from the movement fields, first rule that applies wins:

| state | rule |
|---|---|
| `stale` | last report older than 180 min, so the position says little about now |
| `unknown` | no speed |
| `at anchor (swinging)` | under 3 kn and heading more than 45° off course |
| `stopped` | under 1 kn |
| `slow` | 1 to 3 kn |
| `underway` | 3 kn or more |

The thresholds are constants at the top of `positions.py`. Moored and anchored vessels both
usually show as `stopped` or `at anchor (swinging)`; telling them apart needs a port/berth map.

- Satellite rows have an opaque ID and no name, flag or size. Many sit next to a named row with the
  same course and speed, so they are likely duplicates; `--no-sat` drops them.
- `--fetched` sets the fetch time for `seen_utc` when processing an older saved file (default now).
- GeoJSON output is a FeatureCollection of points, ready to load into a map.
- A summary (count, satellite count, count per type and per state) goes to stderr.

The field meanings above were inferred from the data, not from a published spec.

The data is a position snapshot only. It holds no alerts or events; those need rules applied
across repeated snapshots (e.g. a vessel disappearing, two vessels stopped close together, or
loitering).
