# Postgres on the control host

One Postgres 16 container holds the review database: database `sts`, schema `review`. The run databases that used to
be one SQLite file per run are tables here, keyed by `review.runs.run_key`. Data lives in `data/postgres/data`, the
login in `data/postgres/postgres.env` and the TLS key and certificate in `data/postgres/tls/`, all ignored by Git.

## Access

The server listens on every interface. `pg_hba.conf` accepts password logins (scram-sha-256) only over TLS from
other hosts, refuses plain connections, and allows plain loopback for the containers on the host. Use a random user
name and a long random password, never `postgres`. Limit TCP 5432 in the Lightsail firewall to the addresses that
need it (the GPU worker and the reviewers).

Clients verify the server with its certificate:

```
postgresql://<user>:<password>@<host>:5432/sts?sslmode=verify-full&sslrootcert=<path to server.crt>
```

Set that as `STS_DATABASE_URL` in the client's `.env`. On the host it points at `127.0.0.1`.

## Install

```bash
cd /home/ubuntu/maritime-sts-monitor
mkdir -p data/postgres/data data/postgres/tls
umask 077
USERNAME="rv_$(openssl rand -hex 4)"
PW=$(openssl rand -base64 60 | tr -dc 'A-Za-z0-9' | head -c 40)
printf 'POSTGRES_USER=%s\nPOSTGRES_PASSWORD=%s\nPOSTGRES_DB=sts\n' "$USERNAME" "$PW" > data/postgres/postgres.env
printf 'STS_DATABASE_URL=postgresql://%s:%s@127.0.0.1:5432/sts\n' "$USERNAME" "$PW" >> .env
openssl req -x509 -newkey rsa:4096 -nodes -sha256 -days 3650 -keyout data/postgres/tls/server.key -out data/postgres/tls/server.crt \
  -subj "/CN=<host>" -addext "subjectAltName=IP:<host ip>,DNS:<host name>"
chmod 600 data/postgres/tls/server.key
sudo chown -R 70:70 data/postgres/data data/postgres/tls
sudo cp deploy/postgres/sts-postgres.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now sts-postgres
```

The user name and password are read once, on the first start with an empty data directory; changing them later
means `ALTER ROLE`, or wiping the directory while it is empty.

## Load the runs

From the app image on the host, which carries the importer:

```bash
sudo docker run --rm --network host --user 1000:1000 --env-file .env \
  -v /home/ubuntu/maritime-sts-monitor/data/app/runs:/app/data/runs:ro --entrypoint python maritime-sts-app \
  -m maritime_tip_cue.pgimport import --runs-root /app/data/runs
```

Add `verify` instead of `import` to compare every run with its source. A run that is already in Postgres is skipped,
so re-running never overwrites reviews saved there; `--replace --run N` re-imports one run on purpose.

## Backup

```bash
sudo docker exec sts-postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d sts -Fc' > ~/sts-$(date +%Y%m%d).dump
```
