#!/usr/bin/env python3
"""Cloudflare delegation: the four ways it could silently not work.

    python3 test_cloudflare_dns.py

Hermetic: no network, no database, no registrar, no browser. The double sits
at the `requests` layer, so every line of cloudflare_dns's own envelope
parsing, error-code extraction, status comparison and guard logic really
runs - only the socket is fake.

The zone fixtures are not invented. They are the field set a LIVE zone on
this account actually returns (evidence CF-DELEG-01 EV-008), including the
two details the published schema gets wrong: a real full zone carries
'vanity_name_servers_ips', and carries no 'verification_key' or
'cname_suffix' key at all. A fixture built from the documentation would have
let through code that reads a field the API does not send.

The four defects under test, each of which would look like success:

 1. HARDCODED NAMESERVERS. Cloudflare assigns the pair per zone, and rotates
    which pair it hands out. On this account every zone created up to
    2026-10-10 got lauryn/maciej and every zone created from 2026-10-13 got
    houston/serena - a clean cut with no interleaving, and no announcement.
    A fixed pair is therefore correct until the next rotation and silently
    wrong after it: the domain is delegated to nameservers that do not serve
    its zone, nothing errors, and the zone just stays pending for ever.
 2. POLLING THE ACTIVATION CHECK. PUT /activation_check is capped at once an
    hour on a Free zone. A poll loop built on it reports a healthy
    delegation as broken.
 3. AN UNGUARDED MUTATION. The credential reaches all 26 live zones. A stale
    zone id must fail, not rewrite production DNS for another domain.
 4. ENABLED READ AS READY, AND A TIMEOUT READ AS A FAILURE. Email Routing
    reports enabled and status separately, so enabled=True with
    status="misconfigured" is a real state; and "we stopped waiting" says
    nothing about whether the delegation will come up.
"""
import ast
import os
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

import cloudflare_dns as cf

# Deliberately NOT the real account id. Nothing here needs it: the account id
# is only ever passed through to the request body, so any 32-character value
# exercises the same path, and the real one does not belong in a fixture that
# gets committed.
ACCOUNT_ID = "00000000000000000000000000000000"
ZONE_ID = "9a1b2c3d4e5f60718293a4b5c6d7e8f9"
OTHER_ZONE_ID = "ffffffffffffffffffffffffffffffff"

# The two pairs that really exist on this account, named by WHEN Cloudflare
# handed them out rather than by how many zones carry them: PAIR_RETIRED went
# to every zone created up to 2026-10-10, PAIR_CURRENT to every zone created
# from 2026-10-13. Neither is ever written into cloudflare_dns.py - that is
# what check_no_hardcoded_nameservers enforces, and a test that used only one
# of them would pass just as well against code that hardcoded that one.
PAIR_RETIRED = ["lauryn.ns.cloudflare.com", "maciej.ns.cloudflare.com"]
PAIR_CURRENT = ["houston.ns.cloudflare.com", "serena.ns.cloudflare.com"]
# What Namecheap's own nameservers look like, for the rejection test.
NAMECHEAP_NS = ["dns1.registrar-servers.com", "dns2.registrar-servers.com"]


def zone_fixture(domain="simon-mwangi.site", status="active",
                 name_servers=None, zone_id=ZONE_ID):
    """A zone object with the field set a LIVE zone really returns (EV-008)."""
    return {
        "id": zone_id,
        "name": domain,
        "status": status,
        "type": "full",
        "paused": False,
        "development_mode": 0,
        "activated_on": "2026-08-17T02:45:18.167022Z",
        "created_on": "2026-08-17T02:30:00.000000Z",
        "modified_on": "2026-08-17T02:45:18.167022Z",
        "name_servers": PAIR_RETIRED if name_servers is None else name_servers,
        "original_name_servers": NAMECHEAP_NS,
        "original_registrar": "namecheap, inc. (id: 1068)",
        "original_dnshost": None,
        "account": {"id": ACCOUNT_ID, "name": "an account"},
        "meta": {"custom_certificate_quota": 0, "page_rule_quota": 3,
                 "phishing_detected": False, "step": 4},
        "owner": {"email": "redacted@example.invalid", "id": "x", "type": "user"},
        "permissions": ["#dns_records:edit"],
        "plan": {"id": "free", "name": "Free Website"},
        "tenant": {"id": None, "name": None},
        "tenant_unit": {"id": None},
        "vanity_name_servers": [],
        "vanity_name_servers_ips": [],
    }


