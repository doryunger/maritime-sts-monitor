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
