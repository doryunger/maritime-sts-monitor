from rasterio.warp import transform


def utm_crs(lon: float, lat: float) -> str:
    return f"EPSG:{(32600 if lat >= 0 else 32700) + int((lon + 180) // 6) + 1}"


def shift_point(lon: float, lat: float, crs: str, res: float, east_px: float, north_px: float) -> tuple[float, float]:
    xs, ys = transform("EPSG:4326", crs, [lon], [lat])
    xs, ys = transform(crs, "EPSG:4326", [xs[0] + east_px * res], [ys[0] + north_px * res])
    return xs[0], ys[0]


def point_shift(rules: dict, source: str) -> tuple[float, float]:
    return tuple(rules["ai2_point_correction"][source])


def correct_features(features: list[dict], source: str, rules: dict) -> list[dict]:
    east, north = point_shift(rules, source)
    out = []
    for f in features:
        p = f["properties"]
        proj = p.get("projection") or {}
        if p.get("point_correction_px") is not None or not proj.get("crs"):
            out.append(f)
            continue
        lon, lat = shift_point(p["longitude"], p["latitude"], proj["crs"], abs(proj["x_resolution"]), east, north)
        corrected = p | {"longitude": lon, "latitude": lat, "point_correction_px": [east, north]}
        out.append(f | {"properties": corrected, "geometry": {"type": "Point", "coordinates": [lon, lat]}})
    return out


def correct_hull(hull: dict, source: str, rules: dict, res: float) -> dict:
    east, north = point_shift(rules, source)
    lon, lat = shift_point(hull["lon"], hull["lat"], utm_crs(hull["lon"], hull["lat"]), res, east, north)
    return hull | {"lon": lon, "lat": lat}
