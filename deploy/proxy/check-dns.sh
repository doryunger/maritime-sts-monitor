#!/usr/bin/env bash
set -euo pipefail
env_file="${1:-$(dirname "$0")/proxy.env}"
set -a
. "$env_file"
set +a
public_ip="$(curl -fsS --max-time 10 https://checkip.amazonaws.com | tr -d '[:space:]')"
status=0
for name in "$APP_DOMAIN" "$VIEWER_DOMAIN"; do
    resolved="$(curl -fsS --max-time 10 -H 'accept: application/dns-json' "https://cloudflare-dns.com/dns-query?name=${name}&type=A" \
        | python3 -c 'import sys,json; print(" ".join(sorted(a["data"] for a in json.load(sys.stdin).get("Answer", []) if a.get("type") == 1)))' || true)"
    if [[ " $resolved " == *" $public_ip "* ]]; then
        echo "ok   $name -> $public_ip"
    else
        echo "FAIL $name resolves to '${resolved:-nothing}' but this host is $public_ip"
        status=1
    fi
done
exit "$status"
