import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from maritime_tip_cue.ais import match
from maritime_tip_cue.equasis import client
from maritime_tip_cue.equasis import resolve as equasis
from maritime_tip_cue.sts import rules as rules_mod
from tests.pgstore import add_match, add_scene, new_store

RULES = rules_mod.load()["identity"]

SEARCH_HTML = """
<table class="table-striped"><tbody>
<tr class="hidden-sm hidden-xs"><th>9226140</th><td>AVA 6</td><td>29303</td><td>Chemical/Oil Products Tanker</td><td>2002</td><td>Hong Kong, China (HKG)</td></tr>
<tr class="hidden-sm hidden-xs"><th>9000001</th><td>AVA 6</td><td>5000</td><td>General Cargo Ship</td><td>1990</td><td>Panama (PAN)</td></tr>
<tr class="visible-xs"><td>ignored</td></tr>
</tbody></table>
"""

DETAILS_HTML = """
<div class="row"><div class="col"><b>Call Sign</b></div><div class="col">VRXU4</div></div>
<div class="row"><div class="col"><b>MMSI</b></div><div class="col">477226000</div></div>
<div class="row"><div class="col"><b>DWT</b></div><div class="col">44401</div><div class="col"></div><div class="col">2002 survey</div></div>
<div class="row"><div class="col"><b>Status</b></div><div class="col">In Service/Commission</div></div>
"""


class FakeEquasis:
    def __init__(self, candidates, details=None, fail_on=None):
        self.candidates = candidates
        self.detail_data = details or {}
        self.fail_on = fail_on
        self.searches = []

    def search(self, name):
        self.searches.append(name)
        if name == self.fail_on:
            raise client.SessionExpired("logged out")
        return self.candidates.get(name, [])

    def details(self, imo):
        return self.detail_data.get(imo, {})


AVA = {"imo": "9226140", "name": "AVA 6", "gross_tonnage": "29303", "type": "Chemical/Oil Products Tanker", "year_built": "2002", "flag": "Hong Kong, China (HKG)"}
OTHER = {"imo": "9000001", "name": "AVA 6", "gross_tonnage": "5000", "type": "General Cargo Ship", "year_built": "1990", "flag": "Panama (PAN)"}
DETAIL = {"9226140": {"call_sign": "VRXU4", "mmsi": "477226000", "dwt": "44401", "status": "In Service/Commission"}}


@pytest.fixture
def make(pgdb, tmp_path, monkeypatch):
    stores = []

    def build(ships):
        store = new_store(monkeypatch, tmp_path)
        add_scene(store, hulls=[f"h{index}" for index in range(len(ships))])
        for index, (ship_id, name, flag) in enumerate(ships):
            add_match(store, hull_id=f"h{index}", ship_id=ship_id, name=name, flag=flag, ais_length_m=180)
            store.db.execute(
                "insert into review.ais_candidates (run_key, scene_id, site, hull_id, slot, ship_id, type_code, dwt, flag) "
                "values (%s,'s','x',%s,'a',%s,'8',44400,%s)",
                (store.run_key, f"h{index}", ship_id, flag),
            )
        stores.append(store)
        return store

    yield build
    for store in stores:
        store.close()

def test_search_page_is_parsed_into_candidates():
    rows = client.parse_search(BeautifulSoup(SEARCH_HTML, "html.parser"))
    assert [(r["imo"], r["name"], r["gross_tonnage"], r["year_built"], r["flag"]) for r in rows] == [
        ("9226140", "AVA 6", "29303", "2002", "Hong Kong, China (HKG)"),
        ("9000001", "AVA 6", "5000", "1990", "Panama (PAN)"),
    ]


def test_details_page_gives_call_sign_mmsi_dwt_and_status():
    data = client.parse_details(BeautifulSoup(DETAILS_HTML, "html.parser"), "9226140")
    assert (data["call_sign"], data["mmsi"], data["dwt"], data["status"]) == ("VRXU4", "477226000", "44401", "In Service/Commission")
    assert data["dwt_note"] == "2002 survey"


