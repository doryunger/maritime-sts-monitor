import json
from pathlib import Path

import numpy as np

from maritime_tip_cue.sts import rules as rules_mod
from maritime_tip_cue.sts import s1pair, s2pair, s2window
from maritime_tip_cue.sts.s1object import build, chip_window

FIXTURES = Path(__file__).resolve().parents[2] / "data" / "sts_fixtures"
S2_SETS = ("known", "s1", "s2", "s3")


def s2_results(root: Path = FIXTURES, rules: dict | None = None) -> list[tuple[dict, dict]]:
    rules = rules or rules_mod.load()
    manifest = json.loads((root / "s2" / "manifest.json").read_text(encoding="utf-8"))
    out = []
    for item in manifest["items"]:
        win = None
        if item["window"]:
            z = np.load(root / "s2" / "windows" / f"{item['window']}.npz")
            bands = {k[5:]: z[k] for k in z.files if k.startswith("band_")}
            scl = z["scl"] if "scl" in z.files else None
            win = s2window.window_from_arrays(bands, z["transform"], z["crs"], scl)
        out.append((item, s2pair.run(win, item["hull"], rules, manifest["shape_bands"])))
    return out


def s1_passes(root: Path = FIXTURES) -> list[str]:
    return sorted(p.parent.name for p in (root / "s1").glob("*/manifest.json"))


def s1_results(name: str, root: Path = FIXTURES, rules: dict | None = None) -> list[tuple[dict, dict]]:
    rules = rules or rules_mod.load()
    folder = root / "s1" / name
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    objects = []
    for obj in manifest["objects"]:
        z = np.load(folder / f"{obj['number']:03d}.npz")
        window = chip_window(z["vh"], z["vv"], z["transform"], z["crs"])
        start = (obj["start"]["x"], obj["start"]["y"])
        objects.append(build(window, start, obj["detections"], rules))
    return list(zip(manifest["objects"], s1pair.evaluate(objects, rules)))


def tally(pairs: list[tuple[str | None, str]]) -> dict:
    found = sum(1 for label, v in pairs if label == "pair" and v == "pair")
    total = sum(1 for label, _ in pairs if label == "pair")
    false = sum(1 for label, v in pairs if label == "single" and v == "pair")
    singles = sum(1 for label, _ in pairs if label == "single")
    return {"found": found, "pairs": total, "false": false, "singles": singles}