def envelope(result, success=True, errors=None):
    return {"success": success, "errors": errors or [], "messages": [],
            "result": result}


class Boom(Exception):
    """Stands in for a socket-level failure."""


class FakeHTTP:
    """Records every call and answers from a route table.

    A route value may be a single (status, payload), a LIST consumed in order
    (for polling), or an Exception instance to raise. A call with no route is
    recorded and answered 599 so an UNEXPECTED request is loud rather than
    silently plausible.
    """

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []

    def request(self, method, url, headers=None, json=None, params=None,
                timeout=None):
        path = url.split("/client/v4", 1)[-1]
        key = "%s %s" % (method.upper(), path)
        self.calls.append({"key": key, "method": method.upper(), "path": path,
                           "json": json, "params": params,
                           "auth": (headers or {}).get("Authorization", "")})
        entry = self.routes.get(key)
        if isinstance(entry, list):
            entry = entry.pop(0) if entry else None
        if isinstance(entry, Exception):
            raise entry
        if entry is None:
            return FakeResponse(599, {"success": False, "errors": [
                {"code": 9999, "message": "no route for " + key}]})
        status, payload = entry
        return FakeResponse(status, payload)

    def keys(self):
        return [c["key"] for c in self.calls]

    def count(self, substring):
        return sum(1 for c in self.calls if substring in c["key"])


