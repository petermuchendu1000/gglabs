"""
Cloudflare DNS control plane for delegated company domains.

A domain is registered at Namecheap and then DELEGATED to Cloudflare, which
becomes the authority for its DNS. This module owns the Cloudflare half of
that move: create the zone, read back the nameservers Cloudflare assigned to
it, wait for activation, write records, and turn on Email Routing.

API docs (read 2026-11-13, evidence CF-DELEG-01 EV-006/007/009/010):
    https://developers.cloudflare.com/api/resources/zones/methods/create/
    https://developers.cloudflare.com/api/resources/zones/subresources/activation_check/methods/trigger/
    https://developers.cloudflare.com/api/resources/dns/subresources/records/methods/create/
    https://developers.cloudflare.com/api/resources/email_routing/

Four things in here are not stylistic preferences. They are the whole reason
the module exists in this shape.

1. THE NAMESERVERS ARE PER ZONE AND MUST BE READ BACK, NEVER HARDCODED.
   Cloudflare documents name_servers as "The name servers Cloudflare assigns
   to a zone", and that is literally true per zone. Measured on the account
   this pipeline uses (EV-008): of 26 live zones, 22 answer on
   lauryn/maciej.ns.cloudflare.com and only 4 on houston/serena. The
   documentation's own example shows a third pair, bob/lola. Setting one fixed
   pair at the registrar would therefore point roughly five domains in six at
   nameservers that do not serve their zone. Nothing bounces: the zone simply
   stays pending for ever and the domain never resolves. So create_zone()
   refuses to report success unless Cloudflare actually told us the pair, and
   the pair it returns is the only thing a caller may set at the registrar.

2. WAITING FOR ACTIVATION POLLS THE ZONE, NOT THE ACTIVATION CHECK.
   PUT /zones/{id}/activation_check is rate limited to once an hour on a Free
   zone (EV-007, quoted from the endpoint's own page). A poll loop built on it
   would be throttled on its second iteration and would then report a
   perfectly healthy delegation as a failure. wait_until_active() reads
   GET /zones/{id}.status instead, which is not rate limited that way, and
   trigger_activation_check() is kept separate and called sparingly.

3. A MUTATION IS GUARDED BY THE ZONE'S OWN NAME.
   This Cloudflare account holds 26 zones and every one of them is active
   production DNS (EV-008). The credential reaches all of them. So every
   mutating call here takes the domain it BELIEVES it is editing and verifies
   that the zone really carries that name before it writes anything. A
   transposed or stale zone id then fails the guard instead of rewriting a
   live domain's records. Nothing in this module lists zones and then edits
   what it finds.

4. ENABLED IS NOT READY, AND A TIMEOUT IS NOT A FAILURE TO ACTIVATE.
   Email Routing exposes enabled (bool) and status (enum) as separate fields,
   so enabled=True with status="misconfigured" is a state the API can return
   (EV-010); reporting that as configured is the silent-success defect this
   codebase has been hunting. Likewise wait_until_active() distinguishes
   "Cloudflare says active" from "we stopped waiting", because the second one
   says nothing about whether the delegation will come up.

CREDENTIALS
    CLOUDFLARE_API_TOKEN   required for every live call
    CLOUDFLARE_ACCOUNT_ID  required to create a zone; optional elsewhere
Both are read at CALL time, not at import, so a .env loaded later still
works and so tests can set them. A scoped token is enough: zone creation
needs Zone:Edit or DNS:Edit, the activation check needs Zone:Write, records
need DNS:Write, and Email Routing needs Zone Settings:Write.
"""

import os
import time

try:
    import requests
except ModuleNotFoundError:          # keeps the hermetic suite importable
    requests = None
try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv():
        return False

load_dotenv()

API_BASE = "https://api.cloudflare.com/client/v4"

# Documented zone statuses (EV-006). Membership is tested against this set
# rather than by truthiness, so an unrecognised status is visible instead of
# being read as "not active".
ZONE_STATUSES = frozenset({"initializing", "pending", "active", "moved"})
ZONE_ACTIVE = "active"

