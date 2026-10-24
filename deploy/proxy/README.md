# HTTPS reverse proxy

Caddy runs on the control host with the host network and routes by hostname: `APP_DOMAIN` goes to the STS app on
`127.0.0.1:8010`, `VIEWER_DOMAIN` to the viewer (a placeholder until the viewer exists). Caddy gets and renews the
Let's Encrypt certificates itself over HTTP-01, so ports 80 and 443 must reach the host. Certificates and account keys
live in `data/proxy/` (ignored by Git).

## Prerequisites

1. A static public IP attached to the Lightsail instance.
2. DNS-only A records (not proxied through Cloudflare) for both names, pointing at that IP.
3. Lightsail firewall: TCP 80 and 443 open. Close 8010 once the proxy works.

## Install

```bash
cd /home/ubuntu/maritime-sts-monitor
cp deploy/proxy/proxy.env.example deploy/proxy/proxy.env   # set APP_DOMAIN, VIEWER_DOMAIN
bash deploy/proxy/check-dns.sh                            # both names must resolve to this host (asks a public resolver, not the host's cached one)
mkdir -p data/proxy/data data/proxy/config
sudo cp deploy/proxy/sts-proxy.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now sts-proxy
```

Try the staging CA first (`ACME_CA=https://acme-staging-v02.api.letsencrypt.org/directory`) to avoid production rate
limits while testing; switch to `https://acme-v02.api.letsencrypt.org/directory`, remove `data/proxy/data` and restart
for the real certificate. After the proxy works, set `STS_APP_HOST=127.0.0.1` in `deploy/app/sts-app.service` so the
app is reachable only through Caddy, and restart `sts-app`.

The app's login stays on: HTTPS protects the password in transit, the login decides who gets in. The app's lockout uses
the client address Caddy forwards in `X-Forwarded-For`, trusted only from the loopback peer.
