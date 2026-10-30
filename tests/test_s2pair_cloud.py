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
