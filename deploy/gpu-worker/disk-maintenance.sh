#!/bin/bash
set -u
KEEP_CACHE_GB=${KEEP_CACHE_GB:-15}
KEEP_ROLLBACK_TAGS=${KEEP_ROLLBACK_TAGS:-1}
MIN_FREE_GB=${MIN_FREE_GB:-10}
IMAGE=${WORKER_IMAGE_REPO:-maritime-sts-gpu-worker}

free_gb() { df -BG --output=avail / | tail -1 | tr -dc 0-9; }
usage() { docker system df --format '{{.Type}}={{.Size}}' | tr '\n' ' '; }

before=$(free_gb)
usage_before=$(usage)
docker images "$IMAGE" --format '{{.Tag}}' | grep '^pre-' | tail -n +$((KEEP_ROLLBACK_TAGS + 1)) | while read -r tag; do
    docker rmi "$IMAGE:$tag" >/dev/null
done
docker image prune -f >/dev/null
docker builder prune -f --keep-storage "${KEEP_CACHE_GB}GB" >/dev/null
after=$(free_gb)
echo "disk-maintenance: free ${before}G -> ${after}G; before: ${usage_before}; after: $(usage)"
if [ "$after" -lt "$MIN_FREE_GB" ]; then
    echo "disk-maintenance: WARNING only ${after}G free after cleanup, below ${MIN_FREE_GB}G; remove unused images or grow the volume" >&2
    exit 1
fi
