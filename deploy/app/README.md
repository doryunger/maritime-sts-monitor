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

## Access

Users reach the app at https://sts.stamsite.cc/ (Caddy, see `deploy/proxy/`), signed in with the login below. The app
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