class FakeResponse:
    def __init__(self, status_code, payload, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else "{}"

    def json(self):
        if self._payload is _NON_JSON:
            raise ValueError("not json")
        return self._payload


_NON_JSON = object()


class Results:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []
        self.skipped: list[str] = []

    def check(self, name: str, cond: bool, detail: str = "") -> bool:
        if cond:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed.append(name)
            print(f"  FAIL  {name}" + (f"\n          {detail}" if detail else ""))
        return bool(cond)

    def skip(self, name: str, why: str) -> None:
        self.skipped.append(name)
        print(f"  SKIP  {name}\n          {why}")

    def report(self) -> int:
        print()
        print(f"{self.passed} passed / {len(self.failed)} failed / "
              f"{len(self.skipped)} skipped")
        for f in self.failed:
            print(f"  failed: {f}")
        return 1 if self.failed else 0


def with_http(routes):
    """Install a FakeHTTP as cloudflare_dns's requests module."""
    fake = FakeHTTP(routes)
    cf.requests = fake
    return fake


# ============================================================
# DEFECT 1 - the nameservers must come from Cloudflare, per zone
# ============================================================

def check_nameserver_validation(r: Results) -> None:
    print("\ndefect 1: a delegation target is only usable if Cloudflare gave it")

    ok = cf.validate_nameservers(PAIR_RETIRED)
    r.check("a real Cloudflare pair validates", ok == PAIR_RETIRED, repr(ok))

    ok2 = cf.validate_nameservers(PAIR_CURRENT)
    r.check("the OTHER real pair validates too, unchanged",
            ok2 == PAIR_CURRENT, repr(ok2))

    for bad, why in [
        ([], "an empty list"),
        (PAIR_RETIRED[:1], "a single nameserver"),
        (None, "None"),
        ("lauryn.ns.cloudflare.com", "a bare string"),
        (NAMECHEAP_NS, "Namecheap's own nameservers"),
        ([""], "a blank entry"),
        ([PAIR_RETIRED[0], ""], "one good and one blank"),
        ([PAIR_RETIRED[0], None], "one good and one None"),
        ([PAIR_RETIRED[0], PAIR_RETIRED[0]], "the same host twice"),
    ]:
        raised = False
        try:
            cf.validate_nameservers(bad)
        except ValueError:
            raised = True
        except Exception as exc:
            raised = False
            why += f" (raised {type(exc).__name__} instead of ValueError)"
        r.check(f"rejects {why}", raised,
                f"validate_nameservers({bad!r}) did not raise ValueError - the "
                f"pipeline would set this at the registrar")

    cleaned = cf.validate_nameservers(
        ["LAURYN.ns.cloudflare.com.", " maciej.ns.cloudflare.com "])
    r.check("normalises case, trailing dot and whitespace",
            cleaned == PAIR_RETIRED, repr(cleaned))


def check_create_zone_returns_what_cloudflare_sent(r: Results) -> None:
    print("\ndefect 1: create_zone reports the pair Cloudflare assigned, not a constant")
    os.environ["CLOUDFLARE_ACCOUNT_ID"] = ACCOUNT_ID

    for pair, label in [(PAIR_RETIRED, "retired pair"),
                        (PAIR_CURRENT, "current pair")]:
        fake = with_http({"POST /zones": (200, envelope(
            zone_fixture(status="pending", name_servers=pair)))})
        out = cf.create_zone("simon-mwangi.site")
        r.check(f"create_zone passes through the {label} verbatim",
                out.get("name_servers") == pair,
                f"asked for a zone and got {out.get('name_servers')!r}, "
                f"expected {pair!r}; out={out!r}")
        r.check(f"create_zone reports created=True for the {label}",
                out.get("created") is True, repr(out))

    # The unknown made safe: a pending zone whose POST carried no nameservers.
    fake = with_http({"POST /zones": (200, envelope(
        zone_fixture(status="pending", name_servers=[])))})
    out = cf.create_zone("simon-mwangi.site")
    r.check("a zone with NO nameservers is an error, not a success",
            "error" in out,
            f"create_zone returned {out!r} - the caller would delegate to nothing")
    r.check("...and it says the zone exists, so nobody creates it twice",
            out.get("state") == "zone_without_nameservers", repr(out))
    r.check("...and it carries the zone id it could not get nameservers for",
            out.get("zone_id") == ZONE_ID, repr(out))

    # A pair that is not Cloudflare's at all.
    fake = with_http({"POST /zones": (200, envelope(
        zone_fixture(status="pending", name_servers=NAMECHEAP_NS)))})
    out = cf.create_zone("simon-mwangi.site")
    r.check("non-Cloudflare nameservers in the response are refused",
            "error" in out, repr(out))

    # Cloudflare answering about a different domain.
    fake = with_http({"POST /zones": (200, envelope(
        zone_fixture(domain="someone-else.com", status="pending")))})
    out = cf.create_zone("simon-mwangi.site")
    r.check("a response about a DIFFERENT domain is refused",
            "error" in out and "refusing" in out["error"], repr(out))
    r.check("...and no nameservers are handed back", "name_servers" not in out,
            repr(out))


def check_no_hardcoded_nameservers(r: Results) -> None:
    print("\ndefect 1: no nameserver hostname is written into the module")
    path = os.path.join(REPO, "cloudflare_dns.py")
    tree = ast.parse(open(path, encoding="utf-8").read())

    # Collect string literals that are NOT docstrings. The module docstring
    # legitimately NAMES the real pairs to explain why they must not be
    # hardcoded, so a plain grep would fail on the explanation itself.
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", None) or []
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))

    offenders = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings):
            text = node.value.lower()
            if "ns.cloudflare.com" in text and text != cf.CF_NS_SUFFIX:
                offenders.append((getattr(node, "lineno", "?"), node.value))
    r.check("no executable string names a specific Cloudflare nameserver",
            not offenders,
            f"found {offenders!r} - a fixed pair is correct only until "
            f"Cloudflare next rotates the one it assigns, and this account has "
            f"been rotated once already")
    r.check("the suffix constant is the only nameserver literal",
            cf.CF_NS_SUFFIX == ".ns.cloudflare.com", repr(cf.CF_NS_SUFFIX))