def test_best_candidate_by_flag_type_and_size_wins_and_dwt_confirms_it():
    record = equasis.resolve_vessel(FakeEquasis({"AVA 6": [OTHER, AVA]}, DETAIL), {"name": "AVA 6", "flag": "HK", "type_code": "8", "dwt": 44400}, RULES)
    assert record["imo"] == "9226140" and record["confidence"] == "strong"
    assert record["mmsi"] == "477226000" and record["call_sign"] == "VRXU4" and record["gross_tonnage"] == "29303"
    assert {"flag", "type", "size"} <= set(record["reasons"].split("+"))


def test_unknown_or_ambiguous_names_stay_unresolved():
    ours = {"name": "X", "flag": "HK", "type_code": "8", "dwt": 44400}
    assert equasis.resolve_vessel(FakeEquasis({}), ours, RULES)["confidence"] == "not found"
    weak = {"imo": "1", "name": "X", "gross_tonnage": "1", "type": "Tug", "year_built": "1990", "flag": "Panama (PAN)"}
    record = equasis.resolve_vessel(FakeEquasis({"X": [weak]}), ours, RULES)
    assert record["confidence"] == "unresolved" and "imo" not in record


def test_each_known_vessel_is_looked_up_once_in_order_and_saved_with_its_identity(make):
    store = make([("1", "AVA 6", "HK"), ("2", "OTHER", "PA"), ("1", "AVA 6", "HK")])
    eq = FakeEquasis({"AVA 6": [AVA]}, DETAIL)
    summary = equasis.enrich(store, RULES, eq)
    assert eq.searches == ["AVA 6", "OTHER"] and summary["looked_up"] == 2 and summary["resolved"] == 1
    identity = store.db.execute("select * from review.vessel_identities where ship_id = '1'").fetchone()
    assert (identity["imo"], identity["mmsi"], identity["source"]) == ("9226140", "477226000", "equasis")
    match_row = store.db.execute("select imo, mmsi from review.ais_matches where ship_id = '1' and hull_id = 'h0'").fetchone()
    assert (match_row["imo"], match_row["mmsi"]) == ("9226140", "477226000")
    assert equasis.enrich(store, RULES, FakeEquasis({})) == {"looked_up": 0, "resolved": 0, "unresolved": 0, "skipped": None}


def test_a_ship_is_never_asked_twice_and_the_limit_caps_one_run(make):
    store = make([("1", "A", "HK"), ("2", "B", "HK"), ("3", "C", "HK")])
    eq = FakeEquasis({})
    summary = equasis.enrich(store, dict(RULES, max_lookups_per_run=2), eq)
    assert summary["looked_up"] == 2 and eq.searches == ["A", "B"]
    equasis.enrich(store, RULES, eq)
    assert eq.searches == ["A", "B", "C"]


def test_a_lost_session_stops_the_run_without_hiding_the_vessels_already_done(make):
    store = make([("1", "AVA 6", "HK"), ("2", "BOOM", "HK"), ("3", "LATER", "HK")])
    eq = FakeEquasis({"AVA 6": [AVA]}, DETAIL, fail_on="BOOM")
    summary = equasis.enrich(store, RULES, eq, log=lambda *a, **k: None)
    assert summary["looked_up"] == 1 and "SessionExpired" in summary["skipped"]
    assert eq.searches == ["AVA 6", "BOOM"]
    assert store.db.execute("select count(*) as n from review.vessel_identities").fetchone()["n"] == 1


def test_without_credentials_the_enrichment_is_skipped_quietly(make, monkeypatch):
    store = make([("1", "AVA 6", "HK")])
    monkeypatch.setattr(equasis, "configured", lambda: False)
    summary = equasis.enrich(store, RULES)
    assert summary["skipped"] and summary["looked_up"] == 0


def test_archive_identities_are_used_first_so_equasis_is_not_asked_for_them(make):
    store = make([("1", "AVA 6", "HK"), ("2", "OTHER", "HK")])
    archive = {"1": {"imo": 9226140, "mmsi": 477226000, "flag": "HK", "gross_tonnage": 29303, "equasis_dwt": 44401, "call_sign": "VRXU4",
                     "confidence": "strong", "false_flag": "False", "resolved_utc": "2026-10-13T00:00:00+00:00"}}
    match.refresh_identities(store, lambda ids: {i: archive[i] for i in ids if i in archive})
    eq = FakeEquasis({})
    equasis.enrich(store, RULES, eq)
    assert eq.searches == ["OTHER"]
    stored = store.db.execute("select imo, gross_tonnage, dwt, source from review.vessel_identities where ship_id = '1'").fetchone()
    assert tuple(stored.values()) == ("9226140", "29303", "44401", "archive")