# Documented Email Routing statuses (EV-010). "ready" is the only one that
# means mail will actually be delivered.
EMAIL_ROUTING_STATUSES = frozenset({
    "ready", "unconfigured", "misconfigured", "misconfigured/locked", "unlocked",
})
EMAIL_ROUTING_READY = "ready"

# A full zone's assigned nameservers are always Cloudflare's own; an
# Enterprise vanity setup reports those separately in vanity_name_servers.
# Verified against all 26 zones on this account and the published example
# (EV-006, EV-008).
CF_NS_SUFFIX = ".ns.cloudflare.com"
MIN_NAMESERVERS = 2

HTTP_TIMEOUT = 30

# Defaults for wait_until_active. A fresh registration normally flips within
# minutes, but the figure is NOT derived from a measurement of this pipeline
# yet, so it is a parameter and the caller is expected to set it.
DEFAULT_ACTIVATION_TIMEOUT = 600
DEFAULT_POLL_INTERVAL = 15


# ============================================================
# TRANSPORT
# ============================================================

def _token():
    return os.getenv("CLOUDFLARE_API_TOKEN", "").strip()


def _account_id():
    return os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()


def _request(method, path, body=None, params=None):
    """One Cloudflare call, normalised.

    Returns {"ok": bool, "result": <any>, "errors": [str], "error_codes": [int],
             "http": int|None, "messages": [str]}.

    The body is parsed BEFORE the status code is considered, because
    Cloudflare puts the actual reason in the JSON envelope and raising on the
    status would throw it away - the same mistake that once left the operator
    holding a sentence with no error number to look up.
    """
    if requests is None:
        raise RuntimeError(
            "The 'requests' package is required for live Cloudflare API calls.")
    token = _token()
    if not token:
        return {"ok": False, "result": None, "http": None, "messages": [],
                "errors": ["CLOUDFLARE_API_TOKEN is not set"],
                "error_codes": []}

    url = API_BASE + path
    headers = {"Authorization": "Bearer " + token,
               "Content-Type": "application/json"}
    try:
        resp = requests.request(method, url, headers=headers, json=body,
                                params=params, timeout=HTTP_TIMEOUT)
    except Exception as exc:                      # network-level failure
        # A transport failure is NOT an answer from Cloudflare. It must never
        # be collapsed into "the zone does not exist" or "not enabled".
        return {"ok": False, "result": None, "http": None, "messages": [],
                "errors": ["request to Cloudflare failed: %s: %s"
                           % (type(exc).__name__, exc)],
                "error_codes": []}

    http = resp.status_code
    try:
        payload = resp.json()
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        return {"ok": False, "result": None, "http": http, "messages": [],
                "errors": ["HTTP %s with a non-JSON body: %s"
                           % (http, (resp.text or "")[:200])],
                "error_codes": []}

    errors, codes = [], []
    for item in payload.get("errors") or []:
        if not isinstance(item, dict):
            errors.append(str(item))
            continue
        code = item.get("code")
        message = (item.get("message") or "").strip()
        if isinstance(code, int):
            codes.append(code)
        # Keep the CODE. It is the only part a person can look up.
        errors.append("%s: %s" % (code, message) if code is not None
                      else (message or "unknown error"))
    messages = [str((m or {}).get("message", m)) for m in payload.get("messages") or []]

    ok = payload.get("success") is True and 200 <= http < 300
    if not ok and not errors:
        errors.append("HTTP %s and success=%r with no error detail"
                      % (http, payload.get("success")))
    return {"ok": ok, "result": payload.get("result"), "http": http,
            "errors": errors, "error_codes": codes, "messages": messages}


def _failed(call, prefix):
    """Turn a failed _request into this codebase's error dict."""
    return {"error": "%s: %s" % (prefix, "; ".join(call["errors"])),
            "error_codes": call["error_codes"],
            "http": call["http"]}


