from pathlib import Path

import pytest

from maritime_tip_cue.sts import fixtures
from maritime_tip_cue.sts import rules as rules_mod

FROZEN = rules_mod.load(Path(__file__).with_name("sts_graph_frozen.json"))

pytestmark = pytest.mark.skipif(not fixtures.FIXTURES.exists(), reason="STS fixtures are not present")

TOL = 1e-6
S1_MEASURES = ("length_m", "width_beams", "light_separation", "overlap", "blur_m")


def close(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= TOL


PASSES = fixtures.s1_passes() if fixtures.FIXTURES.exists() else []


@pytest.fixture(scope="module")
def s2():
    return fixtures.s2_results(rules=FROZEN)


@pytest.fixture(scope="module")
def s2_live():
    return fixtures.s2_results()


@pytest.fixture(scope="module", params=PASSES)
def s1(request):
    return request.param, fixtures.s1_results(request.param, rules=FROZEN)


@pytest.fixture(scope="module", params=PASSES)
def s1_live(request):
    return request.param, fixtures.s1_results(request.param)


def counts(pairs: list[tuple[str | None, str]]) -> dict:
    out = {}
    for label, verdict in pairs:
        out[(label, verdict)] = out.get((label, verdict), 0) + 1
    return out


def test_s2_verdicts_and_ratios(s2):
    for item, res in s2:
        exp = item["expected"]
        assert res["verdict"] == exp["verdict"], item["name"]
        assert res["unmeasured_reason"] == exp["unmeasured_reason"], item["name"]
        if "width_ratio" in exp:
            assert close(res["width_ratio"], exp["width_ratio"]), item["name"]
            assert close(res["ratio_ab"], exp["ratio_ab"]), item["name"]


def test_s2_scores(s2_live):
    c = counts([(i["label_user"], r["verdict"]) for i, r in s2_live if i["set"] in fixtures.S2_SETS])
    assert c.get(("pair", "pair"), 0) == 17
    assert sum(n for (label, verdict), n in c.items() if verdict == "possible_pair") == 0
    assert c.get(("single", "pair"), 0) == 0


def test_s1_verdicts_and_measures(s1):
    name, results = s1
    for obj, res in results:
        exp = obj["expected"]
        assert res["verdict"] == exp["verdict"], (name, obj["number"])
        for key in S1_MEASURES:
            assert close(res[key], exp.get(key)), (name, obj["number"], key)


def test_s1_scores(s1_live):
    name, results = s1_live
    c = counts([(o["label_user"], r["verdict"]) for o, r in results])
    got = tuple(c.get(k, 0) for k in (("pair", "pair"), ("pair", "possible_pair"), ("pair", "single"), ("single", "pair"), ("single", "possible_pair")))
    want = {"hormuz_2026-10-04": (15, 2, 0, 0, 0), "fujairah_2026-10-16": (7, 0, 1, 0, 1)}[name]
    assert got == want