# ============================================================
# DEFECT 2 - waiting must not poll the rate-limited endpoint
# ============================================================

def check_wait_polls_the_zone_not_the_activation_check(r: Results) -> None:
    print("\ndefect 2: waiting polls the zone; activation_check is capped at 1/hour")

    # Already active: answer at once, no sleeping, no PUT.
    fake = with_http({"GET /zones/%s" % ZONE_ID: (200, envelope(zone_fixture()))})
    slept = []
    out = cf.wait_until_active(ZONE_ID, "simon-mwangi.site",
                               _sleep=slept.append, _now=_clock())
    r.check("an already-active zone returns active", out.get("active") is True,
            repr(out))
    r.check("...without sleeping", slept == [], repr(slept))
    r.check("...and without triggering an activation check",
            fake.count("activation_check") == 0, repr(fake.keys()))

    # Pending, then pending, then active.
    key = "GET /zones/%s" % ZONE_ID
    fake = with_http({key: [
        (200, envelope(zone_fixture(status="pending", name_servers=[]))),
        (200, envelope(zone_fixture(status="pending", name_servers=[]))),
        (200, envelope(zone_fixture(status="active"))),
    ]})
    slept = []
    out = cf.wait_until_active(ZONE_ID, "simon-mwangi.site", timeout=600,
                               poll_interval=15, _sleep=slept.append,
                               _now=_clock())
    r.check("a zone that flips to active is reported active",
            out.get("active") is True, repr(out))
    r.check("...after exactly the polls it needed", out.get("polls") == 3,
            repr(out))
    r.check("...having slept between them", len(slept) == 2, repr(slept))
    r.check("...and STILL never triggered an activation check",
            fake.count("activation_check") == 0, repr(fake.keys()))

    # Never activates.
    fake = with_http({key: (200, envelope(
        zone_fixture(status="pending", name_servers=[])))})
    slept = []
    out = cf.wait_until_active(ZONE_ID, "simon-mwangi.site", timeout=60,
                               poll_interval=15, _sleep=slept.append,
                               _now=_clock())
    r.check("a zone that never activates is NOT reported active",
            out.get("active") is False, repr(out))
    r.check("...it is reported as timed out", out.get("timed_out") is True,
            repr(out))
    r.check("...and the message names the status it was stuck on",
            "pending" in str(out.get("message", "")), repr(out))
    r.check("...and it never triggered an activation check either",
            fake.count("activation_check") == 0, repr(fake.keys()))
    r.check("...and it did not spin without bound",
            len(slept) <= 5, f"slept {len(slept)} times for a 60s timeout")

    # Cloudflare stops answering mid-wait.
    fake = with_http({key: [
        (200, envelope(zone_fixture(status="pending", name_servers=[]))),
        Boom("connection reset"),
    ]})
    out = cf.wait_until_active(ZONE_ID, "simon-mwangi.site", timeout=600,
                               poll_interval=1, _sleep=lambda s: None,
                               _now=_clock())
    r.check("a transport failure mid-wait is an error, not 'not active'",
            "error" in out, repr(out))
    r.check("...and it does not claim the zone timed out",
            out.get("timed_out") is not True, repr(out))


def _clock():
    """A monotonic clock that advances 15s per read, so the loop terminates."""
    state = {"t": 0.0}

    def now():
        state["t"] += 15.0
        return state["t"]
    return now


def check_activation_check_is_separate_and_guarded(r: Results) -> None:
    print("\ndefect 2: the activation check exists, separately, and is guarded")
    fake = with_http({
        "GET /zones/%s" % ZONE_ID: (200, envelope(
            zone_fixture(status="pending", name_servers=[]))),
        "PUT /zones/%s/activation_check" % ZONE_ID: (200, envelope({"id": ZONE_ID})),
    })
    out = cf.trigger_activation_check(ZONE_ID, "simon-mwangi.site")
    r.check("trigger_activation_check uses PUT", fake.count("PUT ") == 1,
            repr(fake.keys()))
    r.check("...on the activation_check path",
            fake.count("activation_check") == 1, repr(fake.keys()))
    r.check("...and reports it triggered", out.get("triggered") is True, repr(out))


