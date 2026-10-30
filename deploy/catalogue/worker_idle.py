import argparse
import fcntl
import json
import re
import subprocess
import sys

import worker_host
from worker_host import ROOT

RULES_PATH = ROOT / "rules" / "worker_idle.json"
RULE_NAMES = ("cpu_busy_percent_max", "gpu_busy_percent_max", "quiet_checks_to_stop", "sample_seconds")
MEASURE_TIMEOUT_SECONDS = 90
STATUS_MODULE = "maritime_tip_cue.worker_status"


def load_rules(path=RULES_PATH):
    raw = json.loads(path.read_text(encoding="utf-8"))
    rules = {name: raw[name]["value"] for name in RULE_NAMES}
    missing = [name for name, value in rules.items() if value is None]
    if missing:
        raise RuntimeError(f"worker idle rules without a value: {', '.join(missing)}")
    return rules


def state_dir():
    return ROOT / "data" / "worker_idle"


def load_state(directory):
    path = directory / "state.json"
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"quiet_checks": 0}


def save_state(directory, state):
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / "state.json.tmp"
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(directory / "state.json")


def measure_command(rules):
    seconds = int(rules["sample_seconds"])
    return (
        f"cpu=$(vmstat {seconds} 2 | tail -1 | awk '{{print 100-$15}}'); "
        "gpu=$(for i in 1 2 3; do nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits | head -1; sleep 1; done "
        "| sort -n | tail -1); "
        'echo "CPU=$cpu GPU=$gpu"'
    )


def parse_measurement(output):
    match = re.search(r"CPU=(-?\d+)\s+GPU=(\d+)", output)
    if not match:
        return None
    return {"cpu": int(match.group(1)), "gpu": int(match.group(2))}


def decide(instance_state, measurement, quiet_checks, rules):
    if instance_state != "running":
        return "down", 0
    if measurement is None:
        return "unmeasured", quiet_checks
    if measurement["cpu"] >= rules["cpu_busy_percent_max"] or measurement["gpu"] >= rules["gpu_busy_percent_max"]:
        return "busy", 0
    quiet_checks += 1
    if quiet_checks >= rules["quiet_checks_to_stop"]:
        return "stop", 0
    return "quiet", quiet_checks


def measure(settings, rules):
    command = worker_host.ssh_command(settings, measure_command(rules), tty=False)
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=MEASURE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return None
    return parse_measurement(result.stdout) if result.returncode == 0 else None


def run_check(directory, instance_state, measure_worker, stop_worker, rules, record_status=None):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "worker_idle.lock").open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {"action": "skip", "reason": "another check is running"}
        state = load_state(directory)
        state_name = instance_state()
        measurement = measure_worker() if state_name == "running" else None
        action, quiet_checks = decide(state_name, measurement, state["quiet_checks"], rules)
        if record_status:
            record_status(state_name, measurement)
        if action == "stop":
            stop_worker()
        state["quiet_checks"] = quiet_checks
        save_state(directory, state)
        return {"action": action, "instance": state_name, "measurement": measurement, "quiet_checks": quiet_checks}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    env_path = ROOT / ".env"
    settings = worker_host.load_settings(env_path)
    rules = load_rules()

    def stop_worker():
        if not args.dry_run:
            worker_host.power(settings, "stop")

    def record_status(state_name, measurement):
        arguments = ["record", "--state", state_name]
        if measurement:
            arguments += ["--cpu", str(measurement["cpu"]), "--gpu", str(measurement["gpu"])]
        try:
            worker_host.manager_json(env_path, STATUS_MODULE, *arguments)
        except RuntimeError as error:
            print(f"worker status not recorded: {error}", file=sys.stderr)

    result = run_check(
        state_dir(),
        lambda: worker_host.power(settings, "state"),
        lambda: measure(settings, rules),
        stop_worker,
        rules,
        record_status,
    )
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
