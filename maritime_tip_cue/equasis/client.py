import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from maritime_tip_cue.config import ROOT, settings

BASE = "https://www.equasis.org/EquasisWeb/restricted"
HOME = "https://www.equasis.org/EquasisWeb/public/HomePage"
LOGIN = "https://www.equasis.org/EquasisWeb/authen/HomePage?fs=HomePage"
USER_AGENT = "Mozilla/5.0 (X11; Ubuntu; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}
FORM_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://www.equasis.org",
}
DETAIL_KEYS = {
    "Call Sign": "call_sign",
    "MMSI": "mmsi",
    "Gross tonnage": "gross_tonnage",
    "DWT": "dwt",
    "Type of ship": "type",
    "Year of build": "year_built",
    "Status": "status",
}


class SessionExpired(RuntimeError):
    pass


class NotConfigured(RuntimeError):
    pass


def credential(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip().strip("\"'") if value and value.strip() else None


def configured() -> bool:
    return bool((credential("EQUASIS_EMAIL") and credential("EQUASIS_PASSWORD")) or credential("EQUASIS_JSESSIONID"))


def cache_path() -> Path:
    path = Path(settings()["equasis"]["cache"])
    return path if path.is_absolute() else ROOT / path


class Equasis:
    def __init__(self, delay_s: float, path: Path | None = None, clock=time.monotonic, sleep=time.sleep, log=None):
        self.http = requests.Session()
        self.http.headers.update(BROWSER_HEADERS)
        self.session_ready = False
        self.delay_s = delay_s
        self.last = 0.0
        self.previous_end = None
        self.log = log
        self.clock = clock
        self.sleep = sleep
        self.path = path or cache_path()
        self.cache = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {"search": {}, "details": {}}

    def emit(self, kind: str, query: str | None, outcome: str, status: int | None, elapsed_s: float, gap_s: float | None, error: str | None) -> None:
        if self.log is None:
            return
        try:
            self.log({
                "kind": kind, "query": query, "outcome": outcome, "http_status": status, "elapsed_ms": round(elapsed_s * 1000),
                "gap_s": gap_s, "delay_s": self.delay_s, "error": error,
            })
        except Exception:
            pass

    def request(self, method: str, url: str, kind: str = "request", query: str | None = None, **kwargs) -> requests.Response:
        started = self.clock()
        gap_s = None if self.previous_end is None else started - self.previous_end
        status, error = None, None
        try:
            try:
                response = self.http.request(method, url, **kwargs)
            except requests.ConnectionError:
                self.http.close()
                response = self.http.request(method, url, **kwargs)
            status = response.status_code
            return response
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self.previous_end = self.clock()
            outcome = "error" if error else "ok" if status < 400 else "http_error"
            self.emit(kind, query, outcome, status, self.previous_end - started, gap_s, error)

    def wait(self) -> None:
        remaining = self.delay_s - (self.clock() - self.last)
        if remaining > 0:
            self.sleep(remaining)

    def open_session(self) -> None:
        email, password = credential("EQUASIS_EMAIL"), credential("EQUASIS_PASSWORD")
        if not (email and password):
            pasted = credential("EQUASIS_JSESSIONID")
            if not pasted:
                raise NotConfigured("EQUASIS_EMAIL and EQUASIS_PASSWORD are not set")
            self.http.cookies.set("JSESSIONID", pasted, domain="www.equasis.org", path="/EquasisWeb")
            self.session_ready = True
            return
        self.http.cookies.clear()
        self.wait()
        response = self.request("GET", HOME, "home", timeout=60)
        response.raise_for_status()
        if "JSESSIONID" not in self.http.cookies:
            raise SessionExpired("Equasis home page did not set a JSESSIONID cookie")
        self.wait()
        response = self.request(
            "POST", LOGIN, "login", data={"j_email": email, "j_password": password, "submit": "Login"}, timeout=60,
            headers={**FORM_HEADERS, "Referer": HOME},
        )
        self.last = self.clock()
        response.raise_for_status()
        if "j_password" in response.text:
            raise SessionExpired("Equasis login failed")
        self.session_ready = True

    def post(self, data: dict, retry: bool = True) -> BeautifulSoup:
        if not self.session_ready:
            self.open_session()
        self.wait()
        kind = "details" if "P_IMO" in data else "search"
        response = self.request(
            "POST", f"{BASE}/ShipInfo?fs=Search", kind, data.get("P_IMO") or data.get("P_ENTREE"), data=data, timeout=60,
            headers={**FORM_HEADERS, "Referer": f"{BASE}/Search?fs=Search", "Sec-Fetch-Dest": "document",
                     "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "same-origin", "Sec-Fetch-User": "?1"},
        )
        self.last = self.clock()
        response.raise_for_status()
        if "j_password" in response.text or "/authen/" in response.url.lower():
            if retry and credential("EQUASIS_EMAIL") and credential("EQUASIS_PASSWORD"):
                self.session_ready = False
                return self.post(data, retry=False)
            raise SessionExpired("Equasis answered with its login form")
        return BeautifulSoup(response.text, "html.parser")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(self.cache, indent=1, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    def search(self, name: str, refresh: bool = False) -> list[dict]:
        key = name.strip().upper()
        if not refresh and key in self.cache["search"]:
            self.emit("search", key, "cache", None, 0.0, None, None)
            return self.cache["search"][key]["results"]
        soup = self.post({
            "P_PAGE": "1", "P_PAGE_COMP": "1", "P_PAGE_SHIP": "1", "ongletActifSC": "ship",
            "P_ENTREE_HOME_HIDDEN": name, "P_ENTREE": name, "Submit": "Advanced Search",
        })
        results = parse_search(soup)
        self.cache["search"][key] = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "results": results}
        self.save()
        return results

    def details(self, imo: str, refresh: bool = False) -> dict:
        if not refresh and imo in self.cache["details"]:
            self.emit("details", imo, "cache", None, 0.0, None, None)
            return self.cache["details"][imo]
        data = parse_details(self.post({"P_IMO": imo}), imo)
        self.cache["details"][imo] = data
        self.save()
        return data


def parse_search(soup: BeautifulSoup) -> list[dict]:
    results = []
    table = soup.find("table", class_="table-striped")
    body = table.find("tbody") if table else None
    for row in body.find_all("tr", class_="hidden-sm hidden-xs") if body else []:
        columns = row.find_all(["th", "td"])
        if len(columns) >= 6:
            results.append({
                "imo": columns[0].get_text(strip=True),
                "name": columns[1].get_text(strip=True),
                "gross_tonnage": columns[2].get_text(strip=True),
                "type": columns[3].get_text(strip=True),
                "year_built": columns[4].get_text(strip=True),
                "flag": " ".join(columns[5].get_text().split()),
            })
    return results


def parse_details(soup: BeautifulSoup, imo: str) -> dict:
    data = {"imo": imo, "at": datetime.now(UTC).isoformat(timespec="seconds")}
    for row in soup.find_all("div", class_="row"):
        columns = row.find_all("div", recursive=False)
        label = columns[0].find("b") if len(columns) >= 2 else None
        if not label:
            continue
        text = label.get_text(strip=True).lower()
        for title, key in DETAIL_KEYS.items():
            if title.lower() in text and key not in data:
                data[key] = columns[1].get_text(strip=True)
                note = columns[3].get_text(strip=True) if len(columns) >= 4 else ""
                if note:
                    data[f"{key}_note"] = note
    return data
