import argparse
import json
import os
import sys
import time

import boto3
from botocore.exceptions import ClientError, WaiterError

from .config import env


CAPACITY_RETRY_SECONDS = 30
STATE_ORDER = ["running", "pending", "stopping", "shutting-down", "stopped", "terminated"]


def client():
    return boto3.client("ec2", region_name=env("AWS_REGION"))


def instance_state(ec2, instance_id):
    return ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]


def start(ec2, instance_id, timeout=600, sleep=time.sleep):
    deadline = time.time() + timeout
    while instance_state(ec2, instance_id)["State"]["Name"] == "stopping":
        if time.time() > deadline:
            raise RuntimeError("worker did not finish stopping")
        sleep(5)
    while instance_state(ec2, instance_id)["State"]["Name"] == "stopped":
        try:
            ec2.start_instances(InstanceIds=[instance_id])
        except ClientError as error:
            if error.response["Error"]["Code"] != "InsufficientInstanceCapacity" or time.time() > deadline:
                raise
            sleep(CAPACITY_RETRY_SECONDS)
    ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])
    return instance_state(ec2, instance_id)["State"]["Name"]


def stop(ec2, instance_id):
    state = instance_state(ec2, instance_id)["State"]["Name"]
    if state in ("pending", "running"):
        ec2.stop_instances(InstanceIds=[instance_id])
    return instance_state(ec2, instance_id)["State"]["Name"]


def worker_ids():
    spot = os.environ.get("WORKER_SPOT_INSTANCE_ID", "")
    return [i for i in (spot, env("WORKER_INSTANCE_ID")) if i]


def active(ec2, instance_ids):
    found = [instance_state(ec2, i) for i in instance_ids]
    return min(found, key=lambda i: STATE_ORDER.index(i["State"]["Name"]) if i["State"]["Name"] in STATE_ORDER else len(STATE_ORDER))


def start_first(ec2, instance_ids, spot_wait, sleep=time.sleep):
    current = active(ec2, instance_ids)
    if current["State"]["Name"] in ("running", "pending"):
        return start(ec2, current["InstanceId"], sleep=sleep), current["InstanceId"]
    *preferred, fallback = instance_ids
    for instance_id in preferred:
        try:
            return start(ec2, instance_id, timeout=spot_wait, sleep=sleep), instance_id
        except (ClientError, WaiterError, RuntimeError) as error:
            if isinstance(error, ClientError) and error.response["Error"]["Code"] != "InsufficientInstanceCapacity":
                raise
            print(f"{instance_id} did not start ({type(error).__name__}), trying the next worker", file=sys.stderr, flush=True)
    return start(ec2, fallback, sleep=sleep), fallback


def report(ec2, action, instance_ids, instance_id=None):
    current = instance_state(ec2, instance_id) if instance_id else active(ec2, instance_ids)
    return {"action": action, "state": current["State"]["Name"], "instance_id": current["InstanceId"], "private_ip": current.get("PrivateIpAddress")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "stop", "state"])
    args = parser.parse_args()
    ec2 = client()
    instance_ids = worker_ids()
    instance_id = None
    if args.action == "start":
        _, instance_id = start_first(ec2, instance_ids, int(os.environ.get("WORKER_SPOT_WAIT_SECONDS", "600")))
    elif args.action == "stop":
        for i in instance_ids:
            stop(ec2, i)
    print(json.dumps(report(ec2, args.action, instance_ids, instance_id)))


if __name__ == "__main__":
    main()
