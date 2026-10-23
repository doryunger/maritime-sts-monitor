import base64
import hmac
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import Response

MAX_FAILURES = 5
LOCKOUT_SECONDS = 300
LOOPBACK = ("127.0.0.1", "::1", "localhost")
_failures: dict[str, list[float]] = {}


def credentials() -> tuple[str, str] | None:
    password = os.environ.get("STS_APP_PASSWORD")
    if not password:
        return None
    return os.environ.get("STS_APP_USER") or "sts", password


def check_exposure(host: str) -> None:
    if host not in LOOPBACK and credentials() is None:
        raise SystemExit("Refusing to listen on a non-loopback address without STS_APP_PASSWORD set")


def client_address(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for")
    if peer in LOOPBACK and forwarded:
        return forwarded.split(",")[0].strip() or peer
    return peer


def recent_failures(client: str, now: float) -> list[float]:
    kept = [t for t in _failures.get(client, []) if now - t < LOCKOUT_SECONDS]
    _failures[client] = kept
    return kept


def authorised(header: str | None, expected: tuple[str, str]) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        user, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except Exception:
        return False
    return hmac.compare_digest(user.encode(), expected[0].encode()) & hmac.compare_digest(password.encode(), expected[1].encode())


def install(app: FastAPI) -> None:
    expected = credentials()
    if expected is None:
        return

    @app.middleware("http")
    async def guard(request: Request, call_next):
        client = client_address(request)
        now = time.time()
        if len(recent_failures(client, now)) >= MAX_FAILURES:
            return Response("Too many failed attempts; try again later.", status_code=429, headers={"Retry-After": str(LOCKOUT_SECONDS)})
        if authorised(request.headers.get("authorization"), expected):
            return await call_next(request)
        if request.headers.get("authorization"):
            _failures.setdefault(client, []).append(now)
        return Response("Authentication required", status_code=401, headers={"WWW-Authenticate": 'Basic realm="STS monitor"'})