# ============================================================
# NAMESERVER VALIDATION  (defect 1 in the module docstring)
# ============================================================

def validate_nameservers(candidate):
    """Return a cleaned nameserver list, or raise ValueError.

    This is the gate that stops the pipeline handing Namecheap something that
    is not a working delegation. Cloudflare's documented response includes
    name_servers, but whether it is POPULATED on the POST for a still-pending
    zone is not established - every zone on this account was already active
    when it was read (EV-008, DOES NOT ESTABLISH). So the unknown is made
    safe rather than assumed away: if the pair is missing, short, or not
    Cloudflare's, this raises and the caller never reaches the registrar.
    """
    if not isinstance(candidate, (list, tuple)):
        raise ValueError("nameservers must be a list, got %s"
                         % type(candidate).__name__)
    cleaned = []
    for item in candidate:
        if not isinstance(item, str):
            raise ValueError("nameserver is not a string: %r" % (item,))
        host = item.strip().rstrip(".").lower()
        if not host:
            raise ValueError("nameserver is blank")
        if not host.endswith(CF_NS_SUFFIX):
            raise ValueError(
                "nameserver %r is not a Cloudflare nameserver (expected a "
                "*%s host)" % (item, CF_NS_SUFFIX))
        if host not in cleaned:
            cleaned.append(host)
    if len(cleaned) < MIN_NAMESERVERS:
        raise ValueError(
            "Cloudflare returned %d distinct nameserver(s), need at least %d: %r"
            % (len(cleaned), MIN_NAMESERVERS, candidate))
    return cleaned


# ============================================================
# ZONES
# ============================================================

def _zone_summary(zone):
    """The fields a caller needs, from a raw Cloudflare zone object."""
    status = zone.get("status")
    return {
        "zone_id": zone.get("id"),
        "domain": (zone.get("name") or "").lower(),
        "status": status,
        "status_known": status in ZONE_STATUSES,
        "active": status == ZONE_ACTIVE,
        "name_servers": [str(n).lower() for n in (zone.get("name_servers") or [])],
        "original_name_servers": [str(n).lower()
                                  for n in (zone.get("original_name_servers") or [])],
        "original_registrar": zone.get("original_registrar"),
        "type": zone.get("type"),
        "paused": zone.get("paused"),
        "account_id": (zone.get("account") or {}).get("id"),
    }


def get_zone(zone_id):
    """Read one zone by id."""
    if not zone_id:
        return {"error": "zone_id is required"}
    call = _request("GET", "/zones/%s" % zone_id)
    if not call["ok"]:
        return _failed(call, "Could not read zone %s" % zone_id)
    if not isinstance(call["result"], dict):
        return {"error": "Unexpected zone payload for %s" % zone_id}
    return _zone_summary(call["result"])


def find_zone(domain):
    """Look a zone up BY NAME.

    Three outcomes, kept apart on purpose:
        {"found": True, ...zone summary...}
        {"found": False}                      -- Cloudflare answered, no such zone
        {"error": ...}                        -- Cloudflare did not answer
    "Cloudflare did not answer" must never be read as "the zone is not there",
    which is the distinction that decides whether the pipeline goes on to
    create one.
    """
    domain = (domain or "").strip().lower()
    if not domain:
        return {"error": "domain is required"}
    call = _request("GET", "/zones", params={"name": domain})
    if not call["ok"]:
        return _failed(call, "Could not look up zone %s" % domain)
    results = call["result"] if isinstance(call["result"], list) else []
    for zone in results:
        if isinstance(zone, dict) and (zone.get("name") or "").lower() == domain:
            summary = _zone_summary(zone)
            summary["found"] = True
            return summary
    return {"found": False, "domain": domain}


