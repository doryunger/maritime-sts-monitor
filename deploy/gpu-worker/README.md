# GPU worker deployment

Current EC2 image, queue smoke-test and root-volume resize notes are in [the EC2 deployment record](../../docs/gpu-worker-ec2.md).

This role runs `maritime_tip_cue.worker` and the shared processing pipeline. It does not run the six-hour
catalogue scan or the always-on job manager. The Ubuntu one-scene orchestration polls the configured
`/_wake/status` handler until EC2 is ready, then runs one worker command over SSH while continuing to poll the
handler. Each status request refreshes the wake service's idle timer. The command starts
`worker run --job-id <job_id>` on EC2, which claims that task atomically from Redis and loads its pinned manifest;
SSH passes only the job ID. The worker renews its claim lease and publishes an attempt-fenced result. The
manager on the tracker host owns completion and retry transitions. EC2 does not run an idle Redis listener;
the one-job process starts after the wake handler reports ready.

## One-scene orchestration from the STS monitor Ubuntu host

Run the complete scan, single-job Redis handoff, wake request and SSH worker process from the Ubuntu host with
[`run-one-scene.py`](../catalogue/README.md#one-scene-end-to-end-run). It uses the EC2 key at
`~/.ssh/geogen-key.pem` and keeps the SSH session open until the selected job finishes. The Ubuntu host key is
`sts-instance.pem` at `%USERPROFILE%\Downloads\sts-instance.pem`; the EC2 worker key is `geogen-key.pem`.

Set `WORKER_WAKE_STATUS_URL` to the exact wake status route, and keep the worker address and key path in the
ignored Lightsail `.env`. The runner starts SSH only after the handler reports ready and private port 22 accepts
connections. The EC2 instance profile provides worker S3 access.

Clone the repository and build from its root with
`docker build -f deploy/gpu-worker/Dockerfile -t maritime-sts-gpu-worker .`. The image pins PyTorch 2.7,
CUDA 12.8, cuDNN 9, rslearn 0.1.17 and the exact Ai2 detector source revision used by the local pipeline.
It installs detector inference dependencies and reads the matching S1/S2 detector and attribute checkpoints
from the host model cache at `/opt/ai2-models`. Populate that cache once on the EC2 host from Ai2's published
model paths and mount it read-only into the worker container. The detector configs point to these locations
through `RSLP_PREFIX`; loading these trained checkpoints supersedes their training-only restore settings.
Keeping the multi-gigabyte checkpoints outside the image makes build and deployment transfers practical. The
host still needs NVIDIA Container Toolkit support.
The source package is shared by both roles today: the worker imports the manifest validator,
Redis queue protocol, configuration and pipeline modules from the same package. The role boundary is
operational and dependency based; this directory is not a standalone sparse checkout.

Create ignored `config.local.yaml` from `deploy/gpu-worker/config.worker.example.yaml` and an ignored `.env`
from `deploy/gpu-worker/.env.example` beside the clone. The config sets the image detector path and persistent
`/app/data` paths. The environment file supplies `STS_REDIS_URL`, `S3_BUCKET_NAME`, GizmoSQL settings and the
AWS region. Attach an EC2 instance profile that grants required read access to result receipts and scene sources,
and write access to the attempt artifact prefix. The container uses the EC2 metadata credential provider; verify
that it can obtain credentials before claiming a job. The role is attached to the tested instance, and the
worker's live `ListBucket` preflight for `catalogue/results/` passed after the bucket-specific policy was
attached. Keep Redis private and restricted to the worker.

The instance profile needs only the worker's S3 paths and the Sentinel-1 source. Replace `<S3_BUCKET_NAME>` with
the bucket configured in `.env` and attach this policy to the EC2 role:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadWorkerResultReceipts",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::<S3_BUCKET_NAME>/catalogue/results/*"
    },
    {
      "Sid": "CheckWorkerResultReceiptKeys",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::<S3_BUCKET_NAME>",
      "Condition": {
        "StringLike": {
          "s3:prefix": "catalogue/results/*"
        }
      }
    },
    {
      "Sid": "WriteWorkerAttemptArtifacts",
      "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::<S3_BUCKET_NAME>/catalogue/artifacts/*"
    },
    {
      "Sid": "ReadSentinel1Scenes",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::sentinel-s1-l1c/GRD/*"
    }
  ]
}
```

The Sentinel-1 source uses requester-pays requests; AWS bills those reads to the account making the requests.
The policy assumes the configured artifact bucket is in the same AWS account and has no explicit deny or
additional KMS key requirement. Sentinel-2 scene reads use the configured public data source. Do not attach
`AdministratorAccess` or store long-lived AWS keys in the worker `.env`.

In AWS, create an IAM role for the EC2 use case, attach the policy above, and confirm its trusted entity is
`ec2.amazonaws.com`. Then open **EC2 → Instances**, select the worker, and choose **Actions → Security → Modify
IAM role**. Select the role's instance profile and choose **Update IAM role**. The EC2 console creates an instance
profile automatically when you create the role there. After the association appears, the worker container should
obtain temporary credentials through IMDSv2.

Before processing jobs, download the four `best.ckpt` files from the URLs and destination paths in the fetch
script into `/opt/ai2-models/projects/...` on EC2 by running
`sudo bash deploy/gpu-worker/fetch-models.sh /opt/ai2-models`. The script skips existing nonempty files and
downloads missing checkpoints atomically. Preserve this directory when the host is replaced.
Place the AIS archive certificate in `data/secrets/ais_archive_cert.pem`, the Natural
Earth land GeoJSON files in `data/land/`, and the fixed-structure GeoJSON in `data/structures/`. These runtime
inputs persist outside the image.

Run a single job by ID with `docker run --rm --gpus all --shm-size=15g --network host --env-file .env -v "$PWD/config.local.yaml:/app/config.local.yaml:ro" -v "$PWD/data:/app/data" -v /opt/ai2-models:/opt/ai2-models:ro maritime-sts-gpu-worker run --job-id <job_id>`. This claims only that task and lets the manager finalize its result. The ready-queue drain command claims every ready job, so use it only for an intentional batch. The one-scene launcher is documented in the catalogue deployment guide. Keep the host `data/` directory
persistent so detector state and resumable work survive process or host restarts.

Use this command to inspect tasks:

```sh
docker run --rm --network host --env-file .env -v "$PWD/config.local.yaml:/app/config.local.yaml:ro" -v "$PWD/data:/app/data" -v /opt/ai2-models:/opt/ai2-models:ro maritime-sts-gpu-worker list
```

The manager records each attempt and allows one delayed retry. A second failure is terminal and requires an
explicit operator decision before a future manual retry mechanism is used.
