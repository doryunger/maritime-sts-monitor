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


class FakeFleet:
    def __init__(self, states, spot_capacity=True, ips=None):
        self.states = dict(states)
        self.spot_capacity = spot_capacity
        self.ips = ips or {}
        self.calls = []

    def describe_instances(self, InstanceIds):
        i = InstanceIds[0]
        return {"Reservations": [{"Instances": [{"InstanceId": i, "State": {"Name": self.states[i]}, "PrivateIpAddress": self.ips.get(i)}]}]}

    def start_instances(self, InstanceIds):
        i = InstanceIds[0]
        if i == "spot" and not self.spot_capacity:
            raise ClientError({"Error": {"Code": "InsufficientInstanceCapacity"}}, "StartInstances")
        self.calls.append(("start", i))
        self.states[i] = "running"

    def stop_instances(self, InstanceIds):
        self.calls.append(("stop", InstanceIds[0]))
        self.states[InstanceIds[0]] = "stopping"

    def get_waiter(self, name):
        class Waiter:
            def wait(self, **kwargs):
                pass

        return Waiter()


def test_start_first_prefers_the_spot_worker():
    ec2 = FakeFleet({"spot": "stopped", "od": "stopped"})
    assert worker_power.start_first(ec2, ["spot", "od"], 60, sleep=lambda s: None) == ("running", "spot")
    assert ec2.calls == [("start", "spot")]


def test_start_first_falls_back_when_spot_has_no_capacity():
    ec2 = FakeFleet({"spot": "stopped", "od": "stopped"}, spot_capacity=False)
    assert worker_power.start_first(ec2, ["spot", "od"], 0, sleep=lambda s: None) == ("running", "od")
    assert ec2.calls == [("start", "od")]


def test_start_first_keeps_whichever_worker_is_already_up():
    ec2 = FakeFleet({"spot": "stopped", "od": "running"})
    assert worker_power.start_first(ec2, ["spot", "od"], 60, sleep=lambda s: None) == ("running", "od")
    assert ec2.calls == []


def test_report_names_the_running_worker_and_its_address():
    ec2 = FakeFleet({"spot": "running", "od": "stopped"}, ips={"spot": "10.0.0.5", "od": "10.0.0.4"})
    assert worker_power.report(ec2, "state", ["spot", "od"]) == {"action": "state", "state": "running", "instance_id": "spot", "private_ip": "10.0.0.5"}


def test_report_without_a_spot_worker_reads_the_single_worker():
    ec2 = FakeFleet({"od": "stopped"}, ips={"od": "10.0.0.4"})
    assert worker_power.report(ec2, "state", ["od"])["instance_id"] == "od"
