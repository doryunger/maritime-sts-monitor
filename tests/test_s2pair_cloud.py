import pytest

from maritime_tip_cue.sts import s2pair

CFG = {"cloud_beside_single_min_share": 0.02}


@pytest.mark.parametrize(
    "share,expected",
    [(None, False), (0.0, False), (0.019, False), (0.02, True), (0.0420, True), (0.399, True)],
)
def test_cloud_beside_the_hull_stops_the_hull_b_search(share, expected):
    assert s2pair.cloudy_beside({"cloud_share": share}, CFG) is expected


def test_an_unset_limit_never_stops_the_search():
    assert s2pair.cloudy_beside({"cloud_share": 0.9}, {"cloud_beside_single_min_share": None}) is False


def test_a_hull_that_cannot_be_measured_is_called_single_with_its_reason_kept():
    result = s2pair.run(None, {}, {"pair_split": {}}, [])
    assert result["verdict"] == "single" and result["unmeasured_reason"] == "no water peak in the window"


def test_a_moving_hull_is_called_moving_before_any_pair_check(monkeypatch):
    moving = s2pair.check_hull({}, {"speed_kn": 19.7}, {"stopped_speed_kn": 2})
    assert moving["verdict"] == "moving" and "19.7 kn" in moving["unmeasured_reason"]
    monkeypatch.setattr(s2pair.s2window, "to_xy", lambda win, lon, lat: (0, 0))
    monkeypatch.setattr(s2pair, "check", lambda win, box, cfg: {"verdict": "checked"})
    hull = {"lon": 0, "lat": 0, "length_m": 100, "width_m": 20, "heading": 0}
    assert s2pair.check_hull({}, hull | {"speed_kn": 0.3}, {"stopped_speed_kn": 2})["verdict"] == "checked"
    assert s2pair.check_hull({}, hull | {"speed_kn": None}, {"stopped_speed_kn": 2})["verdict"] == "checked"
