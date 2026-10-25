# Equasis identity enrichment

## Why and when
AIS carries a name and a flag but not the IMO number, MMSI, call sign, gross tonnage, DWT or build year; Equasis is the
only source of those. The vessels aggregator used it to enrich suspicious AIS vessels for tips. That tip step is gone,
so the enrichment moved here and runs when a pair exists: for each known vessel on a published pair (one or two), in
the order of its slot. `publish.collect_at` calls `equasis.resolve.enrich` after the archive's own identities were
read; a ship with a `vessel_identities` row is never looked up again, whether it came from the archive or from Equasis.

## Rules of use
Equasis forbids bulk retrieval and the account owner accepted per-suspect use only. So: one vessel at a time, no loop
over AIS vessels, a delay between requests (`identity.request_delay_s`, 2 s, down from the aggregator's 5 s because a
pair run is a handful of lookups), a cap per run (`identity.max_lookups_per_run`) and a JSON cache
(`equasis.cache` in `config.yaml`, under ignored `data/`) so a name or IMO is never asked twice. A lost session stops
the run and is reported; it never stops publication. Without `EQUASIS_EMAIL` and `EQUASIS_PASSWORD` in `.env` the
step is skipped.

## Resolving
The AIS name is searched; each candidate is scored on flag (2), type (1) and gross tonnage per DWT (2), weights and
limits in `rules/sts_graph.json` `identity`. A candidate needs a medium score (3) to count, ties stay unresolved, and
a DWT within tolerance of the AIS DWT, read from the IMO details page, makes it strong. The record keeps IMO, MMSI,
call sign, gross tonnage, DWT, year built, type, status, the Equasis name and flag, the false-flag marker, confidence
and the reasons. IMO and MMSI also fill the matching `ais_matches` slot where it had none.

## Where it ends up
The run's `vessel_identities` table, then the published row per vessel: `vessel_N_imo`, `_mmsi`, `_call_sign`,
`_gross_tonnage`, `_dwt`, `_year_built`, `_vessel_type`, `_status`, `_identity_confidence`, `_identity_source`
(`archive` or `equasis`). A re-publish keeps an identity already found for the same ship.

## Copied from the aggregator
`equasis/client.py` and `equasis/resolve.py` are copies of `reference/equasis.py` and `reference/resolve.py` of the
vessels aggregator, owned here from now on. The sanctions check, the archive-wide selection and the CSV output were not
taken: the archive already supplies `is_sanctioned`, and results live in the run database.
`python -m maritime_tip_cue.equasis <run numbers>` enriches existing runs.
