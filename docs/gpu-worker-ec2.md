# GPU worker EC2 deployment status

Checked 2026-10-22. This records the worker image, instance setup and current one-scene end-to-end validation state.

## Current state

- The worker image was built on EC2 from the repository Dockerfile using the pinned PyTorch 2.7 CUDA 12.8 base, rslearn 0.1.17 and pinned Ai2 detector source.
- The image build imports both Sentinel detector pipelines and the vessel attribute model successfully.
- The container sees a Tesla T4 through NVIDIA Container Toolkit; PyTorch reports CUDA 12.8 and `torch.cuda.is_available()` is true.
- The worker container connected to Redis and listed ready tasks. Listing did not claim or mutate any task.
- The four published detector checkpoints now live in persistent `/opt/ai2-models`. The worker image does not contain these multi-gigabyte files; mount this directory read-only at the same path.
- The AIS archive certificate, land GeoJSON files and structures GeoJSON are present in the isolated worker test directory under `data/`.
- The EC2 instance profile supplies credentials, and a live worker-container `ListBucket` preflight for the configured `catalogue/results/` prefix passed after the bucket-specific policy was attached.
- The selected one-scene job reached the worker twice but produced no S3 artifacts. Attempt 1 failed because the configured detector venv Python path is absent from the container. Attempt 2 used a temporary container-local interpreter link, then failed because the detector runner received a relative `run_spec.json` path after changing directories. The manager recorded the job as terminal after its single retry. Both path fixes are now in the local checkout and need a rebuilt worker image before another one-scene run.
- SSH from the Windows PC to the worker's public address timed out. The STS monitor Lightsail host reaches the EC2 private address on TCP 22; its `geogen-key.pem` authenticates successfully when SSH is run from Lightsail. The key is installed at `~/.ssh/geogen-key.pem` with mode 600.
- Operational rule: SSH to the EC2 worker only from the STS monitor Ubuntu host through the wake status handler, using its private IPv4 address and the EC2 key pair `geogen-key` installed at `~/.ssh/geogen-key.pem` (mode 600). The local PC identity in `data/secrets/control_host.json` is only for reaching Ubuntu; it is not the EC2 key. See [the wake and SSH procedure](../deploy/gpu-worker/README.md).
- The first failures occurred with about 12 GiB free on the original filesystem. Storage did not cause them. The user expanded the existing root EBS volume from 80 to 100 GB. The existing root partition and ext4 filesystem were grown online; `df` then reported 96 GiB total and 32 GiB free. Existing data and boot partitions were retained.
- The existing demo web and app containers stayed healthy throughout the worker build and checks.

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
  -v /opt/ai2-models:/opt/ai2-models:ro \
  maritime-sts-gpu-worker:test list
```

Run an explicitly selected ready job only after confirming enough free space for its inputs and scratch data:

```sh
docker run --rm --gpus all --shm-size=15g --network host --env-file .env \
  -v "$PWD/config.local.yaml:/app/config.local.yaml:ro" \
  -v "$PWD/data:/app/data" \
  -v /opt/ai2-models:/opt/ai2-models:ro \
  maritime-sts-gpu-worker:test run --job-id <job_id>
```
