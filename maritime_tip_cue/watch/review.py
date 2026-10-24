import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from rasterio.warp import transform as warp_transform

from maritime_tip_cue.sts import s2window
from maritime_tip_cue.watch import s2 as s2mod

COLOURS = {"pair": (255, 60, 60), "possible_pair": (255, 190, 0)}


def corners(cx: float, cy: float, heading: float, length: float, width: float) -> list[tuple[float, float]]:
    al, ac = s2window.axes(heading)
    return [
        (cx + sl * length / 2 * al[0] + sw * width / 2 * ac[0], cy + sl * length / 2 * al[1] + sw * width / 2 * ac[1])
        for sl, sw in ((-1, -1), (1, -1), (1, 1), (-1, 1))
    ]


def draw_rect(draw: ImageDraw.ImageDraw, pts: list[tuple[float, float]], colour, width: int = 1) -> None:
    draw.line(pts + [pts[0]], fill=colour, width=width)


def crop_box(c: float, r: float, half: int, h: int, w: int) -> tuple[int, int, int, int]:
    r0, c0 = int(max(0, min(r - half, h - 2 * half))), int(max(0, min(c - half, w - 2 * half)))
    return r0, c0, r0 + 2 * half, c0 + 2 * half


def s2_tile(folder: Path, window_file: str, row: dict, cfg: dict, overlay: bool = True, ai2: bool = False) -> Image.Image:
    win = s2mod.stored_window(folder, window_file)
    chk = row["check"]
    tr = win["transform"]
    lat, lon = chk.get("hull_a_lat") or row["lat"], chk.get("hull_a_lon") or row["lon"]
    x, y = warp_transform("EPSG:4326", win["crs"], [lon], [lat])
    x, y = x[0], y[0]
    col, rw = (x - tr.c) / 10, (tr.f - y) / 10
    half = int(cfg["crop_half_m"] / 10)
    h, w = win["shape"]
    r0, c0, r1, c1 = crop_box(col, rw, half, h, w)
    rgb = np.stack([win["bands"][b][r0:r1, c0:c1] for b in ("B04", "B03", "B02")], axis=-1)
    img = Image.fromarray(np.clip(rgb / cfg["s2_stretch_max"] * 255, 0, 255).astype(np.uint8))
    k = cfg["scale"]
    img = img.resize((img.width * k, img.height * k), Image.NEAREST)
    if not overlay:
        return img
    d = ImageDraw.Draw(img)

    def px(p):
        return ((p[0] - tr.c) / 10 - c0) * k, ((tr.f - p[1]) / 10 - r0) * k

    colour = COLOURS.get(row["verdict"], (255, 255, 255))
    for which in ("a", "b"):
        la, lo = chk.get(f"hull_{which}_lat"), chk.get(f"hull_{which}_lon")
        ln, wd = chk.get(f"hull_{which}_length_m"), chk.get(f"hull_{which}_width_m")
        if None in (la, lo, ln, wd, chk.get("fix_heading")):
            continue
        hx, hy = warp_transform("EPSG:4326", win["crs"], [lo], [la])
        draw_rect(d, [px(p) for p in corners(hx[0], hy[0], chk["fix_heading"], ln, wd)], colour, 2)
    if ai2 and None not in (row.get("ai2_length_m"), row.get("ai2_width_m"), row.get("ai2_heading")):
        ax, ay = warp_transform("EPSG:4326", win["crs"], [row["lon"]], [row["lat"]])
        draw_rect(d, [px(p) for p in corners(ax[0], ay[0], row["ai2_heading"], row["ai2_length_m"], row["ai2_width_m"])], (0, 255, 255), 1)
    return img


def s1_tile(folder: Path, obj: dict, cfg: dict) -> Image.Image:
    z = np.load(folder / obj["file"])
    db = 10 * np.log10(np.clip(z["vh"], 1e-9, None))
    lo, hi = np.percentile(db, 5), np.percentile(db, cfg["s1_sea_clip_percentile"])
    g = np.clip((db - lo) / max(hi - lo, 1e-6), 0, 1)
    tr = z["transform"]
    h, w = g.shape
    x0, y0 = float(tr[2]), float(tr[5])
    col, rw = (obj["x"] - x0) / 10, (y0 - obj["y"]) / 10
    half = int(cfg["crop_half_m"] / 10)
    r0, c0, r1, c1 = crop_box(col, rw, min(half, h // 2, w // 2), h, w)
    img = Image.fromarray((g[r0:r1, c0:c1] * 255).astype(np.uint8)).convert("RGB")
    k = cfg["scale"]
    img = img.resize((img.width * k, img.height * k), Image.NEAREST)
    d = ImageDraw.Draw(img)
    for det in obj["detections"]:
        if None in (det["length_m"], det["width_m"], det["heading"]):
            continue
        pts = [(((x - x0) / 10 - c0) * k, ((y0 - y) / 10 - r0) * k) for x, y in corners(det["x"], det["y"], det["heading"], det["length_m"], det["width_m"])]
        draw_rect(d, pts, (255, 255, 0) if det["own"] else (0, 255, 255), 1)
    return img


def caption(img: Image.Image, lines: list[str], colour) -> Image.Image:
    out = Image.new("RGB", (img.width, img.height + 12 * len(lines) + 4), (0, 0, 0))
    out.paste(img, (0, 12 * len(lines) + 4))
    d = ImageDraw.Draw(out)
    for i, line in enumerate(lines):
        d.text((2, 2 + 12 * i), line, fill=colour)
    return out


def sheet(tiles: list[Image.Image], per_row: int, path: Path) -> None:
    w = max(t.width for t in tiles)
    h = max(t.height for t in tiles)
    rows = math.ceil(len(tiles) / per_row)
    img = Image.new("RGB", (per_row * (w + 4), rows * (h + 4)), (20, 20, 20))
    for i, t in enumerate(tiles):
        img.paste(t, ((i % per_row) * (w + 4), (i // per_row) * (h + 4)))
    img.save(path)


def build(store, out: Path, rules: dict) -> Path | None:
    cfg = rules["review"]
    tiles = []
    for scene in store.scenes():
        if scene["status"] != "checked":
            continue
        folder = out / scene["folder"]
        for row in store.hulls(scene["scene_id"], scene["site"]):
            if row["verdict"] not in COLOURS:
                continue
            colour = COLOURS[row["verdict"]]
            when = scene["capture_utc"][5:16].replace("T", " ")
            if scene["source"] == "s2":
                window = next((w["file"] for w in scene["data"]["index"] if row["det_id"] in w["members"]), None)
                if window is None:
                    continue
                img = s2_tile(folder / "s2_windows", window, row, cfg)
                chk = row["check"]
                ratio = chk.get("width_ratio")
                lines = [f"S2 {row['verdict']} {when}Z", f"{row['lat']:.4f}N {row['lon']:.4f}E", f"w {ratio:.2f}" if ratio is not None else "w n/a"]
            else:
                img = s1_tile(folder / "chips", row["object"], cfg)
                lines = [f"S1 {row['verdict']} {when}Z", f"{row['lat']:.4f}N {row['lon']:.4f}E", f"L {row['length_m']:.0f} m w {row['width_beams']:.2f}"]
            tiles.append(caption(img, lines, colour))
    if not tiles:
        return None
    path = out / "review.png"
    sheet(tiles, cfg["per_row"], path)
    return path
