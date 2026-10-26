import io
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from maritime_tip_cue import publish, publish_merge, publish_sanctioned, sanctions
from maritime_tip_cue.ais import match
from maritime_tip_cue.equasis import resolve as equasis
from maritime_tip_cue.sts import rules as rules_mod
from tests.pgstore import add_match, add_scene, hull, new_store
from tests.test_publish_merge import S3

CSV = (
    "list,list_id,imo,name,other_names,programs\n"
    "OFAC,1,9338905,KRONOS,Old Kronos|Kronos II,RUSSIA-EO14024\n"
    "UK,2,9338905,KRONOS,,RUSSIA\n"
    "OFAC,3,9278064,ABC,,IRAN-EO13846] [SDGT\n"
    "UK,4,,NO IMO,,\n"
)


@pytest.fixture
def listed(tmp_path, monkeypatch):
    path = tmp_path / "list.csv"
    path.write_text(CSV, encoding="utf-8")
    monkeypatch.setattr(sanctions, "list_path", lambda: path)
    sanctions.entries.cache_clear()
    sanctions.table.cache_clear()
    yield
    sanctions.entries.cache_clear()
    sanctions.table.cache_clear()


def test_listed_names_cover_aliases_and_ignore_short_or_unlisted_ones(listed):
    assert sanctions.listed_name("Kronos", 4)
    assert sanctions.listed_name("OLD-KRONOS", 4)
    assert sanctions.listed_name("kronos ii", 4)
    assert not sanctions.listed_name("ABC", 4)
    assert sanctions.listed_name("ABC", 3)
    assert not sanctions.listed_name("OTHER SHIP", 4)
    assert not sanctions.listed_name("NO IMO", 4)
    assert not sanctions.listed_name(None, 4)


def test_programs_of_a_listed_ship_are_joined_across_its_lists(listed):
    assert sanctions.programs_for("IMO 9338905") == "RUSSIA; RUSSIA-EO14024"
    assert sanctions.programs_for("9278064") == "IRAN-EO13846; SDGT"
    assert sanctions.programs_for("1234567") == ""


def test_a_single_row_keeps_what_the_stored_row_knew_about_the_same_ship():
    old = {"vessel_ship_id": "7", "vessel_imo": "9338905", "vessel_is_sanctioned": True, "vessel_sanctions_lists": "EU+OFAC"}
    new = {"vessel_ship_id": "7", "vessel_imo": None, "vessel_is_sanctioned": False, "vessel_sanctions_lists": None}
    kept = publish_merge.keep_identity(new, old)
    assert (kept["vessel_imo"], kept["vessel_is_sanctioned"], kept["vessel_sanctions_lists"]) == ("9338905", True, "EU+OFAC")
    other = publish_merge.keep_identity({"vessel_ship_id": "8", "vessel_imo": None, "vessel_is_sanctioned": False}, old)
    assert other["vessel_imo"] is None and other["vessel_is_sanctioned"] is False


def finding(hull_id="h1", site="fujairah_outer", seconds=0, party=None, source="s2", record=None):
    party = party if party is not None else {"slot": "a", "name": "KRONOS", "imo": "9338905", "is_sanctioned": True, "sanctions_lists": "OFAC+UK", "sanctions_programs": "RUSSIA", "ship_id": "7", "dist_a_m": 35.0, "ais_heading_deg": 95.0, "ais_speed_kn": 0.1}
    return {
        "run": 3, "job_id": "J1", "root": Path("."), "scene_id": "S2A_X", "site": site, "hull_id": hull_id, "source": source, "verdict": "single",
        "capture_utc": datetime(2026, 10, 21, 6, 52, seconds, tzinfo=UTC).isoformat().replace("+00:00", "Z"), "lat": 25.0, "lon": 56.4,
        "run_verdict": "single", "qa": "unreviewed", "qa_note": None, "record": record or {"check": {"hull_a_length_m": 180.0, "fix_heading": 40.0}, "ai2_heading": 10.0}, "folder": "f",
        "parties": {"a": party}, "ais_coverage_ships": 120, "ais_covered": True, "bbox": [56.0, 24.0, 57.0, 26.0], "length_m": 180.0,
    }


@pytest.fixture
def png(monkeypatch):
    def fake(f, base, rules, root, single=False):
        assert single
        path = root / "captures" / publish.ddmmyyyy(f["capture_utc"]) / f["site"] / f["source"] / f"{base}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"png")
        return {"png": path}

    monkeypatch.setattr(publish, "write_captures", fake)


