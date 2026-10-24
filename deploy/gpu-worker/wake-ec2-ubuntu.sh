set -uo pipefail

status_url="${1:-${WORKER_WAKE_STATUS_URL:-}}"
worker_private_ip="${2:-${WORKER_PRIVATE_IP:-}}"
worker_key="${WORKER_KEY_PATH:-$HOME/.ssh/geogen-key.pem}"
poll_seconds="${POLL_SECONDS:-20}"
timeout_seconds="${WAKE_TIMEOUT_SECONDS:-1200}"
shift 2
if [[ "${1:-}" == "--" ]]; then
    shift
fi
remote_command="$*"

if [[ -z "$status_url" || -z "$worker_private_ip" ]]; then
    printf '%s\n' "Usage: bash wake-ec2-ubuntu.sh <status-url> <worker-private-ip> [-- <remote-command>]" >&2
    exit 2
fi

status_path="$(python3 -c 'import sys; from urllib.parse import urlparse; print(urlparse(sys.argv[1]).path.rstrip("/"))' "$status_url")"
if [[ "$status_path" != "/_wake/status" ]]; then
    printf '%s\n' "The status URL must target /_wake/status" >&2
    exit 2
fi

if [[ ! -r "$worker_key" ]]; then
    printf 'EC2 key is not readable: %s\n' "$worker_key" >&2
    exit 2
fi

status_url="${status_url%/}"
chmod 600 "$worker_key"

read_status() {
    curl -fsS --max-time 15 "$status_url"
}

deadline=$((SECONDS + timeout_seconds))
while (( SECONDS < deadline )); do
    status="$(read_status)" || exit 1
    state="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("ec2_state", ""))' <<< "$status")"
    stage="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("stage", ""))' <<< "$status")"
    detail="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("detail", ""))' <<< "$status")"
    printf '%s EC2=%s stage=%s\n' "$(date '+%H:%M:%S')" "$state" "$stage"
    if [[ "$stage" == "error" ]]; then
        printf 'Wake controller error: %s\n' "$detail" >&2
        exit 1
    fi
    if [[ "$stage" == "ready" ]]; then
        break
    fi
    sleep "$poll_seconds"
done
if [[ "$stage" != "ready" ]]; then
    printf 'Wake did not become ready within %s seconds\n' "$timeout_seconds" >&2
    exit 1
fi

while ! timeout 3 bash -c 'exec 3<>/dev/tcp/$1/22' _ "$worker_private_ip" >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
        printf 'Private SSH did not become reachable within %s seconds\n' "$timeout_seconds" >&2
        exit 1
    fi
    status="$(read_status)" || exit 1
    state="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("ec2_state", ""))' <<< "$status")"
    stage="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("stage", ""))' <<< "$status")"
    detail="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("detail", ""))' <<< "$status")"
    printf '%s EC2=%s waiting for private SSH\n' "$(date '+%H:%M:%S')" "$state"
    if [[ "$stage" == "error" ]]; then
        printf 'Wake controller error: %s\n' "$detail" >&2
        exit 1
    fi
    sleep "$poll_seconds"
done

ssh_command=(ssh -tt -i "$worker_key" -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=20 -o ServerAliveCountMax=120 "ubuntu@$worker_private_ip")
if [[ -n "$remote_command" ]]; then
    ssh_command+=("$remote_command")
fi
"${ssh_command[@]}" &
ssh_pid=$!
trap 'kill "$ssh_pid" 2>/dev/null || true; wait "$ssh_pid" 2>/dev/null || true' EXIT INT TERM

while kill -0 "$ssh_pid" 2>/dev/null; do
    ssh_state="$(ps -o stat= -p "$ssh_pid" 2>/dev/null || true)"
    if [[ "$ssh_state" == Z* || -z "$ssh_state" ]]; then
        break
    fi
    sleep "$poll_seconds"
    status="$(read_status)" || exit 1
    state="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("ec2_state", ""))' <<< "$status")"
    stage="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("stage", ""))' <<< "$status")"
    detail="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("detail", ""))' <<< "$status")"
    printf '%s EC2=%s stage=%s\n' "$(date '+%H:%M:%S')" "$state" "$stage"
    if [[ "$stage" == "error" ]]; then
        printf 'Wake controller error: %s\n' "$detail" >&2
        exit 1
    fi
done

wait "$ssh_pid"
ssh_status=$?
ssh_pid=""
trap - EXIT INT TERM
exit "$ssh_status"