def create_zone(domain, account_id=None):
    """Create a zone and return the nameservers Cloudflare assigned to IT.

    Idempotent by lookup, not by error code. If the create is refused, this
    asks Cloudflare whether the zone exists by NAME and adopts it when it
    does. That deliberately avoids keying on a duplicate-zone error number:
    this pipeline has already been bitten once by an API error code quoted
    from memory, and a code that turns out to mean something else would make
    a real failure look like a successful retry.

    On success returns a zone summary with a validated "name_servers" list
    and "created": True|False (False meaning it already existed). The caller
    sets exactly those nameservers at the registrar and nothing else.
    """
    domain = (domain or "").strip().lower().rstrip(".")
    if not domain:
        return {"error": "domain is required"}
    account_id = (account_id or _account_id()).strip()
    if not account_id:
        return {"error": "CLOUDFLARE_ACCOUNT_ID is not set and no account_id "
                         "was passed; refusing to guess which account to "
                         "create %s in" % domain}

    call = _request("POST", "/zones", body={
        "name": domain,
        "type": "full",
        "account": {"id": account_id},
    })

    zone = call["result"] if isinstance(call["result"], dict) else None
    if call["ok"] and zone is not None:
        summary, created = _zone_summary(zone), True
    else:
        existing = find_zone(domain)
        if "error" in existing:
            # The create failed AND we cannot tell whether it exists. Report
            # the create's own error, and say the state is unknown - do not
            # invite a retry that might double-create.
            failure = _failed(call, "Could not create zone %s" % domain)
            failure["lookup_error"] = existing["error"]
            failure["state"] = "unknown"
            return failure
        if not existing.get("found"):
            return _failed(call, "Could not create zone %s" % domain)
        summary, created = existing, False

    if summary.get("domain") != domain:
        return {"error": "Cloudflare returned zone %r for a request about %r; "
                         "refusing to use it"
                         % (summary.get("domain"), domain)}

    try:
        summary["name_servers"] = validate_nameservers(summary.get("name_servers"))
    except ValueError as exc:
        # The zone may well exist. What we do NOT have is a delegation target,
        # and that is the only thing the next step consumes.
        return {"error": "Zone %s exists (id=%s, status=%s) but Cloudflare did "
                         "not give usable nameservers: %s"
                         % (domain, summary.get("zone_id"),
                            summary.get("status"), exc),
                "zone_id": summary.get("zone_id"),
                "status": summary.get("status"),
                "state": "zone_without_nameservers"}

    summary["created"] = created
    return summary


def _verify_zone_is(zone_id, expected_domain):
    """Guard every mutation with the zone's own name (defect 3).

    Returns the zone summary, or an error dict. A zone id that does not carry
    the domain the caller named is refused outright: this credential can edit
    all 26 live zones on the account, so a stale or transposed id must fail
    here rather than rewrite production DNS for somebody else's domain.
    """
    if not zone_id:
        return {"error": "zone_id is required"}
    expected = (expected_domain or "").strip().lower().rstrip(".")
    if not expected:
        return {"error": "expected_domain is required; refusing to mutate a "
                         "zone without knowing which domain it should be"}
    zone = get_zone(zone_id)
    if "error" in zone:
        return zone
    if zone.get("domain") != expected:
        return {"error": "Zone %s is %r, not %r; refusing to modify it"
                         % (zone_id, zone.get("domain"), expected)}
    return zone


def trigger_activation_check(zone_id, expected_domain):
    """Ask Cloudflare to re-check the delegation now.

    RATE LIMITED: once every 5 minutes on paid plans, ONCE AN HOUR on a Free
    zone (EV-007). Call it to nudge a zone that has been pending a while -
    never inside a poll loop. wait_until_active() polls the zone instead.
    """
    zone = _verify_zone_is(zone_id, expected_domain)
    if "error" in zone:
        return zone
    call = _request("PUT", "/zones/%s/activation_check" % zone_id)
    if not call["ok"]:
        return _failed(call, "Activation check for %s failed" % expected_domain)
    return {"triggered": True, "zone_id": zone_id, "domain": expected_domain}