def test_a_sanctioned_single_is_written_with_its_vessel_capture_and_flag(png, tmp_path):
    kept, tables = publish_sanctioned.assemble([finding()], rules_mod.load(), tmp_path)
    ((date, site, source), (path, _)), = tables.items()
    assert (date, site, source) == ("21-10-2026", "fujairah_outer", "s2")
    assert path == tmp_path / "sanctioned" / "21-10-2026" / "fujairah_outer" / "s2.parquet"
    row, = pq.read_table(path).to_pylist()
    assert row["verdict"] == "single" and row["vessel_is_sanctioned"] is True
    assert (row["vessel_imo"], row["vessel_name"], row["vessel_sanctions_lists"], row["vessel_sanctions_programs"]) == ("9338905", "KRONOS", "OFAC+UK", "RUSSIA")
    assert row["capture_png"] == "captures/21-10-2026/fujairah_outer/s2/s2_20261021T065200_h1_KRONOS.png"
    assert (row["vessel_lat"], row["vessel_lon"]) == (25.0, 56.4)
    assert (row["heading_deg"], row["heading_source"]) == (40.0, "fitted")
    assert (row["vessel_ais_heading_deg"], row["vessel_ais_speed_kn"]) == (95.0, 0.1)
    assert b"geo" in pq.read_table(path).schema.metadata
    assert not any(name.startswith("vessel_1_") or name.startswith("vessel_2_") for name in row)


def test_upload_goes_under_the_sanctioned_prefix_and_a_rerun_of_the_job_does_not_duplicate(png, tmp_path):
    s3 = S3()
    rules = rules_mod.load()
    for attempt in (1, 2):
        result = publish_sanctioned.publish_findings(s3, "b", [finding()], rules, tmp_path / str(attempt), "J1")
        assert result["published"] == 1 and result["files"] == ["sanctioned/21-10-2026/fujairah_outer/s2.parquet"]
    rows = pq.read_table(io.BytesIO(s3.objects["sanctioned/21-10-2026/fujairah_outer/s2.parquet"])).to_pylist()
    assert [r["finding_id"] for r in rows] == ["s2_20261021T065200_h1"]
    assert s3.uploads and all(key.startswith("captures/21-10-2026/fujairah_outer/s2/") for key in s3.uploads)
    assert not any(key.startswith("sts/") for key in s3.objects)


def test_the_same_vessel_seen_by_two_hot_spots_is_one_finding(png, tmp_path):
    s3 = S3()
    rules = rules_mod.load()
    publish_sanctioned.publish_findings(s3, "b", [finding("a", "fujairah_outer")], rules, tmp_path / "1", "J1")
    result = publish_sanctioned.publish_findings(s3, "b", [finding("b", "off_sohar", seconds=5)], rules, tmp_path / "2", "J2")
    assert result["published"] == 0 and result["duplicates_skipped"] == 1


def add_single_scene(store, hulls):
    return add_scene(store, hulls=[hull(h, verdict=v, length_m=200.0) if isinstance(h, str) else hull(h[0], verdict=v, lat=h[1], lon=h[2], length_m=200.0) for h, v in hulls])


def test_singles_are_looked_up_only_when_listed_or_flagged_and_pairs_always(pgdb, tmp_path, monkeypatch, listed):
    store = new_store(monkeypatch, tmp_path)
    add_single_scene(store, [("p", "pair"), ("s1", "single"), ("s2", "single"), ("s3", "single")])
    for hull_id, ship_id, name in (("p", "1", "PLAIN PAIRSHIP"), ("s1", "2", "KRONOS"), ("s2", "3", "RANDOM"), ("s3", "4", "FLAGGED ONE")):
        add_match(store, hull_id=hull_id, ship_id=ship_id, name=name)
    store.db.execute("update review.ais_matches set is_sanctioned = true where ship_id = '4'")
    assert [v["name"] for v in equasis.pending(store)] == ["PLAIN PAIRSHIP", "KRONOS", "FLAGGED ONE"]
    store.close()


def report(ship_id, name, lat=25.0, lon=56.0, **override):
    seen = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
    return {
        "ship_id": ship_id, "name": name, "is_sat": False, "type": None, "type_code": 80, "gt_type_code": None, "flag": "PA", "lat": lat, "lon": lon,
        "speed_kn": 0.0, "course": 0.0, "heading": 90, "rot": None, "seen_utc": seen, "elapsed_min": 0, "destination": None, "length_m": 200.0,
        "width_m": 30.0, "dwt": None, "status": None, "state": None, "captured_utc": seen, "tile_utc": seen, "is_sanctioned": True,
        "sanctions_imo": 9338905,
    } | override


def metres(north):
    return north / 111195.0


