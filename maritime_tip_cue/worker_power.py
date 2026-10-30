import argparse
import json
import time

import boto3
from botocore.exceptions import ClientError

from .config import env


CAPACITY_RETRY_SECONDS = 30


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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "stop", "state"])
    args = parser.parse_args()
    ec2 = client()
    instance_id = env("WORKER_INSTANCE_ID")
    if args.action == "start":
        state = start(ec2, instance_id)
    elif args.action == "stop":
        state = stop(ec2, instance_id)
    else:
        state = instance_state(ec2, instance_id)["State"]["Name"]
    print(json.dumps({"action": args.action, "state": state}))


if __name__ == "__main__":
    main()