def wait_until_active(zone_id, expected_domain,
                      timeout=DEFAULT_ACTIVATION_TIMEOUT,
                      poll_interval=DEFAULT_POLL_INTERVAL,
                      _sleep=None, _now=None):
    """Poll GET /zones/{id} until status is active, or the timeout expires.

    Returns a dict that keeps three outcomes apart (defect 4):
        active=True                      Cloudflare says the zone is active
        timed_out=True, active=False     we stopped waiting; says NOTHING about
                                         whether the delegation will come up
        "error" in result                Cloudflare could not be read

    _sleep and _now are injection seams so the tests can exercise the loop
    without spending the wall clock on it.
    """
    sleep = _sleep or time.sleep
    now = _now or time.monotonic

    zone = _verify_zone_is(zone_id, expected_domain)
    if "error" in zone:
        return zone

    started = now()
    polls = 0
    last = zone
    while True:
        polls += 1
        if last.get("active"):
            return {"active": True, "timed_out": False, "status": last.get("status"),
                    "zone_id": zone_id, "domain": expected_domain,
                    "polls": polls, "waited": now() - started,
                    "name_servers": last.get("name_servers", [])}
        if now() - started + poll_interval > timeout:
            return {"active": False, "timed_out": True, "status": last.get("status"),
                    "zone_id": zone_id, "domain": expected_domain,
                    "polls": polls, "waited": now() - started,
                    "message": "zone %s was still %r after %.0fs; Cloudflare has "
                               "not seen the nameservers change yet"
                               % (expected_domain, last.get("status"),
                                  now() - started)}
        sleep(poll_interval)
        last = get_zone(zone_id)
        if "error" in last:
            return last


# ============================================================
# DNS RECORDS
# ============================================================

def add_txt_record(zone_id, expected_domain, name, content, ttl=1, comment=None):
    """Add one TXT record. Does NOT replace anything.

    Namecheap's setHosts takes the complete record set, so adding one record
    there means reading them all back and reposting them - and dropping one by
    accident. Cloudflare creates records individually, so this adds and
    touches nothing else.

    `content` is required HERE even though Cloudflare marks it optional
    (EV-009): an empty TXT record would be accepted and would verify nothing.
    ttl=1 is Cloudflare's documented "automatic".
    """
    zone = _verify_zone_is(zone_id, expected_domain)
    if "error" in zone:
        return zone

    content = (content or "").strip()
    if not content:
        return {"error": "refusing to create an empty TXT record on %s"
                         % expected_domain}
    record_name = (name or "").strip().rstrip(".").lower() or expected_domain
    if record_name == "@":
        record_name = expected_domain
    elif record_name != expected_domain and not record_name.endswith(
            "." + expected_domain):
        # Cloudflare wants the FULL record name including the zone (EV-009).
        # The suffix test is on "." + domain, not on the bare domain: a label
        # like "notexample.com" ends with "example.com" but names a DIFFERENT
        # zone, and qualifying it is the only way it stays inside this one.
        record_name = "%s.%s" % (record_name, expected_domain)

    body = {"type": "TXT", "name": record_name, "content": content, "ttl": ttl}
    if comment:
        body["comment"] = comment
    call = _request("POST", "/zones/%s/dns_records" % zone_id, body=body)
    if not call["ok"]:
        return _failed(call, "Could not add TXT record to %s" % expected_domain)
    record = call["result"] if isinstance(call["result"], dict) else {}
    return {"status": "configured",
            "zone_id": zone_id,
            "domain": expected_domain,
            "record_id": record.get("id"),
            "name": record.get("name", record_name),
            "content": record.get("content", content),
            "ttl": record.get("ttl", ttl)}


def list_txt_records(zone_id, expected_domain, name=None):
    """Read the zone's TXT records, for confirming what is actually live."""
    zone = _verify_zone_is(zone_id, expected_domain)
    if "error" in zone:
        return zone
    params = {"type": "TXT"}
    if name:
        params["name"] = name
    call = _request("GET", "/zones/%s/dns_records" % zone_id, params=params)
    if not call["ok"]:
        return _failed(call, "Could not list TXT records for %s" % expected_domain)
    rows = call["result"] if isinstance(call["result"], list) else []
    return {"domain": expected_domain,
            "records": [{"record_id": r.get("id"), "name": r.get("name"),
                         "content": r.get("content"), "ttl": r.get("ttl")}
                        for r in rows if isinstance(r, dict)]}


