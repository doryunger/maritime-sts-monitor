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
    assert any(close(c, YELLOW) for c in pixel_near(marked, 90 - 16, 180, 2))
    assert any(close(c, YELLOW) for c in pixel_near(marked, 270 + 16, 180, 2))


def test_marker_style_has_no_black_filled_disc():
    img = Image.new("RGB", (360, 360), (200, 200, 200))
    marked = publish.draw_markers(img, [{"n": 1, "fx": 0.4, "fy": 0.4}, {"n": 2, "fx": 0.6, "fy": 0.6}], [False, False])
    assert not any(close(c, (0, 0, 0), 20) for c in pixel_near(marked, 0.4 * 360, 0.4 * 360, 2))
