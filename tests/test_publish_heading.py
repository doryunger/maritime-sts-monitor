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


def test_sentinel2_uses_the_fitted_heading_for_both_vessels():
    f = {"source": "s2", "record": {"check": {"fix_heading": 315.0}, "ai2_heading": -47.1}}
    assert publish.pair_heading(f) == (135.0, "fitted")


def test_sentinel2_falls_back_to_the_ai2_prior():
    f = {"source": "s2", "record": {"check": {}, "ai2_heading": -47.0}}
    assert publish.pair_heading(f) == (133.0, "ai2")


def test_sentinel1_averages_the_own_boxes():
    f = {"source": "s1", "record": {"ai2_boxes": [{"heading": 84.0}, {"heading": 90.0}]}}
    assert publish.pair_heading(f) == (87.0, "ai2")


def test_no_heading_gives_none():
    assert publish.pair_heading({"source": "s1", "record": {}}) == (None, None)
    assert publish.pair_heading({"source": "s2", "record": {"check": {}}}) == (None, None)
