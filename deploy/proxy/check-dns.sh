#!/usr/bin/env bash
set -euo pipefail
env_file="${1:-$(dirname "$0")/proxy.env}"
set -a
. "$env_file"
set +a
public_ip="$(curl -fsS --max-time 10 https://checkip.amazonaws.com | tr -d '[:space:]')"
status=0
for name in "$APP_DOMAIN" "$VIEWER_DOMAIN"; do
    resolved="$( (getent ahostsv4 "$name" || true) | awk '{print $1}' | sort -u | tr '\n' ' ')"
    if [[ " $resolved " == *" $public_ip "* ]]; then
        echo "ok   $name -> $public_ip"
    else
        echo "FAIL $name resolves to '${resolved:-nothing}' but this host is $public_ip"
        status=1
    fi
done
exit "$status"
