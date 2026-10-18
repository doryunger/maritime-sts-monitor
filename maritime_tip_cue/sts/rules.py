import json
from functools import cache
from pathlib import Path

RULES_PATH = Path(__file__).resolve().parents[2] / "rules" / "sts_graph.json"


@cache
def _load(path: str) -> dict:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return {
        group: {key: entry["value"] for key, entry in body.items() if isinstance(entry, dict) and "value" in entry}
        for group, body in raw.items()
        if isinstance(body, dict)
    }


def load(path: Path | None = None) -> dict:
    return _load(str(path or RULES_PATH))


S1_GROUPS = ("s1_outline", "s1_shape", "s1_clean", "s1_inside", "s1_two", "s1_pairrule")


def s1_config(rules: dict) -> dict:
    merged = {"hull_length_to_beam": rules["pair_split"]["hull_length_to_beam"]}
    for group in S1_GROUPS:
        merged.update(rules[group])
    return merged