def patch_archive(monkeypatch, reports):
    monkeypatch.setattr(match, "fetch_reports", lambda capture, bbox, rule: reports)
    monkeypatch.setattr(match, "site_bbox", lambda store, site: [55.0, 24.0, 57.0, 26.0])
    monkeypatch.setattr(match, "load_identities", lambda ship_ids: {})


def test_the_singles_pass_cannot_take_a_ship_a_pair_already_has(pgdb, tmp_path, monkeypatch):
    store = new_store(monkeypatch, tmp_path)
    add_single_scene(store, [("p", "pair"), ("s", "single")])
    patch_archive(monkeypatch, [report("1", "ONE")])
    rules = rules_mod.load()
    match.run_match(store, match.select_hulls(store, match.PAIR_VERDICTS, False, False), rules, lambda *a: None)
    match.match_singles(store, rules, lambda *a: None)
    chosen = {r["hull_id"] for r in store.db.execute("select hull_id from review.ais_matches where chosen")}
    assert chosen == {"p"}
    match.run_match(store, match.select_hulls(store, ("single",), False, False), rules, lambda *a: None)
    chosen = {r["hull_id"] for r in store.db.execute("select hull_id from review.ais_matches where chosen")}
    assert chosen == {"p", "s"}
    store.close()


def test_a_matched_single_takes_the_archive_imo_and_flag_without_an_identity_row(pgdb, tmp_path, monkeypatch):
    store = new_store(monkeypatch, tmp_path)
    add_single_scene(store, [("s", "single")])
    patch_archive(monkeypatch, [report("1", "ONE")])
    match.match_singles(store, rules_mod.load(), lambda *a: None)
    row = store.db.execute("select imo, is_sanctioned, score from review.ais_matches where hull_id = 's'").fetchone()
    assert (row["imo"], row["is_sanctioned"], row["score"]) == ("9338905", True, 0.97)
    store.close()


def test_only_singles_with_a_sanctioned_party_are_collected_and_pairs_are_left_out(pgdb, tmp_path, monkeypatch, listed):
    store = new_store(monkeypatch, tmp_path)
    add_single_scene(store, [("flagged", "single"), ("by_imo", "single"), ("plain", "single"), ("unmatched", "single"), ("pair", "pair")])
    add_match(store, hull_id="flagged", ship_id="1", name="KRONOS", imo="9338905")
    store.db.execute("update review.ais_matches set is_sanctioned = true where ship_id = '1'")
    add_match(store, hull_id="by_imo", ship_id="2", name="OLD NAME", imo="9338905")
    add_match(store, hull_id="plain", ship_id="3", name="PLAIN", imo="1234567")
    add_match(store, hull_id="pair", ship_id="4", name="PAIR", imo="9338905")
    run_no = store.run_no
    store.db.execute(
        "insert into review.ais_candidates (run_key, scene_id, site, hull_id, slot, ship_id, ais_heading) values (%s, 's', 'x', 'flagged', 'a', '1', 123.0)",
        (store.run_key,),
    )
    found = publish.collect_at(store.path, run_no, "J", None, verdicts=("single",))
    assert sorted(f["hull_id"] for f in found) == ["by_imo", "flagged"]
    party = found[0]["parties"]["a"]
    assert [f["parties"]["a"]["ais_heading_deg"] for f in found] == [None, 123.0] or [f["parties"]["a"]["ais_heading_deg"] for f in found] == [123.0, None]
    assert party["is_sanctioned"] and party["sanctions_lists"] == "OFAC+UK" and party["sanctions_programs"] == "RUSSIA; RUSSIA-EO14024"
    both = publish.collect_at(store.path, run_no, "J", None, verdicts=("pair", "single"))
    assert sorted((f["hull_id"], f["verdict"]) for f in both) == [("by_imo", "single"), ("flagged", "single"), ("pair", "pair")]
    store.close()


def test_a_failing_sanctioned_publish_is_recorded_and_does_not_fail_the_pair_publish(monkeypatch, tmp_path):
    monkeypatch.setattr(publish, "collect_at", lambda *a, **k: [])
    monkeypatch.setattr(publish_sanctioned, "publish_findings", lambda *a, **k: (_ for _ in ()).throw(KeyError("sanctioned_prefix")))
    result = publish.publish_run(None, "b", tmp_path, 1, "J", rules_mod.load())
    assert result["pairs"] == 0 and result["sanctioned"] == {"error": "KeyError: 'sanctioned_prefix'"}


def test_the_summary_says_how_many_listed_imos_the_worker_could_see(png, tmp_path, listed):
    result = publish_sanctioned.publish_findings(S3(), "b", [finding()], rules_mod.load(), tmp_path, "J1")
    assert result["listed_imos"] == 2


