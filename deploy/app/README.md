# STS monitor app on the control host

The app (`app/`: FastAPI backend, React frontend) runs as a container on the Ubuntu control host next to the job
manager, so it is always up and does not depend on the development PC.

## What it shows there

- **Jobs:** the ledger read directly from `data/catalogue/tracker.db` (mounted read-only, opened with `mode=ro`) plus
  live scene progress from Redis. `STS_LEDGER_LOCAL=1` in the image selects this; without it the app reads the ledger
  over SSH, which is how the development PC runs it.
- **Runs and QA:** read from run folders under `data/app/runs`, which is empty on the host. Runs made by the EC2 worker
  reach S3 as review data (`catalogue/reviews/<job>/<attempt>/`); loading those into the app is not built yet, so this
  page is empty on the host until then. QA flags are written to the run's own database.

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
