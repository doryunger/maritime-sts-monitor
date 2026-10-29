# STS monitor app on the control host

The app (`app/`: FastAPI backend, React frontend) runs as a container on the Ubuntu control host next to the job
manager, so it is always up and does not depend on the development PC.

## What it shows there

- **Jobs:** the ledger read from the Postgres `processing` schema (`STS_DATABASE_URL` in the host `.env`) plus
  live scene progress from Redis.
- **Runs and QA:** run folders live under `data/app/runs` (a host volume). With `STS_RUNS_FROM_S3=1`, set in the unit,
  the app syncs every 5 minutes: each succeeded job's attempt from `catalogue/reviews/<job>/<attempt>/` and the older
  local runs from `catalogue/reviews/legacy/run-<n>/` are downloaded for every `review.runs` row in Postgres that has no
  folder under `data/app/runs`, checked against the sizes and hashes in their `index.json`. The folder holds only scene
  chips and windows and the image cache: run state and QA flags are in Postgres (`STS_DATABASE_URL`), which the app
  needs to start. A folder already downloaded is never overwritten. Run numbers are the global `review.runs.run_no`.
- **Hotspot tests:** `/hotspot-tests` opens with the random grid scan status map (tile status, today's scene budget; select a tile for its scenes and a link to its review run) above the scene index of the test runs (pair candidates only, reviewed as real
  pair or not); `/hotspot-tests/areas` lists the versioned scan areas and the Parquets under
  `hotspot-tests/findings/`. The app reads these private objects using its AWS credentials and downloads review
  images from `hotspot-tests/reviews/`; it does not make the bucket public.

## Access

Users reach the app at https://sts.stamsite.cc/ and the hotspot-test page at https://hotspots.stamsite.cc/hotspot-tests (Caddy, see `deploy/proxy/`), signed in with the login below; both names reach the same app and login. The app
itself listens only on `127.0.0.1:8010` (`STS_APP_HOST` and `STS_APP_PORT` in the unit), so the only way in from the
internet is through the proxy. The username is `sts` and the password is `STS_APP_PASSWORD` in the host `.env`; the app
refuses to listen on a non-loopback address without one. After 5 wrong passwords a client address is locked out for
5 minutes. `app\open-remote.cmd` still opens an SSH tunnel to http://127.0.0.1:8010/ from the PC if the proxy is down.

## Build and install

On the host, from the repository checkout (a rebuild needs roughly 600 MB of free memory, so run it while the
dispatcher is idle):

```bash
cd /home/ubuntu/maritime-sts-monitor
sudo docker build -f deploy/app/Dockerfile --build-arg GIT_COMMIT=<commit> -t maritime-sts-app .
mkdir -p data/app/runs data/app/reviews && sudo chown -R 1000:1000 data/app
sudo cp deploy/app/sts-app.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now sts-app
```

The unit sets `STS_APP_PORT=8010` and runs the image as uid 1000 with a 400 MB memory limit and `Restart=always`. It reads the same protected
`.env` as the job manager (Redis URL, AWS settings). Update with a new `docker build` and `systemctl restart sts-app`.

### Hotspot-test S3 IAM

Two AWS identities touch the test prefixes, and each needs one small inline policy. Neither policy grants access to
`sts/`, `sanctioned/`, `captures/` or `catalogue/`, and neither changes the bucket policy or Block Public Access.

| Identity | Used by | Needs | Policy file |
|---|---|---|---|
| IAM user whose access key is in the control host `.env` | `sts-app`, `sts-job-manager`, `sts-catalogue` | List `hotspot-tests/findings/`; read `hotspot-tests/{artifacts,findings,reviews}/` | [`iam-hotspot-tests-control.json`](iam-hotspot-tests-control.json) |
| IAM role on the GPU worker's EC2 instance profile | `maritime-sts-gpu-worker` | Write `hotspot-tests/{artifacts,findings,reviews}/` | [`../gpu-worker/iam-hotspot-tests-worker.json`](../gpu-worker/iam-hotspot-tests-worker.json) |

The files contain `<S3_BUCKET_NAME>`; replace it with the exact `S3_BUCKET_NAME` from the control host `.env`.
Do the IAM changes from an admin identity (the AWS console, or CloudShell signed in as an admin), not with the
`.env` keys, which cannot edit their own permissions.

**1. Find the control-host user.** On the control host:

```bash
cd /home/ubuntu/maritime-sts-monitor
set -a; . ./.env; set +a
aws sts get-caller-identity --query Arn --output text    # arn:aws:iam::<account>:user/<USER_NAME>
```

If `aws` is not installed there, run the same call with boto3 through the app image:
`sudo docker run --rm --env-file .env --entrypoint python maritime-sts-app -c "import boto3; print(boto3.client('sts').get_caller_identity()['Arn'])"`.

**2. Find the worker role.** AWS console, EC2, Instances, select the worker, Security tab, "IAM Role" link. The link
opens the role; note its name. (From the worker: `curl -s -H "X-aws-ec2-metadata-token: $(curl -s -X PUT -H
'X-aws-ec2-metadata-token-ttl-seconds: 60' http://169.254.169.254/latest/api/token)"
http://169.254.169.254/latest/meta-data/iam/security-credentials/` prints the role name.)

**3. Check for KMS encryption.** If the bucket default encryption is SSE-KMS with a customer-managed key, both
identities also need `kms:Decrypt` (control user) and `kms:GenerateDataKey` (worker role) on that key. Check with
`aws s3api get-bucket-encryption --bucket <S3_BUCKET_NAME>`; `AES256` or an `aws/s3` key needs nothing extra.

