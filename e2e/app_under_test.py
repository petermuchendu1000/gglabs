#!/usr/bin/env python3
"""End-to-end harness: the REAL app, with only the third-party boundaries doubled.

What is real here: Flask, auth.py's gate, every api_v2 route, app.html as
shipped, the job runner and its stdout tee, logging_setup, run_pipeline's
register_company_domain, namecheap_automation's own XML parsing, and a real
PostgreSQL 16 database created by the real db.py.

What is doubled, and nothing else: the HTTP boundary to Namecheap
(namecheap_automation._api_call), to Companies House (run_pipeline.ch_get /
ch_get_raw and api_v2._ch_session), to D&B (run_pipeline.get_duns_number), and
the email pool. No request leaves this container.

The outcome of the next Namecheap order is read from the mode file on every
call, so the driver can walk the stage through each of its real outcomes.
"""
import json
import os
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.environ.get("E2E_REPO") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODE_FILE = os.environ.get("E2E_MODE_FILE") or os.path.join(HERE, ".e2e_mode")

if not os.environ.get("DATABASE_URL"):
    raise SystemExit("DATABASE_URL must be set; run_e2e.py sets it")
if not os.environ.get("APP_AUTH_TOKEN"):
    raise SystemExit("APP_AUTH_TOKEN must be set; run_e2e.py generates one")
os.environ.setdefault("FLASK_SECRET_KEY", os.urandom(16).hex())
os.environ.setdefault("ALLOW_INSECURE_COOKIE", "1")       # plain http on localhost
os.environ.setdefault("COMPANIES_HOUSE_API_KEY", "e2e-stub-key")
os.environ.setdefault("NAMECHEAP_API_USER", "e2e")
os.environ.setdefault("NAMECHEAP_API_KEY", "e2e")
os.environ.setdefault("NAMECHEAP_USERNAME", "e2e")
os.environ.setdefault("NAMECHEAP_CLIENT_IP", "127.0.0.1")

sys.path.insert(0, REPO)


