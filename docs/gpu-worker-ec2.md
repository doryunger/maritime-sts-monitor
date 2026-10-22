# GPU worker EC2 deployment status

Checked 2026-10-22. This records the worker image, instance setup and current one-scene end-to-end validation state.

## Current state

- The worker image was built on EC2 from the repository Dockerfile using the pinned PyTorch 2.7 CUDA 12.8 base, rslearn 0.1.17 and pinned Ai2 detector source.
- The image build imports both Sentinel detector pipelines and the vessel attribute model successfully.
- The container sees a Tesla T4 through NVIDIA Container Toolkit; PyTorch reports CUDA 12.8 and `torch.cuda.is_available()` is true.
- The worker container connected to Redis and listed ready tasks. Listing did not claim or mutate any task.
- The four published detector checkpoints are included in the worker image under `/opt/ai2-models`, fetched in a separate Docker build stage. The existing host `/opt/ai2-models` cache remains available but is no longer mounted into the worker container.
- The AIS archive certificate, land GeoJSON files and structures GeoJSON are present in the isolated worker test directory under `data/`.
- The EC2 instance profile supplies credentials, and a live worker-container `ListBucket` preflight for the configured `catalogue/results/` prefix passed after the bucket-specific policy was attached.
- The worker container passed a CUDA tensor canary on the Tesla T4. Shadow's successful S2 log showed Lightning using CUDA, 10-38% GPU utilization, about 900 MiB GPU memory, and 175 seconds total. Worker inference now logs NVIDIA utilization/memory every two seconds alongside PyTorch memory diagnostics. Initial container attempts exposed forkserver and model-management path failures; the image now includes the four checkpoints so rslearn can load and write under the same writable `/opt/ai2-models` path. Its pinned PyTorch 2.7.0 differs from Shadow's PyTorch 2.9.1 and remains a runtime variable to assess after a successful worker run.
- SSH from the Windows PC to the worker's public address timed out. The STS monitor Lightsail host reaches the EC2 private address on TCP 22; its `geogen-key.pem` authenticates successfully when SSH is run from Lightsail. The key is installed at `~/.ssh/geogen-key.pem` with mode 600.
- The first failures occurred with about 12 GiB free on the original filesystem. Storage did not cause them. The user expanded the existing root EBS volume from 80 to 100 GB. The existing root partition and ext4 filesystem were grown online; `df` then reported 96 GiB total and 32 GiB free. Existing data and boot partitions were retained.
- The existing demo web and app containers stayed healthy throughout the worker build and checks.

## SSH access procedure

Connect to the Lightsail control host with the identity and host recorded in the ignored
`data/secrets/control_host.json`. In this Codex environment, run OpenSSH through an elevated
`exec_command` (`sandbox_permissions=require_escalated`); a restricted-shell network error is not evidence that
the Lightsail key was rejected. For an interactive session, allocate a TTY and keep the returned process
session open:

```powershell
$control = Get-Content .\data\secrets\control_host.json -Raw | ConvertFrom-Json
$key = [Environment]::ExpandEnvironmentVariables($control.identity_file)
ssh -tt -i $key -o BatchMode=yes -o ServerAliveInterval=20 -o ServerAliveCountMax=120 "$($control.user)@$($control.host)"
```

SSH authentication has not started if verbose OpenSSH output stops at key exchange or banner exchange. For
example, `Connection established` followed by `kex_exchange_identification: write: Connection timed out` is a
transport failure before key authentication; do not rotate the key or switch to an EC2 address on that basis.
Once on Lightsail, connect to EC2 only through `deploy/gpu-worker/wake-ec2-ubuntu.sh` with the exact
`/_wake/status` URL, the worker's private IPv4 address and `~/.ssh/geogen-key.pem` (mode 600). The script polls
the handler to start and keep EC2 active while the SSH worker command runs. See [the wake and SSH procedure](../deploy/gpu-worker/README.md).

## Build and disk findings

The first image definition copied detector checkpoints into a Docker layer. Docker's legacy image export duplicated the multi-gigabyte layer while saving it and drove root disk free space below 7 GiB. The build was stopped before it affected the existing containers. The Dockerfile was changed to keep model weights in host storage, and `deploy/gpu-worker/fetch-models.sh` now fetches missing weights atomically into `/opt/ai2-models`.

The corrected image built successfully. The root filesystem is ext4 on the first partition of `/dev/nvme1n1`. After removing only dangling images created by these worker builds, Docker reported 11.13 GiB reclaimed and the root filesystem had about 14 GiB free. Rebuilding the worker image with the CLI fix left about 12 GiB free. The worker image, model files and existing app images were retained. On 2026-10-22 the root EBS volume was enlarged to 100 GB; the root partition and ext4 filesystem were extended online, resulting in 96 GiB total and about 32 GiB free. Confirm free disk with `df -hT /` before running or retrying a task because scene data and detector scratch files also use this filesystem; stop if free space approaches 3 GiB.

## Manager and retry integration findings

The first container invocation exited before touching scene data with `AttributeError: 'Namespace' object has no attribute 'resume'`. The worker command parser now defines the supported flag. The task stayed claimed in Redis because the deployed manager used an older implementation and SQLite schema. The manager backup is `/home/ubuntu/maritime-sts-monitor/data/catalogue/tracker.pre-worker-manager-v2.20261022.db` on Lightsail; the original database remains in place. The manager container migration added the retry ledger without dropping existing tables. The failed event was restored to the Redis event stream only after confirming the exact attempt ID, Redis owner, receipt status and unchanged SQLite job state. The manager then recorded attempt 1 as failed and scheduled exactly one retry.

The managed wake controller's idle-stop interval elapsed while the retry was delayed. Its existing status path completed the stop/start sequence and reached ready before the retry ran; no direct EC2 start request was used. The controller was left to its existing idle-stop behavior after the failed attempt.

## Increase the existing EBS volume

Increasing the size of the current EBS volume preserves its existing blocks; it does not require creating a replacement volume or formatting the disk. AWS supports increasing an attached EBS volume with Elastic Volumes. Create a snapshot first as a rollback point, then modify the existing root volume's size in the EC2 console. Do not detach, replace or format the current volume.

After the volume modification reaches `optimizing` or `completed`, extend the existing partition and ext4 filesystem on EC2:

```sh
lsblk -f
sudo growpart /dev/nvme1n1 1
sudo resize2fs /dev/nvme1n1p1
df -hT /
```

The EBS volume identifier is account-specific and is intentionally kept out of repository files. In the EC2 console, identify the root volume by the worker instance's block-device mapping; the current Linux device is `/dev/nvme1n1`, with ext4 partition `/dev/nvme1n1p1`. This existing volume was increased in place to 100 GB and the root partition/filesystem were expanded. EBS volume size cannot be decreased later.

## Worker run commands

From the isolated clone on EC2, prepare runtime settings and assets as described in [the worker deployment README](../deploy/gpu-worker/README.md). Check the queue without claiming tasks:

```sh
docker run --rm --network host --env-file .env \
  -v "$PWD/config.local.yaml:/app/config.local.yaml:ro" \
  -v "$PWD/data:/app/data" \
  maritime-sts-gpu-worker:test list
```

Run an explicitly selected ready job only after confirming enough free space for its inputs and scratch data:

```sh
docker run --rm --gpus all --shm-size=15g --network host --env-file .env \
  -v "$PWD/config.local.yaml:/app/config.local.yaml:ro" \
  -v "$PWD/data:/app/data" \
  maritime-sts-gpu-worker:test run --job-id <job_id>
```
