import io
from datetime import UTC, datetime, timedelta

import pyarrow.parquet as pq
import pytest
from botocore.exceptions import ClientError

from maritime_tip_cue import publish_merge as merge

RULE = {"dedupe_time_s": 120, "dedupe_length_share": 0.5}
T0 = datetime(2026, 10, 21, 6, 52, tzinfo=UTC)


class S3:
    def __init__(self):
        self.objects = {}
        self.etags = {}
        self.conflicts = 0
        self.uploads = []

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key]), "ETag": self.etags[Key]}

    def put_object(self, Bucket, Key, Body, ContentType=None, IfMatch=None, IfNoneMatch=None):
        if self.conflicts:
            self.conflicts -= 1
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        if IfNoneMatch and Key in self.objects or IfMatch and self.etags.get(Key) != IfMatch:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.objects[Key] = Body
        self.etags[Key] = f'"{len(Body)}-{len(self.objects)}-{id(Body)}"'

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        return {"Contents": [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)], "IsTruncated": False}

    def upload_file(self, path, bucket, key, ExtraArgs=None):
        self.uploads.append(key)


def row(finding_id, site="fujairah_outer", lat=25.0, lon=56.4, seconds=0, job="J1", length=200.0):
    return {
        "finding_id": finding_id, "geometry": b"x", "lat": lat, "lon": lon, "source": "s2", "hot_spot": site,
        "capture_utc": T0 + timedelta(seconds=seconds), "measures": '{"hull_a_length_m": %s}' % length,
        "job_id": job, "also_in": None, "qa_status": "unreviewed",
    }


def publish(s3, rows, job="J1", site="fujairah_outer", on_keep=None):
    key = f"sts/21-10-2026/{site}/s2.parquet"
    return merge.merge_group(s3, "b", "sts/", "21-10-2026", key, rows, job, RULE, on_keep)


def stored(s3, site="fujairah_outer"):
    return pq.read_table(io.BytesIO(s3.objects[f"sts/21-10-2026/{site}/s2.parquet"])).to_pylist()


def test_two_jobs_for_the_same_date_and_hot_spot_accumulate_instead_of_overwriting():
    s3 = S3()
    publish(s3, [row("a")], job="J1")
    publish(s3, [row("b", lat=25.5)], job="J2")
    assert sorted(r["finding_id"] for r in stored(s3)) == ["a", "b"]


def test_rerunning_a_job_replaces_its_own_rows_without_duplicating():
    s3 = S3()
    publish(s3, [row("a"), row("b", lat=25.5)], job="J1")
    result = publish(s3, [row("a")], job="J1")
    assert [r["finding_id"] for r in stored(s3)] == ["a"] and result["kept"] == ["a"]


def test_duplicate_seen_by_an_overlapping_hot_spot_is_skipped_and_noted_on_the_first():
    s3 = S3()
    publish(s3, [row("a")], job="J1")
    result = publish(s3, [row("a2", site="off_sohar", seconds=30)], job="J2", site="off_sohar")
    assert result["skipped"] == ["a2"] and result["kept"] == []
    assert stored(s3)[0]["also_in"] == "off_sohar/a2"
    assert "sts/21-10-2026/off_sohar/s2.parquet" not in s3.objects


def test_images_are_uploaded_only_for_findings_that_are_kept():
    s3, pushed = S3(), []
    publish(s3, [row("a")], job="J1")
    publish(s3, [row("a2", site="off_sohar", seconds=30), row("c", site="off_sohar", lat=26.0)], job="J2",
            site="off_sohar", on_keep=lambda kept: pushed.extend(r["finding_id"] for r in kept))
    assert pushed == ["c"]


def test_concurrent_writer_conflict_is_retried():
    s3 = S3()
    s3.conflicts = 2
    assert publish(s3, [row("a")])["kept"] == ["a"]


def test_endless_conflicts_fail_loudly():
    s3 = S3()
    s3.conflicts = 100
    with pytest.raises(RuntimeError, match="concurrent writers"):
        publish(s3, [row("a")])


def test_other_dates_and_sources_are_not_treated_as_duplicates():
    s3 = S3()
    publish(s3, [row("a")], job="J1")
    far = row("b", seconds=3600)
    assert publish(s3, [far], job="J2")["kept"] == ["b"]


def test_obsolete_image_columns_are_dropped_when_a_file_is_rewritten():
    s3 = S3()
    legacy = dict(row("a"), capture_tif="captures/x.tif", capture_markers_png="captures/x_markers.png", capture_png="captures/x.png")
    publish(s3, [legacy], job="J1")
    names = {n for r in stored(s3) for n in r}
    assert "capture_png" in names and not names & {"capture_tif", "capture_markers_png"}