# ============================================================
# DEFECT 3 - every mutation is guarded by the zone's own name
# ============================================================

def check_mutations_are_guarded_by_zone_name(r: Results) -> None:
    print("\ndefect 3: a zone id that is not the named domain must not be written")

    # The zone exists but is somebody else's domain.
    routes = {"GET /zones/%s" % ZONE_ID: (200, envelope(
        zone_fixture(domain="paidpesa.com")))}

    cases = [
        ("add_txt_record",
         lambda: cf.add_txt_record(ZONE_ID, "simon-mwangi.site", "@", "v=x")),
        ("enable_email_routing",
         lambda: cf.enable_email_routing(ZONE_ID, "simon-mwangi.site")),
        ("trigger_activation_check",
         lambda: cf.trigger_activation_check(ZONE_ID, "simon-mwangi.site")),
        ("email_routing_status",
         lambda: cf.email_routing_status(ZONE_ID, "simon-mwangi.site")),
        ("list_txt_records",
         lambda: cf.list_txt_records(ZONE_ID, "simon-mwangi.site")),
        ("list_routing_rules",
         lambda: cf.list_routing_rules(ZONE_ID, "simon-mwangi.site")),
    ]
    for label, call in cases:
        fake = with_http(dict(routes))
        out = call()
        r.check(f"{label} refuses a zone whose name is another domain",
                isinstance(out, dict) and "error" in out, repr(out))
        r.check(f"...{label} names both domains in the refusal",
                "paidpesa.com" in str(out.get("error", ""))
                and "simon-mwangi.site" in str(out.get("error", "")),
                repr(out))
        writes = [c["key"] for c in fake.calls
                  if c["method"] in ("POST", "PUT", "PATCH", "DELETE")]
        r.check(f"...and {label} issued NO write to Cloudflare", writes == [],
                f"it sent {writes!r} before checking whose zone it was")

    # A blank expected_domain must not even reach the network.
    fake = with_http({})
    out = cf.add_txt_record(ZONE_ID, "", "@", "v=x")
    r.check("a blank expected_domain is refused", "error" in out, repr(out))
    r.check("...with no HTTP call at all", fake.calls == [], repr(fake.keys()))

    # And the happy path does go through.
    fake = with_http({
        "GET /zones/%s" % ZONE_ID: (200, envelope(zone_fixture())),
        "POST /zones/%s/dns_records" % ZONE_ID: (200, envelope(
            {"id": "rec1", "name": "simon-mwangi.site",
             "content": "google-site-verification=abc", "ttl": 1})),
    })
    out = cf.add_txt_record(ZONE_ID, "simon-mwangi.site", "@",
                            "google-site-verification=abc")
    r.check("a matching zone IS written", out.get("status") == "configured",
            repr(out))
    r.check("...and the record id comes back", out.get("record_id") == "rec1",
            repr(out))


# ============================================================
# DEFECT 4 - enabled is not ready
# ============================================================

