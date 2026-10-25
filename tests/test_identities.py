import io

import pyarrow.parquet as pq
import pytest

from maritime_tip_cue import publish_merge as merge
from maritime_tip_cue import refresh_identities as refresher
from maritime_tip_cue.ais import match
from tests.pgstore import add_match, add_scene, new_store
from tests.test_publish_merge import RULE, S3, row


@pytest.fixture
def make(pgdb, tmp_path, monkeypatch):
    stores = []

    def build(matches):
        store = new_store(monkeypatch, tmp_path)
        add_scene(store, hulls=["h"])
        for ship_id, imo, mmsi, flag, chosen in matches:
            add_match(store, hull_id="h", ship_id=ship_id, imo=imo, mmsi=mmsi, flag=flag, chosen=bool(chosen))
        stores.append(store)
        return store

    yield build
    for store in stores:
        store.close()

def test_refresh_fills_missing_identity_from_the_archive_and_never_overwrites(make):
    store = make([("1", None, None, None, 1), ("2", "9999999", None, "PA", 1), ("3", None, None, None, 0)])
    archive = {"1": {"imo": 9226140, "mmsi": 477226000, "flag": "HK"}, "2": {"imo": 1111111, "mmsi": 538003857, "flag": "MH"}, "3": {"imo": 5}}
    changed = match.refresh_identities(store, lambda ids: {i: archive[i] for i in ids})
    values = {r["ship_id"]: (r["imo"], r["mmsi"], r["flag"]) for r in store.db.execute("select * from review.ais_matches")}
    assert changed == 2
    assert values["1"] == ("9226140", "477226000", "HK")
    assert values["2"] == ("9999999", "538003857", "PA")
    assert values["3"] == (None, None, None)


def test_refresh_survives_an_unresolved_ship(make):
    store = make([("1", None, None, None, 1)])
    assert match.refresh_identities(store, lambda ids: {}) == 0


def published(s3, **extra):
    data = row("a")
    data.update(vessel_1_ship_id="1", vessel_1_name="SHIP", vessel_1_imo=None, vessel_1_mmsi=None, vessel_1_flag=None, **extra)
    s3.put_object(Bucket="b", Key="sts/21-10-2026/x/s2.parquet", Body=merge.encode_rows([data]))


def test_published_rows_get_imo_and_mmsi_when_the_archive_resolves_the_ship(monkeypatch):
    s3 = S3()
    published(s3)
    monkeypatch.setattr(refresher, "settings", lambda: {"s3": {"findings_prefix": "sts/"}})
    summary = refresher.refresh(s3, "b", loader=lambda ids: {"1": {"imo": 9226140, "mmsi": 477226000, "flag": "HK"}})
    stored = pq.read_table(io.BytesIO(s3.objects["sts/21-10-2026/x/s2.parquet"])).to_pylist()[0]
    assert summary["slots_filled"] == 3
    assert (stored["vessel_1_imo"], stored["vessel_1_mmsi"], stored["vessel_1_flag"]) == ("9226140", "477226000", "HK")


def test_a_second_refresh_with_nothing_new_leaves_the_file_alone(monkeypatch):
    s3 = S3()
    published(s3)
    monkeypatch.setattr(refresher, "settings", lambda: {"s3": {"findings_prefix": "sts/"}})
    refresher.refresh(s3, "b", loader=lambda ids: {})
    before = s3.objects["sts/21-10-2026/x/s2.parquet"]
    refresher.refresh(s3, "b", loader=lambda ids: {})
    assert s3.objects["sts/21-10-2026/x/s2.parquet"] == before


def test_republishing_a_job_never_loses_an_identity_that_was_already_found():
    s3 = S3()
    resolved = dict(row("a"), vessel_1_ship_id="1", vessel_1_name="SHIP", vessel_1_imo="9226140", vessel_1_mmsi="477226000", vessel_1_flag="HK")
    key = "sts/21-10-2026/fujairah_outer/s2.parquet"
    merge.merge_group(s3, "b", "sts/", "21-10-2026", key, [resolved], "J1", RULE)
    unresolved = dict(row("a"), vessel_1_ship_id="1", vessel_1_name="SHIP", vessel_1_imo=None, vessel_1_mmsi=None, vessel_1_flag=None)
    merge.merge_group(s3, "b", "sts/", "21-10-2026", key, [unresolved], "J1", RULE)
    stored = pq.read_table(io.BytesIO(s3.objects[key])).to_pylist()[0]
    assert (stored["vessel_1_imo"], stored["vessel_1_mmsi"]) == ("9226140", "477226000")


def test_a_different_ship_on_the_same_finding_does_not_inherit_the_old_identity():
    s3 = S3()
    key = "sts/21-10-2026/fujairah_outer/s2.parquet"
    old = dict(row("a"), vessel_1_ship_id="1", vessel_1_name="OLD", vessel_1_imo="111", vessel_1_mmsi="222", vessel_1_flag="HK")
    merge.merge_group(s3, "b", "sts/", "21-10-2026", key, [old], "J1", RULE)
    new = dict(row("a"), vessel_1_ship_id="7", vessel_1_name="OTHER", vessel_1_imo=None, vessel_1_mmsi=None, vessel_1_flag=None)
    merge.merge_group(s3, "b", "sts/", "21-10-2026", key, [new], "J1", RULE)
    stored = pq.read_table(io.BytesIO(s3.objects[key])).to_pylist()[0]
    assert stored["vessel_1_imo"] is None and stored["vessel_1_ship_id"] == "7"
