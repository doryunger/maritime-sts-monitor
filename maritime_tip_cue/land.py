import json
from functools import cache

from shapely.geometry import box, shape
from shapely.strtree import STRtree

from maritime_tip_cue.config import ROOT, settings


@cache
def _tree() -> tuple[STRtree, list]:
    geoms = []
    for path in settings()["land"]:
        for f in json.loads((ROOT / path).read_text(encoding="utf-8"))["features"]:
            g = shape(f["geometry"])
            geoms += list(g.geoms) if g.geom_type == "MultiPolygon" else [g]
    return STRtree(geoms), geoms


def within(bounds: tuple[float, float, float, float]) -> list:
    tree, geoms = _tree()
    area = box(*bounds)
    return [geoms[i].intersection(area) for i in tree.query(area)]
