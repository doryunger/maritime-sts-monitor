import json

import pytest

from maritime_tip_cue import hotspot_tests, worker
from maritime_tip_cue.config import settings


class Client:
    def __init__(self):
        self.files = {}
        self.objects = {}

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        self.files[key] = ExtraArgs["Metadata"]["sha256"]

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = json.loads(Body)


def test_shipped_plan_is_valid_and_isolated():
    areas = hotspot_tests.load()
    assert areas and all(a["name"].startswith("test_") and a["pipeline"] == "hotspot_test" for a in areas)


def test_shipped_areas_look_back_ten_days():
    assert {hotspot_tests.lookback_days(a["name"]) for a in hotspot_tests.load()} == {10}


def test_prefixes_do_not_overlap_regular_output():
    cfg = settings()
    regular = [cfg["s3"]["findings_prefix"], cfg["s3"]["sanctioned_prefix"], cfg["s3"]["captures_prefix"]]
    regular += [cfg["catalogue"][key] for key in ("jobs_prefix", "results_prefix", "claims_prefix", "artifacts_prefix", "reviews_prefix")]
    mine = [hotspot_tests.findings_prefix(), hotspot_tests.artifacts_prefix(), hotspot_tests.reviews_prefix()]
    assert all(m.startswith("hotspot-tests/") for m in mine)
    assert not any(m.startswith(r) or r.startswith(m) for m in mine for r in regular)


def test_worker_uploads_test_attempts_under_the_test_prefixes(tmp_path):
    out = tmp_path / "run"
    out.mkdir()
    (out / "review.png").write_bytes(b"png")
    manifest = {"job_id": "J", "site": {"name": "test_x", "pipeline": "hotspot_test"}}
    client = Client()
    worker.upload_review(client, "b", manifest, out, 1, "A")
    artifact = tmp_path / "detections.parquet"
    artifact.write_bytes(b"x")
    worker.upload_artifacts(client, "b", manifest, [artifact], "A")
    keys = set(client.files) | set(client.objects)
    assert keys and all(key.startswith("hotspot-tests/") for key in keys)


@pytest.mark.parametrize("bad", [{"name": "mumbai"}, {"pipeline": "live"}, {"bbox": [1, 1, 0, 2]}, {"lookback_days": 0}, {"lookback_days": None}])
def test_plan_rejects_invalid_areas(tmp_path, monkeypatch, bad):
    area = {
        "name": "test_a", "label": "A", "pipeline": "hotspot_test", "area_version": 1,
        "bbox": [0, 0, 1, 1], "min_shore_distance_m": 2000, "sources": ["s1"], "lookback_days": 10,
    } | bad
    path = tmp_path / "areas.json"
    path.write_text(json.dumps({"schema_version": 1, "areas": [area]}))
    monkeypatch.setattr(hotspot_tests, "settings", lambda: {"hotspot_tests": {"areas_path": str(path)}})
    with pytest.raises(ValueError):
        hotspot_tests.load_plan()