def check_email_routing_enabled_is_not_ready(r: Results) -> None:
    print("\ndefect 4: Email Routing is only configured when status is 'ready'")
    zone_get = "GET /zones/%s" % ZONE_ID
    enable = "POST /zones/%s/email/routing/dns" % ZONE_ID
    settings = "GET /zones/%s/email/routing" % ZONE_ID

    fake = with_http({
        zone_get: (200, envelope(zone_fixture())),
        enable: (200, envelope({"id": "er1", "enabled": True, "status": "ready",
                                "name": "simon-mwangi.site"})),
        settings: (200, envelope({"id": "er1", "enabled": True,
                                  "status": "ready",
                                  "name": "simon-mwangi.site"})),
    })
    out = cf.enable_email_routing(ZONE_ID, "simon-mwangi.site")
    r.check("status ready -> configured", out.get("result") == "configured",
            repr(out))
    r.check("...ready is True", out.get("ready") is True, repr(out))

    for bad_status in ["unconfigured", "misconfigured", "misconfigured/locked",
                       "unlocked"]:
        fake = with_http({
            zone_get: (200, envelope(zone_fixture())),
            enable: (200, envelope({"id": "er1", "enabled": True,
                                    "status": bad_status})),
            settings: (200, envelope({"id": "er1", "enabled": True,
                                      "status": bad_status})),
        })
        out = cf.enable_email_routing(ZONE_ID, "simon-mwangi.site")
        r.check(f"enabled=True but status={bad_status!r} is NOT configured",
                out.get("result") == "enabled_not_ready",
                f"reported {out.get('result')!r}; a half-configured zone would "
                f"look like a working mailbox. out={out!r}")
        r.check(f"...ready is False for {bad_status!r}",
                out.get("ready") is False, repr(out))
        r.check(f"...and the real status {bad_status!r} is in the message",
                bad_status in str(out.get("message", "")), repr(out))

    # An undocumented status must be visible as unknown, not silently false.
    fake = with_http({
        zone_get: (200, envelope(zone_fixture())),
        settings: (200, envelope({"id": "er1", "enabled": True,
                                  "status": "something_new"})),
    })
    out = cf.email_routing_status(ZONE_ID, "simon-mwangi.site")
    r.check("an unrecognised routing status is flagged unknown",
            out.get("status_known") is False, repr(out))
    r.check("...and is not reported ready", out.get("ready") is False, repr(out))
    r.check("...and the raw status is preserved",
            out.get("status") == "something_new", repr(out))


# ============================================================
# TRANSPORT AND ENVELOPE
# ============================================================

def check_errors_keep_their_codes(r: Results) -> None:
    print("\nenvelope: a Cloudflare error keeps the code a person can look up")
    fake = with_http({"POST /zones": (400, {
        "success": False,
        "errors": [{"code": 1061, "message": "zone already exists"}],
        "messages": [], "result": None}),
        "GET /zones": (200, envelope([]))})
    os.environ["CLOUDFLARE_ACCOUNT_ID"] = ACCOUNT_ID
    out = cf.create_zone("simon-mwangi.site")
    r.check("the failure is reported", "error" in out, repr(out))
    r.check("...the numeric code is preserved in error_codes",
            1061 in (out.get("error_codes") or []), repr(out))
    r.check("...and the code appears in the message a person reads",
            "1061" in out.get("error", ""), repr(out))

    # HTTP 200 with success=false is still a failure.
    fake = with_http({"GET /zones/%s" % ZONE_ID: (200, {
        "success": False, "errors": [{"code": 7003, "message": "no route"}],
        "messages": [], "result": None})})
    out = cf.get_zone(ZONE_ID)
    r.check("HTTP 200 with success=false is a failure", "error" in out, repr(out))

    # A non-JSON body must not crash.
    fake = FakeHTTP()
    fake.routes["GET /zones/%s" % ZONE_ID] = (502, _NON_JSON)
    cf.requests = fake
    out = cf.get_zone(ZONE_ID)
    r.check("a non-JSON body is an error, not an exception",
            "error" in out and "502" in out["error"], repr(out))

    # A transport failure must not raise out of the module.
    fake = with_http({"GET /zones/%s" % ZONE_ID: Boom("dns failure")})
    out = cf.get_zone(ZONE_ID)
    r.check("a transport failure is an error dict, not a raise",
            "error" in out and "Boom" in out["error"], repr(out))

    # No token -> no call.
    saved = os.environ.pop("CLOUDFLARE_API_TOKEN", None)
    fake = with_http({})
    out = cf.get_zone(ZONE_ID)
    r.check("a missing token is reported", "error" in out
            and "CLOUDFLARE_API_TOKEN" in out["error"], repr(out))
    r.check("...and nothing is sent", fake.calls == [], repr(fake.keys()))
    if saved is not None:
        os.environ["CLOUDFLARE_API_TOKEN"] = saved

    # The token is sent as a bearer and is not mangled.
    os.environ["CLOUDFLARE_API_TOKEN"] = "t0ken-for-test"
    fake = with_http({"GET /zones/%s" % ZONE_ID: (200, envelope(zone_fixture()))})
    cf.get_zone(ZONE_ID)
    r.check("the credential goes in an Authorization: Bearer header",
            fake.calls and fake.calls[0]["auth"] == "Bearer t0ken-for-test",
            repr([c["auth"][:12] for c in fake.calls]))