def mode() -> str:
    try:
        with open(MODE_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "registered"


# ── Namecheap: double the HTTP transport, keep ALL the real parsing ────────
# Deliberately below _api_call, not at it. Stubbing _api_call would skip the
# very code that reads the provider's Status, error numbers and the
# DomainCreateResult/@Registered attribute - which is where this pipeline's
# money decisions are actually made.
NSU = "http://api.namecheap.com/xml.response"


def _namecheap_xml(command: str, p: dict) -> str:
    cmd = (command or "").replace("namecheap.", "")
    if cmd == "domains.check":
        first = (p.get("DomainList", "") or "x.online").split(",")[0]
        return (f'<?xml version="1.0" encoding="utf-8"?>'
                f'<ApiResponse Status="OK" xmlns="{NSU}"><CommandResponse>'
                f'<DomainCheckResult Domain="{first}" Available="true" '
                f'IsPremiumName="false" PremiumRegistrationPrice="0" IcannFee="0" />'
                f'</CommandResponse></ApiResponse>')
    if cmd == "domains.create":
        m = mode()
        dom = p.get("DomainName", "")
        if m == "refused":
            # 3050900 is a code Namecheap documents for domains.create
            # ("Unknown response from provider"). It is deliberately an
            # ambiguous one: it is the case where an automatic re-order would
            # be unsafe, and the pipeline must stop and say so.
            return (f'<?xml version="1.0" encoding="utf-8"?>'
                    f'<ApiResponse Status="ERROR" xmlns="{NSU}"><Errors>'
                    f'<Error Number="3050900">Unknown response from provider</Error>'
                    f'</Errors></ApiResponse>')
        registered = "false" if m == "unresolved" else "true"
        order = "o9" if m == "unresolved" else "o1"
        return (f'<?xml version="1.0" encoding="utf-8"?>'
                f'<ApiResponse Status="OK" xmlns="{NSU}"><CommandResponse>'
                f'<DomainCreateResult Domain="{dom}" Registered="{registered}" '
                f'ChargedAmount="8.88" DomainID="d1" OrderID="{order}" '
                f'WhoisguardEnable="true" /></CommandResponse></ApiResponse>')
    # Anything else (the account address profiles) answers OK with no nodes,
    # which makes register_company_domain take its Companies-House contact
    # fallback.
    return (f'<ApiResponse Status="OK" xmlns="{NSU}"><CommandResponse/>'
            f'</ApiResponse>')


class _FakeNamecheapHTTP:
    """Stands in for the `requests` module inside namecheap_automation."""

    def _call(self, url, params=None, data=None, timeout=None, **kw):
        body = _namecheap_xml((params or data or {}).get("Command", ""),
                              params or data or {})

        class _R:
            text = body
            status_code = 200

            @staticmethod
            def raise_for_status():
                return None

        return _R()

    get = _call
    post = _call


# ── Companies House / D&B doubles ──────────────────────────────────────────
OFFICERS = {"items": [
    {"name": "WANJIKU, Grace", "nationality": "Kenyan", "officer_role": "director",
     "appointed_on": "2021-03-02", "date_of_birth": {"month": 4, "year": 1985},
     "resigned_on": None},
    {"name": "SMITH, Johnathan Archibald", "nationality": "British",
     "officer_role": "director", "appointed_on": "2021-03-02",
     "date_of_birth": {"month": 7, "year": 1979}, "resigned_on": None},
]}
PROFILE = {
    "company_name": "TAMU TRADERS LTD", "company_number": "14123456",
    "company_status": "active", "type": "ltd", "date_of_creation": "2021-03-01",
    "sic_codes": ["6201"],
    "registered_office_address": {"address_line_1": "1 Example Street",
                                  "locality": "London", "postal_code": "EC1A 1BB"},
}


class _FakeResp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status
        self.headers = {"X-Ratelimit-Remain": "590"}

    def json(self):
        return self._p


class _FakeSession:
    """Stands in for api_v2._ch_session (the admission check and autofill)."""

    def get(self, url, **kw):
        if "/officers" in url:
            return _FakeResp(OFFICERS)
        return _FakeResp(PROFILE)


def fake_ch_get(path, params=None):
    if "/search/companies" in path:
        # Echo the searched number straight back as the one hit, so the Add
        # dialog's real Companies-House fallback has something to offer and
        # the real Kenyan-director admission check runs on it.
        q = path.split("q=", 1)[1].split("&", 1)[0] if "q=" in path else "14123456"
        return {"total_results": 1, "items": [{
            "company_number": q, "title": "TAMU TRADERS LTD",
            "company_status": "active", "date_of_creation": "2021-03-01"}]}
    if "/officers" in path:
        return OFFICERS
    if "/filing-history" in path:
        if mode() == "nocert":
            return {"items": [{"category": "accounts", "description": "accounts",
                               "type": "AA", "links": {}}]}
        return {"items": [{"category": "incorporation", "description": "incorporation",
                           "type": "NEWINC",
                           "links": {"document_metadata": "/document/abc"}}]}
    return PROFILE


class _FakePdf:
    status_code = 200

    @staticmethod
    def iter_content(chunk_size=8192):
        yield b"%PDF-1.4\n% end-to-end stub certificate\n%%EOF\n"


def fake_ch_get_raw(url, headers=None, stream=False):
    return _FakePdf()


def fake_duns(company_number, headless=True):
    return {"duns_number": "987654321", "status": "found",
            "company_name": "TAMU TRADERS LTD", "dnb_name": "TAMU TRADERS LTD",
            "dnb_address": "1 Example Street, London, EC1A 1BB"}


class FakeEmailPool:
    _assigned: dict = {}

    def get_assigned_email(self, cn):
        got = self._assigned.get(cn)
        return got or ("", "", "")

    def assign_next(self, company_number="", company_name=""):
        who = ("acct41@example.invalid", "Ada", "Byron")
        self._assigned[company_number] = who
        return who


# ── install the doubles, then import the real app ──────────────────────────
import namecheap_automation
import run_pipeline
import email_pool

namecheap_automation.requests = _FakeNamecheapHTTP()
run_pipeline.ch_get = fake_ch_get
run_pipeline.ch_get_raw = fake_ch_get_raw
run_pipeline.get_duns_number = fake_duns
email_pool.EmailPool = FakeEmailPool

from app import app          # the real application, gate and all
import api_v2

api_v2._ch_session = _FakeSession()
# api_v2's details branch imports from run_pipeline at call time, so the
# doubles above are what it gets. pipeline.py's director_id/app stages are
# left alone: they are not part of this verification and are not run.

if __name__ == "__main__":
    print("e2e: doubles installed for Namecheap, Companies House, D&B, email pool",
          flush=True)
    app.run(host="127.0.0.1", port=int(os.environ.get("E2E_PORT", "5000")),
            threaded=True, debug=False, use_reloader=False)
