import json

from maritime_tip_cue import identity_seed
from maritime_tip_cue.ais import match

HEADER = "ship_id,name,flag,type_code,length_m,dwt,imo,confidence,reasons,equasis_name,equasis_flag,false_flag,equasis_type,gross_tonnage,equasis_dwt,year_built,call_sign,mmsi,status,sanctioned,candidates,resolved_utc"
ROWS = [
    "100,AVA 6,HK,8,180,44401,9226140,strong,flag+type+size+dwt,AVA 6,Hong Kong,False,Chemical Tanker,29303,44401,2002,VRXU4,477226000,In Service,OFAC,1,2026-10-13T02:47:03+00:00",
    "200,GHOST,PA,8,150,0,,unresolved,,,,False,,,,,,,,,0,2026-10-13T02:47:03+00:00",
    "300,KNOWN,GR,8,200,60000,9111111,medium,flag+type,KNOWN,Greece,True,Oil Tanker,30000,60000,2005,SXYZ,240123000,In Service,,2,2026-10-14T00:00:00+00:00",
]


def csv_file(tmp_path):
    path = tmp_path / "vessel_ids.csv"
    path.write_text(chr(10).join([HEADER, *ROWS]) + chr(10), encoding="utf-8")
    return path


def test_only_resolved_vessels_are_read_and_typed(tmp_path):
    rows = identity_seed.read_rows(csv_file(tmp_path))
    assert [r["ship_id"] for r in rows] == ["100", "300"]
    first = rows[0]
    assert first["imo"] == "9226140" and first["dwt"] == "44401" and first["vessel_type"] == "Chemical Tanker"
    assert first["false_flag"] is False and rows[1]["false_flag"] is True
    assert first["candidates"] == 1 and first["source"] == "aggregator" and first["resolved_utc"].year == 2026


def test_seeding_adds_new_vessels_and_never_overwrites_a_known_one(pgdb, tmp_path):
    pgdb.execute("insert into review.vessel_identities (ship_id, imo, source) values ('300', '9999999', 'equasis')")
    result = identity_seed.seed(pgdb, identity_seed.read_rows(csv_file(tmp_path)))
    assert result == {"resolved_in_file": 2, "inserted": 1, "already_known": 1}
    assert pgdb.execute("select imo, source from review.vessel_identities where ship_id = '300'").fetchone() == ("9999999", "equasis")
    assert pgdb.execute("select imo, mmsi, source from review.vessel_identities where ship_id = '100'").fetchone() == ("9226140", "477226000", "aggregator")
    assert identity_seed.seed(pgdb, identity_seed.read_rows(csv_file(tmp_path)))["inserted"] == 0


def test_the_identity_lookup_reads_this_repos_table_in_the_old_archive_shape(pgdb, tmp_path):
    identity_seed.seed(pgdb, identity_seed.read_rows(csv_file(tmp_path)))
    pgdb.execute("insert into review.vessel_identities (ship_id, source) values ('400', 'equasis')")
    pgdb.commit()
    found = match.load_identities(["100", "400", "nope"])
    assert set(found) == {"100"}
    assert found["100"]["imo"] == "9226140" and found["100"]["equasis_dwt"] == "44401" and found["100"]["equasis_type"] == "Chemical Tanker"
    record = match.archive_identity(found["100"])
    assert record["imo"] == "9226140" and record["dwt"] == "44401" and record["vessel_type"] == "Chemical Tanker"
    assert match.load_identities([]) == {}


def test_cache_merge_keeps_what_is_there_and_adds_what_is_new(tmp_path):
    source, target = tmp_path / "old.json", tmp_path / "data" / "cache.json"
    target.parent.mkdir()
    target.write_text(json.dumps({"search": {"A": {"at": "new"}}, "details": {}}), encoding="utf-8")
    source.write_text(json.dumps({"search": {"A": {"at": "old"}, "B": {"at": "old"}}, "details": {"111": {"x": 1}}}), encoding="utf-8")
    assert identity_seed.merge_cache(source, target) == {"search": 1, "details": 1, "total_search": 2, "total_details": 1}
    merged = json.loads(target.read_text(encoding="utf-8"))
    assert merged["search"]["A"] == {"at": "new"} and merged["search"]["B"] == {"at": "old"} and merged["details"]["111"] == {"x": 1}