def test_requests_are_spaced_by_the_configured_delay():
    clock = {"now": 100.0}
    slept = []
    eq = client.Equasis(2, path=Path("unused.json"), clock=lambda: clock["now"], sleep=slept.append)
    eq.last = 100.0
    clock["now"] = 100.5
    eq.wait()
    clock["now"] = 105.0
    eq.wait()
    assert slept == [pytest.approx(1.5)]


def test_cached_answers_never_reach_equasis(tmp_path, monkeypatch):
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({"search": {"AVA 6": {"results": [AVA]}}, "details": {"9226140": {"mmsi": "1"}}}), encoding="utf-8")
    eq = client.Equasis(2, path=cache)
    monkeypatch.setattr(eq, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network used")))
    assert eq.search(" ava 6 ")[0]["imo"] == "9226140" and eq.details("9226140")["mmsi"] == "1"


def test_the_rules_carry_the_two_second_delay():
    assert RULES["request_delay_s"] == 2


def stored(name, **extra):
    return {"imo": extra.pop("imo", "9226140"), "name": name, "gross_tonnage": "29303", "type": "Chemical/Oil Products Tanker",
            "year_built": "2002", "flag": "Hong Kong, China (HKG)", **extra}


def test_name_variants_strip_prefixes_and_fall_back_to_shorter_forms():
    assert equasis.name_variants("M/T MARITIME PROSPERITY", RULES) == ["MARITIME PROSPERITY", "MARITIME"]
    assert equasis.name_variants("TIAN SHENG 27", RULES) == ["TIAN SHENG 27", "TIAN SHENG"]
    assert equasis.name_variants("SEA-PROSPER", RULES) == ["SEA PROSPER", "SEA-PROSPER"]
    assert equasis.name_variants("ZHOU", RULES) == ["ZHOU"]
    assert len(equasis.name_variants("MV MARIA & SONS TRADER XX", RULES)) <= RULES["name_variants_max"]


def test_a_name_that_is_not_stored_exactly_is_found_through_a_shorter_search():
    eq = FakeEquasis({"TIAN SHENG": [stored("TIAN SHENG 27")]}, DETAIL)
    record = equasis.resolve_vessel(eq, {"name": "TIAN SHENG 27 EXTRA", "flag": "HK", "type_code": "8", "dwt": 44400}, RULES)
    assert eq.searches[0] == "TIAN SHENG 27 EXTRA" and "TIAN SHENG" in eq.searches
    assert record["imo"] == "9226140" and record["search_name"] == "TIAN SHENG"


def test_shorter_searches_ignore_ships_whose_names_do_not_look_like_ours():
    stranger = stored("MARITIME STAR OF ASIA", imo="9999999")
    eq = FakeEquasis({"MARITIME": [stranger, stored("MARITIME PROSPERITY")]}, DETAIL)
    record = equasis.resolve_vessel(eq, {"name": "M/T MARITIME PROSPERITY", "flag": "HK", "type_code": "8", "dwt": 44400}, RULES)
    assert record["imo"] == "9226140" and record["candidates"] == 1


def test_the_search_stops_at_the_first_variant_that_gives_a_confident_match():
    eq = FakeEquasis({"AVA 6": [AVA]}, DETAIL)
    equasis.resolve_vessel(eq, {"name": "AVA 6", "flag": "HK", "type_code": "8", "dwt": 44400}, RULES)
    assert eq.searches == ["AVA 6"]


def test_every_variant_is_tried_before_giving_up_but_never_more_than_the_cap():
    eq = FakeEquasis({})
    record = equasis.resolve_vessel(eq, {"name": "ALPHA BETA GAMMA DELTA EPSILON", "flag": "HK", "type_code": "8", "dwt": 1}, RULES)
    assert record["confidence"] == "not found" and 1 < len(eq.searches) <= RULES["name_variants_max"]
