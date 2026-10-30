import importlib
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("fcntl")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy" / "catalogue"))
worker_idle = importlib.import_module("worker_idle")

RULES = {"cpu_busy_percent_max": 5, "gpu_busy_percent_max": 5, "quiet_checks_to_stop": 3, "sample_seconds": 10}


def check(tmp_path, state, measurement, stopped):
    return worker_idle.run_check(tmp_path, lambda: state, lambda: measurement, lambda: stopped.append(1), RULES)


def test_rules_file_has_a_value_for_every_threshold():
    rules = worker_idle.load_rules()
    assert set(rules) == set(worker_idle.RULE_NAMES)


def test_a_missing_rule_value_is_refused(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps({name: {"value": None} for name in worker_idle.RULE_NAMES}))
    with pytest.raises(RuntimeError):
        worker_idle.load_rules(path)


def test_measurement_output_is_parsed():
    assert worker_idle.parse_measurement("noise\nCPU=3 GPU=0\r\n") == {"cpu": 3, "gpu": 0}
    assert worker_idle.parse_measurement("CPU= GPU=") is None


def test_stopped_worker_is_left_alone_and_resets_the_count(tmp_path):
    stopped = []
    worker_idle.save_state(tmp_path, {"quiet_checks": 2})
    result = check(tmp_path, "stopped", None, stopped)
    assert result["action"] == "down" and result["quiet_checks"] == 0 and not stopped


def test_stops_only_after_enough_quiet_checks_in_a_row(tmp_path):
    stopped = []
    for expected in (1, 2):
        result = check(tmp_path, "running", {"cpu": 1, "gpu": 0}, stopped)
        assert result["action"] == "quiet" and result["quiet_checks"] == expected
    assert not stopped
    assert check(tmp_path, "running", {"cpu": 1, "gpu": 0}, stopped)["action"] == "stop"
    assert stopped == [1]
    assert worker_idle.load_state(tmp_path)["quiet_checks"] == 0


@pytest.mark.parametrize("measurement", [{"cpu": 60, "gpu": 0}, {"cpu": 1, "gpu": 90}, {"cpu": 5, "gpu": 0}])
def test_any_busy_reading_resets_the_count(tmp_path, measurement):
    stopped = []
    worker_idle.save_state(tmp_path, {"quiet_checks": 2})
    result = check(tmp_path, "running", measurement, stopped)
    assert result["action"] == "busy" and result["quiet_checks"] == 0 and not stopped


def test_an_unmeasurable_worker_is_never_stopped_and_keeps_its_count(tmp_path):
    stopped = []
    worker_idle.save_state(tmp_path, {"quiet_checks": 2})
    result = check(tmp_path, "running", None, stopped)
    assert result["action"] == "unmeasured" and result["quiet_checks"] == 2 and not stopped


def test_second_check_cannot_run_concurrently(tmp_path):
    import fcntl

    with (tmp_path / "worker_idle.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        result = check(tmp_path, "running", {"cpu": 1, "gpu": 0}, [])
    assert result["action"] == "skip"
