# STS monitor app on the control host

The app (`app/`: FastAPI backend, React frontend) runs as a container on the Ubuntu control host next to the job
manager, so it is always up and does not depend on the development PC.

## What it shows there

- **Jobs:** the ledger read directly from `data/catalogue/tracker.db` (mounted read-only, opened with `mode=ro`) plus
  live scene progress from Redis. `STS_LEDGER_LOCAL=1` in the image selects this; without it the app reads the ledger
  over SSH, which is how the development PC runs it.
- **Runs and QA:** run folders live under `data/app/runs` (a host volume). With `STS_RUNS_FROM_S3=1`, set in the unit,
  the app syncs every 5 minutes: each succeeded job's attempt from `catalogue/reviews/<job>/<attempt>/` and the older
  local runs from `catalogue/reviews/legacy/run-<n>/` are downloaded, checked against the sizes and hashes in their
  `index.json`, and registered under local run numbers. A run already downloaded is never overwritten, so QA flags
  saved in its database stay. Run numbers on the host differ from those on the development PC.

## Access

The container uses the host network and binds `127.0.0.1:8765`, so nothing is exposed to the internet. Reach it from
the PC through SSH:

```powershell
ssh -i $env:USERPROFILE\Downloads\sts-instance.pem -N -L 8765:127.0.0.1:8765 ubuntu@<control-host>
```

then open http://127.0.0.1:8765/. Do not publish the port or open it in the Lightsail firewall without adding
authentication: the app can write QA flags.

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

The unit runs the image as uid 1000 with a 400 MB memory limit and `Restart=always`. It reads the same protected
`.env` as the job manager (Redis URL, AWS settings). Update with a new `docker build` and `systemctl restart sts-app`.
