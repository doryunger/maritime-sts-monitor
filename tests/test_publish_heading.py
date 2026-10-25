from maritime_tip_cue import publish


def test_axial_mean_wraps_around_north():
    mean = publish.axial_mean([179.0, 1.0])
    assert mean < 1e-6 or mean > 180 - 1e-6
    assert abs(publish.axial_mean([80.0, 100.0]) - 90.0) < 1e-6


def test_axial_mean_ignores_missing_and_handles_empty():
    assert abs(publish.axial_mean([None, 45.0]) - 45.0) < 1e-6
    assert publish.axial_mean([None]) is None


def test_axial_mean_treats_opposite_directions_as_one_axis():
    assert abs(publish.axial_mean([10.0, 190.0]) - 10.0) < 1e-6


def test_map_angle_becomes_a_compass_heading():
    assert publish.compass(0.0) == 90.0
    assert publish.compass(90.0) == 0.0
    assert abs(publish.compass(25.262449) - 64.737551) < 1e-6


def test_sentinel1_uses_the_fitted_box_and_ignores_the_ai2_box():
    record = {"single_box": {"angle_deg": 25.262449}, "rect_angle_deg": 36.0, "ai2_boxes": [{"heading": 0.6}]}
    assert publish.pair_heading({"source": "s1", "record": record}) == (64.7, "fitted_box")


def test_sentinel1_falls_back_to_the_pixel_rectangle_not_the_ai2_box():
    record = {"single_box": None, "rect_angle_deg": 36.0, "ai2_boxes": [{"heading": 0.6}]}
    assert publish.pair_heading({"source": "s1", "record": record}) == (54.0, "pixel_rect")


def test_sentinel1_without_our_own_angle_has_no_heading():
    record = {"single_box": None, "rect_angle_deg": None, "ai2_boxes": [{"heading": 0.6}]}
    assert publish.pair_heading({"source": "s1", "record": record}) == (None, None)


def test_sentinel2_uses_the_heading_the_pixel_fit_moved():
    f = {"source": "s2", "record": {"check": {"fix_heading": 315.0}, "ai2_heading": -47.1}}
    assert publish.pair_heading(f) == (135.0, "fitted")


def test_sentinel2_heading_still_equal_to_the_ai2_box_is_dropped():
    f = {"source": "s2", "record": {"check": {"fix_heading": 312.9}, "ai2_heading": -47.1}}
    assert publish.pair_heading(f) == (None, None)


def test_sentinel2_without_a_fit_has_no_heading():
    assert publish.pair_heading({"source": "s2", "record": {"check": {}, "ai2_heading": -47.0}}) == (None, None)