def check_find_zone_keeps_three_outcomes_apart(r: Results) -> None:
    print("\nlookup: 'not there' and 'could not ask' are different answers")
    fake = with_http({"GET /zones": (200, envelope([zone_fixture()]))})
    out = cf.find_zone("simon-mwangi.site")
    r.check("an existing zone is found", out.get("found") is True, repr(out))
    r.check("...with its id", out.get("zone_id") == ZONE_ID, repr(out))

    fake = with_http({"GET /zones": (200, envelope([]))})
    out = cf.find_zone("simon-mwangi.site")
    r.check("an absent zone is found=False", out.get("found") is False, repr(out))
    r.check("...and is NOT an error", "error" not in out, repr(out))

    fake = with_http({"GET /zones": Boom("timeout")})
    out = cf.find_zone("simon-mwangi.site")
    r.check("an unreachable Cloudflare is an error", "error" in out, repr(out))
    r.check("...and must NOT read as found=False",
            out.get("found") is not False,
            "an outage that reads as 'no such zone' makes the caller create a "
            "second zone for a domain that already has one")

    # A response about a different name must not satisfy the lookup.
    fake = with_http({"GET /zones": (200, envelope(
        [zone_fixture(domain="paidpesa.com")]))})
    out = cf.find_zone("simon-mwangi.site")
    r.check("a zone with another name does not satisfy the lookup",
            out.get("found") is False, repr(out))


def check_create_zone_idempotency(r: Results) -> None:
    print("\ncreate: a refused create is resolved by NAME, not by error code")
    os.environ["CLOUDFLARE_ACCOUNT_ID"] = ACCOUNT_ID

    # Refused, but it exists: adopt it.
    fake = with_http({
        "POST /zones": (400, {"success": False, "errors": [
            {"code": 1061, "message": "already exists"}], "result": None}),
        "GET /zones": (200, envelope([zone_fixture()])),
    })
    out = cf.create_zone("simon-mwangi.site")
    r.check("an existing zone is adopted", out.get("zone_id") == ZONE_ID, repr(out))
    r.check("...and reported created=False", out.get("created") is False, repr(out))
    r.check("...with validated nameservers",
            out.get("name_servers") == PAIR_RETIRED, repr(out))

    # Refused, and it does not exist: a real failure.
    fake = with_http({
        "POST /zones": (403, {"success": False, "errors": [
            {"code": 1049, "message": "not allowed"}], "result": None}),
        "GET /zones": (200, envelope([])),
    })
    out = cf.create_zone("simon-mwangi.site")
    r.check("a refused create with no such zone is an error", "error" in out,
            repr(out))
    r.check("...and does not claim a zone id", "zone_id" not in out, repr(out))

    # Refused, and we cannot tell: state unknown, not 'not found'.
    fake = with_http({
        "POST /zones": (500, {"success": False, "errors": [
            {"code": 1000, "message": "internal"}], "result": None}),
        "GET /zones": Boom("timeout"),
    })
    out = cf.create_zone("simon-mwangi.site")
    r.check("create failed AND lookup failed -> state unknown",
            out.get("state") == "unknown", repr(out))
    r.check("...and the lookup failure is reported too",
            "lookup_error" in out, repr(out))

    # No account id: refuse rather than guess.
    os.environ.pop("CLOUDFLARE_ACCOUNT_ID", None)
    fake = with_http({})
    out = cf.create_zone("simon-mwangi.site")
    r.check("no account id -> refuse to guess", "error" in out
            and "ACCOUNT_ID" in out["error"], repr(out))
    r.check("...and send nothing", fake.calls == [], repr(fake.keys()))
    os.environ["CLOUDFLARE_ACCOUNT_ID"] = ACCOUNT_ID

    # A blank domain never reaches the network.
    fake = with_http({})
    out = cf.create_zone("")
    r.check("a blank domain is refused", "error" in out, repr(out))
    r.check("...with no HTTP call", fake.calls == [], repr(fake.keys()))


