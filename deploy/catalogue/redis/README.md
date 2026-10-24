# Redis task queue host

Install Ubuntu's Redis server on the dedicated Lightsail AIS/control host. Use the instance's private
address, reachable from the designated EC2 instance over the VPC peering/private-network route. Do not
open TCP 6379 to the public internet.

Create `/etc/redis/sts-redis.env` with mode `0600` using `sts-redis.env.example` as a template. Set
`REDIS_BIND_ADDRESS` to the Ubuntu host's private address and `STS_REDIS_PASSWORD` to a generated secret.
Install `redis.conf` as
`/etc/redis/sts-redis.conf`; the systemd unit adds the private address to Redis's localhost bind. Render
`users.acl.template` with the same secret and install
as `/etc/redis/users.acl`, owned by `redis:redis`, mode `0600`. Install `sts-redis.service` in
`/etc/systemd/system/`, then reload systemd and enable the service. Redis uses RDB and AOF persistence and
`noeviction` so queue state is not discarded under memory pressure.

Allow inbound TCP 6379 in both the Ubuntu firewall and the Lightsail networking firewall only from the
EC2 private IP or the narrowest shared private subnet. Permit the corresponding outbound connection from
EC2. If EC2 is in a different VPC, configure private routing first; a public Redis listener is not the
fallback. Verify from EC2 with `redis-cli -u "$STS_REDIS_URL" PING` and verify its route to the private
host before enabling the catalogue migration.

On both tracker and EC2, add `STS_REDIS_URL=redis://sts:<password>@<private-ip>:6379/0` to the protected
`.env`; keep the actual endpoint and secret out of Git. Install the Redis Python requirement from
`deploy/catalogue/requirements.txt`. Queue operations use the `sts:tasks` key prefix by default.

The tracker SQLite database remains the source for discovery and durable job history. Redis is the active
batch. The six-hour catalogue scan discovers jobs; a separate always-on job manager dispatches SQLite jobs,
consumes Redis Stream claim, result and lease-expiry events, and updates SQLite after validating receipts and
successful S3 artifacts. Workers atomically claim jobs and renew leases with heartbeats. Result publication is
fenced by attempt identity. Failed or expired attempts receive one delayed retry; another failure is terminal.
The manager recovers pending, retry-waiting and terminal Redis state from SQLite each cycle. A task whose
worker stops heartbeating is recovered after its configured lease expires; do not manually requeue active jobs.
The `sts` ACL user must allow `MULTI` and `EXEC` as well as Streams and scripting commands because the manager
uses Redis transactions when it synchronizes retry and terminal state.

Redis 7.0.15 was installed on the dedicated Ubuntu host on 2026-10-21, with the `sts-redis.service`
systemd unit enabled, an ACL credential stored outside the repository, RDB/AOF persistence, and a listener
on localhost plus the host's private interface. Local authenticated `PING` succeeded. The Redis credential
was rotated after setup and stays in protected environment files. The Redis-aware tracker was deployed on
2026-10-21 and its normal scan authenticated locally, reconciled two succeeded records, and published eight
post-baseline jobs as pending. The baseline was established on 2026-10-20; this migration did not backfill
older imagery. The previous S3 queue records remain in place until worker and restart validation is complete.

EC2 private connectivity was verified on 2026-10-21 from the designated worker. Authenticated `PING` and
`SET`/`GET`/`DEL` succeeded using a temporary key under the ACL-allowed `sts:tasks:*` prefix. A key outside
that prefix was denied by ACL. Keep Redis private; no public listener or ingress was added. The development
PC has no private route to Redis; its local app uses an SSH tunnel through the control host. The EC2 worker
still needs the Redis-aware code and protected Redis URL deployed before it can consume these jobs. No EC2
wake or processing was started during tracker migration.

The always-on `sts-job-manager.service` and Redis Streams ACL permission were installed on Ubuntu on
2026-10-21. Redis restarted successfully with no in-flight jobs, the manager created its consumer group, and
its first cycle completed without errors: eight jobs remain dispatched in SQLite and two remain succeeded.
The service only reconciles jobs; it does not start or stop EC2.

An isolated EC2 queue canary passed on 2026-10-22 using the existing managed wake controller. The test kept
the controller's idle timer refreshed while active, claimed a synthetic task, rejected a duplicate claim,
renewed its heartbeat, expired and recovered one lease, allowed exactly one retry, and finalized an intentional
second-attempt failure in SQLite and Redis. No production job was claimed and no scene was processed. The
worker checkout currently on EC2 is the unrelated geo-dataset-generator application, so the STS worker image
and detector runtime still need deployment before a real scene can be processed. See
[EC2 lifecycle test procedure](../../../maritime_tip_cue/context/watch.md#ec2-lifecycle-reference).
