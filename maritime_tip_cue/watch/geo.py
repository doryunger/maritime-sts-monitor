import math
from math import asin, cos, radians, sin, sqrt

from shapely.affinity import scale
from shapely.geometry import Point
from shapely.ops import unary_union

from maritime_tip_cue import land


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = radians(lat1), radians(lat2)
    a = sin((p2 - p1) / 2) ** 2 + cos(p1) * cos(p2) * sin(radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371008.8 * asin(sqrt(a))


def offset_m(a: dict, b: dict, heading: float | None) -> tuple[float, float | None, float | None]:
    north = (b["lat"] - a["lat"]) * 110540
    east = (b["lon"] - a["lon"]) * 111320 * cos(radians(a["lat"]))
    dist = math.hypot(north, east)
    if heading is None:
        return dist, None, None
    hn, he = cos(radians(heading)), sin(radians(heading))
    return dist, abs(north * hn + east * he), abs(east * hn - north * he)


class Shore:
    def __init__(self, bbox: list[float], margin_m: float):
        pad = margin_m / 110540 * 1.5 + 0.01
        w, s, e, n = bbox
        self.k = max(cos(radians((s + n) / 2)), 0.1)
        parts = land.within((w - pad, s - pad, e + pad, n + pad))
        parts = [g for g in parts if not g.is_empty]
        self.land = scale(unary_union(parts), xfact=self.k, yfact=1, origin=(0, 0)) if parts else None

    def distance_m(self, lon: float, lat: float) -> float:
        if self.land is None:
            return float("inf")
        return self.land.distance(Point(lon * self.k, lat)) * 110540


class Structures:
    def __init__(self, bbox: list[float], margin_m: float, points: list[tuple[float, float, str]]):
        pad = margin_m / 110540 * 1.5
        w, s, e, n = bbox
        self.points = [p for p in points if w - pad <= p[0] <= e + pad and s - pad <= p[1] <= n + pad]

    def nearest_m(self, lon: float, lat: float) -> float:
        return min((haversine_m(lat, lon, p[1], p[0]) for p in self.points), default=float("inf"))
