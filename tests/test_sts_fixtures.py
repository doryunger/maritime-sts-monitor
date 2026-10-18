import pytest

from maritime_tip_cue.sts import fixtures

pytestmark = pytest.mark.skipif(not fixtures.FIXTURES.exists(), reason="STS fixtures are not present")

TOL = 1e-6
S1_MEASURES = ("length_m", "width_beams", "light_separation", "overlap", "blur_m")


def close(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= TOL


@pytest.fixture(scope="module")
def s2():
    return fixtures.s2_results()


@pytest.fixture(scope="module", params=fixtures.s1_passes() if fixtures.FIXTURES.exists() else [])
def s1(request):
    return request.param, fixtures.s1_results(request.param)


def test_s2_verdicts_and_ratios(s2):
    for item, res in s2:
        exp = item["expected"]
        assert res["verdict"] == exp["verdict"], item["name"]
        assert res["unmeasured_reason"] == exp["unmeasured_reason"], item["name"]
        if "width_ratio" in exp:
            assert close(res["width_ratio"], exp["width_ratio"]), item["name"]
            assert close(res["ratio_ab"], exp["ratio_ab"]), item["name"]


def test_s2_scores(s2):
    pairs = [(i["label_user"], r["verdict"]) for i, r in s2 if i["set"] in fixtures.S2_SETS]
    t = fixtures.tally(pairs)
    assert (t["found"], t["pairs"]) == (17, 17)
    assert t["false"] == 0


def test_s1_verdicts_and_measures(s1):
    name, results = s1
    for obj, res in results:
        exp = obj["expected"]
        assert res["verdict"] == exp["verdict"], (name, obj["number"])
        for key in S1_MEASURES:
            assert close(res[key], exp.get(key)), (name, obj["number"], key)


def test_s1_scores(s1):
    name, results = s1
    t = fixtures.tally([(o["label_user"], r["verdict"]) for o, r in results])
    want = {"hormuz_2026-10-04": (17, 17, 0, 47), "fujairah_2026-10-16": (7, 8, 1, 189)}[name]
    assert (t["found"], t["pairs"], t["false"], t["singles"]) == want
