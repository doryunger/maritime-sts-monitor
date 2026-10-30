import pytest
from botocore.exceptions import ClientError

pytest.importorskip("boto3")
from maritime_tip_cue import worker_power


class FakeEc2:
    def __init__(self, states):
        self.states = list(states)
        self.calls = []

    def describe_instances(self, InstanceIds):
        name = self.states[0] if len(self.states) == 1 else self.states.pop(0)
        return {"Reservations": [{"Instances": [{"State": {"Name": name}}]}]}

    def start_instances(self, **kwargs):
        if getattr(self, "capacity_failures", 0) > 0:
            self.capacity_failures -= 1
            raise ClientError({"Error": {"Code": "InsufficientInstanceCapacity"}}, "StartInstances")
        self.calls.append("start")

    def stop_instances(self, **kwargs):
        self.calls.append("stop")

    def get_waiter(self, name):
        calls = self.calls

        class Waiter:
            def wait(self, **kwargs):
                calls.append("wait")

        return Waiter()


def test_start_starts_a_stopped_worker_and_waits_for_running():
    ec2 = FakeEc2(["stopped", "stopped", "running"])
    assert worker_power.start(ec2, "i-1") == "running"
    assert ec2.calls == ["start", "wait"]


def test_start_does_not_restart_a_running_worker():
    ec2 = FakeEc2(["running", "running", "running"])
    worker_power.start(ec2, "i-1")
    assert "start" not in ec2.calls


def test_start_waits_for_a_stopping_worker_before_starting():
    ec2 = FakeEc2(["stopping", "stopping", "stopped", "stopped", "running"])
    worker_power.start(ec2, "i-1", sleep=lambda s: None)
    assert ec2.calls == ["start", "wait"]


def test_stop_stops_a_running_worker():
    ec2 = FakeEc2(["running", "stopping"])
    assert worker_power.stop(ec2, "i-1") == "stopping"
    assert ec2.calls == ["stop"]


def test_stop_leaves_a_stopped_worker_alone():
    ec2 = FakeEc2(["stopped"])
    worker_power.stop(ec2, "i-1")
    assert ec2.calls == []


def test_start_retries_while_aws_has_no_capacity():
    ec2 = FakeEc2(["stopped"] * 4 + ["running"])
    ec2.capacity_failures = 2
    sleeps = []
    assert worker_power.start(ec2, "i-1", sleep=sleeps.append) == "running"
    assert len(sleeps) == 2
    assert ec2.calls == ["start", "wait"]