# ============================================================
# EMAIL ROUTING
# ============================================================

def email_routing_status(zone_id, expected_domain):
    """Read Email Routing settings for a zone.

    enabled and status are reported SEPARATELY and `ready` is computed from
    status alone, because enabled=True with status="misconfigured" is a state
    the API can return (EV-010).
    """
    zone = _verify_zone_is(zone_id, expected_domain)
    if "error" in zone:
        return zone
    call = _request("GET", "/zones/%s/email/routing" % zone_id)
    if not call["ok"]:
        return _failed(call, "Could not read Email Routing for %s" % expected_domain)
    settings = call["result"] if isinstance(call["result"], dict) else {}
    status = settings.get("status")
    return {"domain": expected_domain,
            "zone_id": zone_id,
            "enabled": settings.get("enabled") is True,
            "status": status,
            "status_known": status in EMAIL_ROUTING_STATUSES,
            "ready": status == EMAIL_ROUTING_READY,
            "settings_id": settings.get("id")}


def enable_email_routing(zone_id, expected_domain):
    """Turn on Email Routing, then CHECK that it is actually on.

    Uses POST /zones/{id}/email/routing/dns. The endpoint most tutorials
    reach for, POST .../email/routing/enable, is marked deprecated (EV-010).

    The 2xx is not the answer. This reads the settings back and returns
    status "configured" only when Cloudflare reports status == "ready";
    anything else comes back as "enabled_not_ready" carrying the real status,
    so a half-configured zone cannot be mistaken for a working mailbox.
    """
    zone = _verify_zone_is(zone_id, expected_domain)
    if "error" in zone:
        return zone

    call = _request("POST", "/zones/%s/email/routing/dns" % zone_id)
    if not call["ok"]:
        # Already-enabled is a legitimate state, so do not fail on the call
        # alone: read the settings and let them decide.
        observed = email_routing_status(zone_id, expected_domain)
        if "error" not in observed and observed.get("ready"):
            observed["status_detail"] = "already enabled"
            observed["status"] = EMAIL_ROUTING_READY
            observed["result"] = "configured"
            return observed
        failure = _failed(call, "Could not enable Email Routing for %s"
                          % expected_domain)
        if "error" not in observed:
            failure["observed_status"] = observed.get("status")
            failure["observed_enabled"] = observed.get("enabled")
        return failure

    observed = email_routing_status(zone_id, expected_domain)
    if "error" in observed:
        return observed
    observed["result"] = "configured" if observed.get("ready") else "enabled_not_ready"
    if not observed.get("ready"):
        observed["message"] = (
            "Email Routing on %s reports enabled=%r status=%r, not %r; mail "
            "will not be delivered until it is"
            % (expected_domain, observed.get("enabled"), observed.get("status"),
               EMAIL_ROUTING_READY))
    return observed


def list_routing_rules(zone_id, expected_domain):
    """Read the zone's Email Routing rules."""
    zone = _verify_zone_is(zone_id, expected_domain)
    if "error" in zone:
        return zone
    call = _request("GET", "/zones/%s/email/routing/rules" % zone_id)
    if not call["ok"]:
        return _failed(call, "Could not list routing rules for %s" % expected_domain)
    rows = call["result"] if isinstance(call["result"], list) else []
    rules = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        rules.append({
            "rule_id": r.get("id") or r.get("tag"),
            "name": r.get("name"),
            "enabled": r.get("enabled") is True,
            "matchers": r.get("matchers") or [],
            "actions": r.get("actions") or [],
        })
    return {"domain": expected_domain, "zone_id": zone_id, "rules": rules}