def check_txt_record_shape(r: Results) -> None:
    print("\nrecords: a TXT record must be inside the zone and never empty")
    zone_get = "GET /zones/%s" % ZONE_ID
    post = "POST /zones/%s/dns_records" % ZONE_ID
    domain = "simon-mwangi.site"

    # Empty content is refused before any write.
    for empty in ["", "   ", None]:
        fake = with_http({zone_get: (200, envelope(zone_fixture()))})
        out = cf.add_txt_record(ZONE_ID, domain, "@", empty)
        r.check(f"content {empty!r} is refused", "error" in out, repr(out))
        r.check(f"...and nothing is posted for {empty!r}",
                fake.count("POST") == 0, repr(fake.keys()))

    # Name qualification.
    for given, expected in [
        ("@", domain),
        ("", domain),
        ("_dmarc", "_dmarc." + domain),
        ("_dmarc." + domain, "_dmarc." + domain),
        (domain, domain),
        # ends with the domain but is a DIFFERENT zone - must be qualified,
        # not left pointing outside this zone.
        ("not" + domain, "not%s.%s" % (domain, domain)),
    ]:
        fake = with_http({
            zone_get: (200, envelope(zone_fixture())),
            post: (200, envelope({"id": "r", "name": expected,
                                  "content": "v", "ttl": 1})),
        })
        cf.add_txt_record(ZONE_ID, domain, given, "v")
        sent = [c["json"] for c in fake.calls if c["method"] == "POST"]
        r.check(f"name {given!r} is sent as {expected!r}",
                sent and sent[0].get("name") == expected,
                f"sent {sent!r}")

    # ttl and type.
    fake = with_http({
        zone_get: (200, envelope(zone_fixture())),
        post: (200, envelope({"id": "r", "name": domain, "content": "v", "ttl": 1})),
    })
    cf.add_txt_record(ZONE_ID, domain, "@", "v")
    sent = [c["json"] for c in fake.calls if c["method"] == "POST"][0]
    r.check("the record type is TXT", sent.get("type") == "TXT", repr(sent))
    r.check("the default ttl is Cloudflare's automatic (1)",
            sent.get("ttl") == 1, repr(sent))


def main() -> int:
    print("test_cloudflare_dns.py — the four ways delegation could silently "
          "not work")
    os.environ["CLOUDFLARE_API_TOKEN"] = "t0ken-for-test"
    os.environ["CLOUDFLARE_ACCOUNT_ID"] = ACCOUNT_ID
    saved_requests = cf.requests
    r = Results()
    try:
        check_nameserver_validation(r)
        check_create_zone_returns_what_cloudflare_sent(r)
        check_no_hardcoded_nameservers(r)
        check_wait_polls_the_zone_not_the_activation_check(r)
        check_activation_check_is_separate_and_guarded(r)
        check_mutations_are_guarded_by_zone_name(r)
        check_email_routing_enabled_is_not_ready(r)
        check_errors_keep_their_codes(r)
        check_find_zone_keeps_three_outcomes_apart(r)
        check_create_zone_idempotency(r)
        check_txt_record_shape(r)
    finally:
        cf.requests = saved_requests
        os.environ.pop("CLOUDFLARE_API_TOKEN", None)
        os.environ.pop("CLOUDFLARE_ACCOUNT_ID", None)
    return r.report()


if __name__ == "__main__":
    raise SystemExit(main())
