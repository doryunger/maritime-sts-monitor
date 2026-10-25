import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from maritime_tip_cue.ais import match
from maritime_tip_cue.equasis import client
from maritime_tip_cue.equasis import resolve as equasis
from maritime_tip_cue.sts import rules as rules_mod
from maritime_tip_cue.watch.state import Store

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


def store_with_matches(tmp_path, ships):
    store = Store(tmp_path / "state.db", tmp_path)
    for index, (ship_id, name, flag) in enumerate(ships):
        store.db.execute(
            "insert into ais_matches (scene_id, site, hull_id, ship_id, name, flag, chosen, slot, ais_length_m) values ('s','x',?,?,?,?,1,'a',180)",
            (f"h{index}", ship_id, name, flag),
        )
        store.db.execute(
            "insert into ais_candidates (scene_id, site, hull_id, slot, ship_id, type_code, dwt, flag) values ('s','x',?,'a',?,'8',44400,?)",
            (f"h{index}", ship_id, flag),
        )
    store.db.commit()
    return store


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


def test_each_known_vessel_is_looked_up_once_in_order_and_saved_with_its_identity(tmp_path):
    store = store_with_matches(tmp_path, [("1", "AVA 6", "HK"), ("2", "OTHER", "PA"), ("1", "AVA 6", "HK")])
    eq = FakeEquasis({"AVA 6": [AVA]}, DETAIL)
    summary = equasis.enrich(store, RULES, eq)
    assert eq.searches == ["AVA 6", "OTHER"] and summary["looked_up"] == 2 and summary["resolved"] == 1
    identity = store.db.execute("select * from vessel_identities where ship_id = '1'").fetchone()
    assert (identity["imo"], identity["mmsi"], identity["source"]) == ("9226140", "477226000", "equasis")
    match_row = store.db.execute("select imo, mmsi from ais_matches where ship_id = '1' and hull_id = 'h0'").fetchone()
    assert (match_row["imo"], match_row["mmsi"]) == ("9226140", "477226000")
    assert equasis.enrich(store, RULES, FakeEquasis({})) == {"looked_up": 0, "resolved": 0, "unresolved": 0, "skipped": None}
    store.close()


def test_a_ship_is_never_asked_twice_and_the_limit_caps_one_run(tmp_path):
    store = store_with_matches(tmp_path, [("1", "A", "HK"), ("2", "B", "HK"), ("3", "C", "HK")])
    eq = FakeEquasis({})
    summary = equasis.enrich(store, dict(RULES, max_lookups_per_run=2), eq)
    assert summary["looked_up"] == 2 and eq.searches == ["A", "B"]
    equasis.enrich(store, RULES, eq)
    assert eq.searches == ["A", "B", "C"]
    store.close()


def test_a_lost_session_stops_the_run_without_hiding_the_vessels_already_done(tmp_path):
    store = store_with_matches(tmp_path, [("1", "AVA 6", "HK"), ("2", "BOOM", "HK"), ("3", "LATER", "HK")])
    eq = FakeEquasis({"AVA 6": [AVA]}, DETAIL, fail_on="BOOM")
    summary = equasis.enrich(store, RULES, eq, log=lambda *a, **k: None)
    assert summary["looked_up"] == 1 and "SessionExpired" in summary["skipped"]
    assert eq.searches == ["AVA 6", "BOOM"]
    assert store.db.execute("select count(*) from vessel_identities").fetchone()[0] == 1
    store.close()


def test_without_credentials_the_enrichment_is_skipped_quietly(tmp_path, monkeypatch):
    store = store_with_matches(tmp_path, [("1", "AVA 6", "HK")])
    monkeypatch.setattr(equasis, "configured", lambda: False)
    summary = equasis.enrich(store, RULES)
    assert summary["skipped"] and summary["looked_up"] == 0
    store.close()


def test_archive_identities_are_used_first_so_equasis_is_not_asked_for_them(tmp_path):
    store = store_with_matches(tmp_path, [("1", "AVA 6", "HK"), ("2", "OTHER", "HK")])
    archive = {"1": {"imo": 9226140, "mmsi": 477226000, "flag": "HK", "gross_tonnage": 29303, "equasis_dwt": 44401, "call_sign": "VRXU4",
                     "confidence": "strong", "false_flag": "False", "resolved_utc": "2026-10-13T00:00:00+00:00"}}
    match.refresh_identities(store, lambda ids: {i: archive[i] for i in ids if i in archive})
    eq = FakeEquasis({})
    equasis.enrich(store, RULES, eq)
    assert eq.searches == ["OTHER"]
    stored = store.db.execute("select imo, gross_tonnage, dwt, source from vessel_identities where ship_id = '1'").fetchone()
    assert tuple(stored) == ("9226140", "29303", "44401", "archive")
    store.close()


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
