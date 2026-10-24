import base64

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.backend import auth


def make(monkeypatch, password="s3cret", user="sts", peer="testclient"):
    auth._failures.clear()
    monkeypatch.delenv("STS_APP_PASSWORD", raising=False)
    monkeypatch.delenv("STS_APP_USER", raising=False)
    if password:
        monkeypatch.setenv("STS_APP_PASSWORD", password)
    if user:
        monkeypatch.setenv("STS_APP_USER", user)
    app = FastAPI()
    auth.install(app)

    @app.get("/api/ping")
    def ping():
        return {"ok": True}

    return TestClient(app, client=(peer, 50000))


def basic(user, password):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def test_no_password_configured_leaves_the_app_open(monkeypatch):
    assert make(monkeypatch, password=None).get("/api/ping").status_code == 200


def test_requests_without_or_with_wrong_credentials_are_refused(monkeypatch):
    client = make(monkeypatch)
    assert client.get("/api/ping").status_code == 401
    assert client.get("/api/ping").headers["www-authenticate"].startswith("Basic")
    assert client.get("/api/ping", headers=basic("sts", "wrong")).status_code == 401
    assert client.get("/api/ping", headers=basic("other", "s3cret")).status_code == 401


def test_correct_credentials_are_accepted(monkeypatch):
    assert make(monkeypatch).get("/api/ping", headers=basic("sts", "s3cret")).json() == {"ok": True}


def test_password_may_contain_a_colon(monkeypatch):
    assert make(monkeypatch, password="a:b:c").get("/api/ping", headers=basic("sts", "a:b:c")).status_code == 200


def test_repeated_failures_lock_the_client_out_even_for_the_right_password(monkeypatch):
    client = make(monkeypatch)
    for _ in range(auth.MAX_FAILURES):
        assert client.get("/api/ping", headers=basic("sts", "nope")).status_code == 401
    assert client.get("/api/ping", headers=basic("sts", "s3cret")).status_code == 429


def test_malformed_authorization_headers_do_not_crash(monkeypatch):
    client = make(monkeypatch)
    assert client.get("/api/ping", headers={"Authorization": "Basic !!!notbase64"}).status_code == 401
    assert client.get("/api/ping", headers={"Authorization": "Bearer abc"}).status_code == 401


def test_public_binding_without_a_password_is_refused(monkeypatch):
    monkeypatch.delenv("STS_APP_PASSWORD", raising=False)
    with pytest.raises(SystemExit):
        auth.check_exposure("0.0.0.0")
    auth.check_exposure("127.0.0.1")
    monkeypatch.setenv("STS_APP_PASSWORD", "x")
    auth.check_exposure("0.0.0.0")


def test_lockout_is_per_real_client_behind_the_proxy(monkeypatch):
    client = make(monkeypatch, peer="127.0.0.1")
    for _ in range(auth.MAX_FAILURES):
        client.get("/api/ping", headers={**basic("sts", "nope"), "X-Forwarded-For": "203.0.113.9"})
    blocked = client.get("/api/ping", headers={**basic("sts", "s3cret"), "X-Forwarded-For": "203.0.113.9"})
    other = client.get("/api/ping", headers={**basic("sts", "s3cret"), "X-Forwarded-For": "198.51.100.4"})
    assert blocked.status_code == 429 and other.status_code == 200


def test_forwarded_address_from_an_untrusted_peer_is_ignored(monkeypatch):
    client = make(monkeypatch, peer="203.0.113.50")
    for index in range(auth.MAX_FAILURES):
        client.get("/api/ping", headers={**basic("sts", "nope"), "X-Forwarded-For": f"10.0.0.{index}"})
    assert client.get("/api/ping", headers={**basic("sts", "s3cret"), "X-Forwarded-For": "10.9.9.9"}).status_code == 429