def candidate_reasons(store):
    return {
        (r["hull_id"], r["ship_id"]): (r["decision"], r["reasons"], r["score"])
        for r in store.db.execute("select hull_id, ship_id, decision, reasons, score from review.ais_candidates")
    }


def test_a_single_is_matched_only_with_length_and_stationary_evidence_inside_150_metres(pgdb, tmp_path, monkeypatch):
    store = new_store(monkeypatch, tmp_path)
    spots = [("good", 25.0), ("short", 25.1), ("moving", 25.2), ("far", 25.3), ("near", 25.4)]
    add_single_scene(store, [((h, lat, 56.0), "single") for h, lat in spots])
    patch_archive(
        monkeypatch,
        [
            report("good", "GOOD", 25.0, 56.0),
            report("short", "SHORT", 25.1 + metres(10), 56.0, length_m=100.0),
            report("moving", "MOVING", 25.2 + metres(10), 56.0, speed_kn=8.0),
            report("far", "FAR", 25.3 + metres(200), 56.0),
            report("near_a", "NEAR A", 25.4 + metres(100), 56.0),
            report("near_b", "NEAR B", 25.4 - metres(40), 56.0),
        ],
    )
    match.match_singles(store, rules_mod.load(), lambda *a: None)
    chosen = {r["hull_id"]: r["ship_id"] for r in store.db.execute("select hull_id, ship_id from review.ais_matches where chosen")}
    assert chosen == {"good": "good", "near": "near_b"}
    seen = candidate_reasons(store)
    assert seen[("short", "short")][1] == "score" and seen[("short", "short")][2] == 0.31
    assert seen[("moving", "moving")][1] == "score" and seen[("moving", "moving")][2] == 0.66
    assert seen[("far", "far")][1] == "distance,score" or seen[("far", "far")][1] == "distance"
    assert seen[("near", "near_a")][0] == "eligible_not_selected"
    store.close()


def hull_with(candidates):
    return {"slots": [{"slot": "a"}], "cands": [{"c": {"ship_id": ship, "age": age}, "per_slot": [{"reasons": [], "dist": dist, "score": score}]} for ship, dist, age, score in candidates]}


def test_the_assignment_prefers_the_higher_score_then_the_nearer_ship_and_pairs_still_go_by_distance():
    scored = match.assign({"h": hull_with([("near", 20.0, 5.0, 0.97), ("strong", 140.0, 5.0, 1.28)])})
    assert scored == {("h", "strong"): 0}
    tied = match.assign({"h": hull_with([("far", 100.0, 5.0, 0.97), ("near", 40.0, 9.0, 0.97)])})
    assert tied == {("h", "near"): 0}
    plain = match.assign({"h": hull_with([("far", 100.0, 5.0, None), ("near", 40.0, 9.0, None)])})
    assert plain == {("h", "near"): 0}


def test_an_s1_single_takes_the_pixel_rect_axis_as_its_heading_and_none_when_there_is_no_fit(png, tmp_path):
    kept, tables = publish_sanctioned.assemble([finding(source="s1", record={"rect_angle_deg": 30.0})], rules_mod.load(), tmp_path)
    row, = pq.read_table(next(iter(tables.values()))[0]).to_pylist()
    assert (row["heading_deg"], row["heading_source"]) == (60.0, "pixel_rect")
    kept, tables = publish_sanctioned.assemble([finding(record={"check": {"fix_heading": 10.0}, "ai2_heading": 10.0})], rules_mod.load(), tmp_path / "2")
    row, = pq.read_table(next(iter(tables.values()))[0]).to_pylist()
    assert (row["heading_deg"], row["heading_source"]) == (None, None)


def test_collecting_without_enrichment_never_reaches_equasis_or_the_identity_steps(pgdb, tmp_path, monkeypatch, listed):
    store = new_store(monkeypatch, tmp_path)
    add_single_scene(store, [("flagged", "single")])
    add_match(store, hull_id="flagged", ship_id="1", name="KRONOS", imo="9338905")
    store.db.execute("update review.ais_matches set is_sanctioned = true where ship_id = '1'")

    def refused(*a, **k):
        raise AssertionError("enrichment must not run")

    monkeypatch.setattr(equasis, "enrich", refused)
    monkeypatch.setattr(match, "refresh_identities", refused)
    monkeypatch.setattr(sanctions, "apply", refused)
    found = publish.collect_at(store.path, store.run_no, "J", None, verdicts=("single",), enrich=False)
    assert [f["hull_id"] for f in found] == ["flagged"]
    store.close()
