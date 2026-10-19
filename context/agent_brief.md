# Agent brief (read this instead of the full docs)

Conventions are in CLAUDE.md (already loaded). Read docs by section only: `grep -n '^## ' <file>`, then
`sed -n` the range. Use `.venv/Scripts/python`. Print compact output.

## Module map
- `maritime_tip_cue/sts/`: STS pair check. `s2window.py`, `s2pair.py` (Sentinel-2 hull pair), `s1object.py`,
  `s1pair.py` (Sentinel-1 outline pair), `ai2point.py` (Ai2 point half-pixel shift), `rules.py` (reads
  `rules/sts_graph.json`), `fixtures.py` (fixture loaders and tallies), `score.py` (CLI summary).
  Why notes: `maritime_tip_cue/context/sts.md`.
- `maritime_tip_cue/hotspots.py`: loads the shared hot-spot list from S3 (`config.yaml` `s3`, bucket and keys in `.env`).
- `maritime_tip_cue/config.py`: `.env` and `config.yaml`. `db/`, `__main__.py initdb`: AIS-era scaffold, removed in stage 2.

## Rules
- `rules/sts_graph.json`: scene selection, detection cleanup, pair check, AIS matching. Values only there; unknown stays `null` with `basis: "tbd"`.

## Data (gitignored `data/`)
- `data/sts_fixtures/s1`, `data/sts_fixtures/s2`: expected verdicts of the STS proof of concept.
- `data/poc_from_vessels_aggregator/`: imagery and STS outputs moved from vessels-aggregator.
- Stage 0 detector bench (outside the repo): `C:\Users\Shadow\projects\ai2-detectors\` (README there).

## Docs
- `README.md`: hot spots, processing, S3 output tree, decisions. `PLAN.md`: stages and open gaps.
- `docs/feasibility.md`: measured research. Tip-era docs and rules: tag `tip-era-final`.

## Common commands (repo root)
- `.venv/Scripts/python -m pytest tests -q` fixture tests (STS verdicts to 1e-6).
- `.venv/Scripts/python -m maritime_tip_cue.sts.score` per-set STS tallies.
- `.venv/Scripts/python -m maritime_tip_cue.hotspots [publish <file>]` print (or replace) the S3 hot-spot list.