**4. Control policy.** A read/write probe on the control user's keys (put, head, get and delete under each of the three
test prefixes, plus listing and reading `sts/`, `sanctioned/` and `catalogue/*`) succeeded without any new policy, so
that user currently has unrestricted bucket access and this step changes nothing. Attach the policy only if that
access is later narrowed or the host `.env` turns out to use a different user than the one `get-caller-identity`
showed in step 1. Console: IAM, Users, `<USER_NAME>`, Permissions tab, Add permissions, Create inline policy, JSON tab,
paste [`iam-hotspot-tests-control.json`](iam-hotspot-tests-control.json) with the bucket name filled in, Next, name it
`sts-hotspot-tests-control`, Create policy. CLI from an admin shell:

```bash
sed "s/<S3_BUCKET_NAME>/$S3_BUCKET_NAME/g" deploy/app/iam-hotspot-tests-control.json > /tmp/control.json
aws iam put-user-policy --user-name <USER_NAME> --policy-name sts-hotspot-tests-control --policy-document file:///tmp/control.json
```

**5. Worker policy (required).** The worker role has no statement for the test prefixes, so its first test upload fails
with `AccessDenied` and the attempt is recorded as failed. Add one inline policy: IAM, Roles, `<ROLE_NAME>`, Add
permissions, Create inline policy, JSON tab, paste [`iam-hotspot-tests-worker.json`](../gpu-worker/iam-hotspot-tests-worker.json)
with the bucket name filled in, name it `sts-hotspot-tests-worker`, Create policy. CLI:

```bash
sed "s/<S3_BUCKET_NAME>/<bucket>/g" deploy/gpu-worker/iam-hotspot-tests-worker.json > /tmp/worker.json
aws iam put-role-policy --role-name <ROLE_NAME> --policy-name sts-hotspot-tests-worker --policy-document file:///tmp/worker.json
```

The worker only calls `PutObject` (single and multipart uploads) on the test prefixes; it does not read, head, list or
delete there, so nothing else is needed. The role's existing statements stay as they are. The `catalogue/claims/*` and
`catalogue/results/*` write statements in the combined policy of the GPU worker guide are for direct, non-queue runs
only; the Redis queue path used in production does not need them.

**6. Verify the permissions.** IAM changes apply within seconds. From the control host, with the `.env` keys:

```bash
aws s3 ls s3://$S3_BUCKET_NAME/hotspot-tests/findings/    # succeeds, empty until the first scan
aws s3 ls s3://$S3_BUCKET_NAME/hotspot-tests/reviews/     # AccessDenied is expected: only findings/ is listable
```

From the worker (the instance profile supplies credentials; use the image if the CLI is not installed there), write
a probe that the control user must then be able to read:

```bash
echo probe | aws s3 cp - s3://<S3_BUCKET_NAME>/hotspot-tests/artifacts/_iam_check/probe.txt     # worker role
aws s3 cp s3://$S3_BUCKET_NAME/hotspot-tests/artifacts/_iam_check/probe.txt -                   # control host
```

Neither identity has delete rights on the test prefixes, so remove the probe object from the console as an admin.
To test without writing anything, an admin can run `aws iam simulate-principal-policy --policy-source-arn <ARN>
--action-names s3:GetObject --resource-arns arn:aws:s3:::<S3_BUCKET_NAME>/hotspot-tests/findings/x.parquet`.
Last, after the app is rebuilt, open `/hotspot-tests`: the area table and an empty Parquet list mean the app's
`ListBucket` works; a "Could not load test results" error points at the control policy or `AWS_REGION`.

**Rollback.** Delete the two inline policies (`aws iam delete-user-policy` and `aws iam delete-role-policy`). Test
scans then fail at upload and the manager records the failed attempt; regular scans are unaffected.

The manager needs `GetObject` on artifacts and findings because it verifies uploaded objects with `HeadObject`; the app
lists and downloads findings and downloads review images. If app and manager later use separate AWS identities, give
the manager only `GetObject` on `hotspot-tests/artifacts/*` and `hotspot-tests/findings/*`, and give the app the
`ListBucket` condition and `GetObject` on findings and reviews.

## Sync results button

The Runs page has a "Sync results" button. The app container (400 MB cap) never runs the sync: `POST /api/sync` writes
a `pending` row to `review.sync_runs`, and `sts-sync.timer` starts `sts-sync.service` every 30 seconds. That oneshot
runs a short-lived container from the same image (`python -m maritime_tip_cue.publish_service run-pending`, 700 MB
cap), which claims the oldest pending row, runs publish sync with upload on and stores the result in the row. With
nothing pending it exits at once. It needs the same `.env` (S3, `GIZMOSQL_*`, `EQUASIS_*`, `STS_DATABASE_URL`, Redis),
the AIS certificate under `data/secrets/` and the run folders in `data/app/runs` (the captures of newly added pairs
are built from them), and `data/equasis` (the Equasis lookup cache, so a vessel is never asked twice across syncs;
create it first: `mkdir -p data/equasis && sudo chown 1000:1000 data/equasis`). Install after the image is built:

```bash
sudo cp deploy/app/sts-sync.service deploy/app/sts-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now sts-sync.timer
```

Check with `systemctl list-timers sts-sync.timer` and `journalctl -u sts-sync -n 50`. A request left `running` for
over 3 hours is marked failed (`abandoned`) by the next poll; a failed sync leaves S3 as the per-file merge left it and
a new click resumes. Rebuilding the image updates both the app and the sync.
