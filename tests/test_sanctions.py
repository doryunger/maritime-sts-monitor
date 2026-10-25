import io

import pyarrow.parquet as pq

from maritime_tip_cue import publish_merge as merge
from maritime_tip_cue import refresh_identities as refresher
from maritime_tip_cue import sanctions
from tests.pgstore import add_match, add_scene, new_store
from tests.test_publish_merge import S3, row

LISTED = {"9338905": "EU+OFAC", "9278064": "UK"}


def test_the_list_is_read_by_imo_and_joins_the_lists_a_vessel_is_on(tmp_path):
    path = tmp_path / "list.csv"
    path.write_text("list,list_id,imo,name\nOFAC,1,9338905,KRONOS\nEU,2,9338905,KRONOS\nUK,3,,NO IMO\nUK,4,9278064,SOFOS\n", encoding="utf-8")
    sanctions.table.cache_clear()
    assert sanctions.table(str(path)) == LISTED
    sanctions.table.cache_clear()


def test_imo_numbers_are_matched_however_they_are_written():
    assert sanctions.lists_for("IMO 9338905", LISTED) == "EU+OFAC"
    assert sanctions.lists_for(9278064, LISTED) == "UK"
    assert sanctions.lists_for("1234567", LISTED) == ""
    assert sanctions.lists_for(None, LISTED) == ""


def test_listed_ships_are_flagged_in_the_run_and_others_left_alone(pgdb, tmp_path, monkeypatch):
    store = new_store(monkeypatch, tmp_path)
    add_scene(store, hulls=["h1", "h2", "h3"])
    for ship_id, imo, with_identity in (("1", "9338905", True), ("2", "9999999", True), ("3", "9278064", False)):
        add_match(store, hull_id=f"h{ship_id}", ship_id=ship_id, name=f"SHIP{ship_id}", imo=imo)
        if with_identity:
            store.db.execute("insert into review.vessel_identities (ship_id, imo, source) values (%s,%s, 'equasis')", (ship_id, imo))
    assert sanctions.apply(store, LISTED) == 2
    flags = {r["ship_id"]: r["is_sanctioned"] for r in store.db.execute("select ship_id, is_sanctioned from review.ais_matches")}
    assert flags == {"1": True, "2": False, "3": True}
    assert store.db.execute("select sanctions_lists from review.vessel_identities where ship_id = '1'").fetchone()["sanctions_lists"] == "EU+OFAC"
    assert store.db.execute("select sanctions_lists from review.vessel_identities where ship_id = '2'").fetchone()["sanctions_lists"] is None
    store.close()


def test_published_rows_with_a_listed_imo_get_flagged_and_keep_the_lists():
    rows = [dict(row("a"), vessel_1_imo="9338905", vessel_1_is_sanctioned=False, vessel_2_imo=None, vessel_2_is_sanctioned=False)]
    assert sanctions.apply_rows(rows, LISTED) == 1
    assert rows[0]["vessel_1_is_sanctioned"] is True and rows[0]["vessel_1_sanctions_lists"] == "EU+OFAC"
    assert rows[0]["vessel_2_is_sanctioned"] is False
    assert sanctions.apply_rows(rows, LISTED) == 0


def test_the_refresher_flags_existing_pairs_on_s3_without_an_archive_lookup(monkeypatch):
    s3 = S3()
    data = dict(row("a"), vessel_1_ship_id="1", vessel_1_name="KRONOS", vessel_1_imo="9338905", vessel_1_mmsi="1", vessel_1_flag="RU",
                vessel_1_is_sanctioned=False)
    s3.put_object(Bucket="b", Key="sts/21-10-2026/x/s2.parquet", Body=merge.encode_rows([data]))
    monkeypatch.setattr(refresher, "settings", lambda: {"s3": {"findings_prefix": "sts/"}})
    summary = refresher.refresh(s3, "b", loader=lambda ids: {}, listed=LISTED)
    stored = pq.read_table(io.BytesIO(s3.objects["sts/21-10-2026/x/s2.parquet"])).to_pylist()[0]
    assert summary["sanctioned_slots"] == 1
    assert stored["vessel_1_is_sanctioned"] is True and stored["vessel_1_sanctions_lists"] == "EU+OFAC"


def test_a_republish_does_not_lose_a_sanctions_flag_for_the_same_ship():
    s3 = S3()
    key = "sts/21-10-2026/fujairah_outer/s2.parquet"
    flagged = dict(row("a"), vessel_1_ship_id="1", vessel_1_name="KRONOS", vessel_1_imo="9338905", vessel_1_is_sanctioned=True,
                   vessel_1_sanctions_lists="EU+OFAC")
    merge.merge_group(s3, "b", "sts/", "21-10-2026", key, [flagged], "J1", {"dedupe_time_s": 120, "dedupe_length_share": 0.5})
    later = dict(row("a"), vessel_1_ship_id="1", vessel_1_name="KRONOS", vessel_1_imo=None, vessel_1_is_sanctioned=False)
    merge.merge_group(s3, "b", "sts/", "21-10-2026", key, [later], "J1", {"dedupe_time_s": 120, "dedupe_length_share": 0.5})
    stored = pq.read_table(io.BytesIO(s3.objects[key])).to_pylist()[0]
    assert stored["vessel_1_is_sanctioned"] is True and stored["vessel_1_imo"] == "9338905"
