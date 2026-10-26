from PIL import Image

from maritime_tip_cue import publish

YELLOW = (255, 214, 0)
WHITE = (255, 255, 255)


def pixel_near(image, x, y, radius=3):
    return {image.getpixel((int(x + dx), int(y + dy))) for dx in range(-radius, radius + 1) for dy in range(-radius, radius + 1)}


def close(colour, target, tolerance=30):
    return all(abs(a - b) <= tolerance for a, b in zip(colour, target))


def test_known_vessels_are_yellow_and_unknown_are_white():
    img = Image.new("RGB", (360, 360), (20, 20, 20))
    points = [{"n": 1, "fx": 0.25, "fy": 0.5}, {"n": 2, "fx": 0.75, "fy": 0.5}]
    marked = publish.draw_markers(img, points, [True, False])
    known = [c for c in pixel_near(marked, 90, 180, 2) if close(c, YELLOW)]
    unknown = [c for c in pixel_near(marked, 270, 180, 2) if close(c, WHITE)]
    assert known and unknown
    assert not [c for c in pixel_near(marked, 270, 180, 2) if close(c, YELLOW)]


def test_pin_sits_away_from_the_other_vessel_like_the_review_app():
    img = Image.new("RGB", (360, 360), (20, 20, 20))
    points = [{"n": 1, "fx": 0.25, "fy": 0.5}, {"n": 2, "fx": 0.75, "fy": 0.5}]
    marked = publish.draw_markers(img, points, [True, True])
    offset = round(16 * publish.MARKER_SCALE)
    assert any(close(c, YELLOW) for c in pixel_near(marked, 90 - offset, 180, 2))
    assert any(close(c, YELLOW) for c in pixel_near(marked, 270 + offset, 180, 2))


def test_marker_style_has_no_black_filled_disc():
    img = Image.new("RGB", (360, 360), (200, 200, 200))
    marked = publish.draw_markers(img, [{"n": 1, "fx": 0.4, "fy": 0.4}, {"n": 2, "fx": 0.6, "fy": 0.6}], [False, False])
    assert not any(close(c, (0, 0, 0), 20) for c in pixel_near(marked, 0.4 * 360, 0.4 * 360, 2))


def test_markers_are_larger_than_the_review_app_dot():
    img = Image.new("RGB", (360, 360), (20, 20, 20))
    marked = publish.draw_markers(img, [{"n": 1, "fx": 0.5, "fy": 0.5}, {"n": 2, "fx": 0.8, "fy": 0.5}], [True, True])
    assert close(marked.getpixel((180 + 4, 180)), YELLOW)
    assert publish.MARKER_SCALE > 1


def test_a_single_capture_is_the_plain_crop_without_any_marker(tmp_path, monkeypatch):
    crop = Image.new("RGB", (360, 360), (20, 20, 20))
    monkeypatch.setattr(publish.report, "render", lambda *a: crop)
    monkeypatch.setattr(publish.report, "vessel_points", lambda *a: (_ for _ in ()).throw(AssertionError("single must not look for markers")))
    f = {"capture_utc": "2026-10-21T06:52:00Z", "site": "x", "source": "s2", "folder": "f", "scene_data": {}, "root": tmp_path, "record": {}, "parties": {}}
    out = publish.write_captures(f, "single", {}, tmp_path, single=True)
    saved = Image.open(out["png"]).convert("RGB")
    assert saved.getcolors() == [(360 * 360, (20, 20, 20))]
