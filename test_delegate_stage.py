#!/usr/bin/env python3
"""Delegating a company domain from the registrar to Cloudflare.

    python3 test_delegate_stage.py

Hermetic: no network, no database, no registrar, no Cloudflare. The doubles
replace delegate.namecheap_automation and delegate.cloudflare_dns, so every
line of delegate.py's own ordering, guarding and status logic really runs.

This stage sits between buying a domain and configuring mail on it, and it
is the one that can take a domain off the air. The checks that matter most:

  * nameservers are read BEFORE the host records. A domain already delegated
    has no host records at the registrar, so the other order turns the normal
    case into a failure.
  * nothing is written to the registrar until Cloudflare has handed back the
    nameservers for THIS zone.
  * a domain carrying mail, verification or real subdomains is refused, and
    refused BEFORE any zone is created.
  * "delegated but not yet active" is its own status. Treating it as done is
    the silent-success defect: mail would not arrive.
"""
import os
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

os.environ.setdefault("DATABASE_URL", "postgresql://unused@127.0.0.1:1/none")
os.environ.setdefault("LOG_LEVEL", "CRITICAL")

import delegate
import api_v2
import db

CF_NS = ["houston.ns.cloudflare.com", "serena.ns.cloudflare.com"]
NC_NS = ["dns1.registrar-servers.com", "dns2.registrar-servers.com"]
DOMAIN = "simon-mwangi.site"
ZONE = "a590f8b7508ed27dfd94da49006b9c78"
PARKED = [{"name": "@", "type": "A", "address": "162.255.119.133"},
          {"name": "www", "type": "CNAME", "address": "parkingpage.namecheap.com"}]


class Results:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def check(self, name, cond, detail=""):
        if cond:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed.append(name)
            print(f"  FAIL  {name}" + (f"\n          {detail}" if detail else ""))
        return bool(cond)

    def report(self):
        print()
        print(f"{self.passed} passed / {len(self.failed)} failed")
        for f in self.failed:
            print(f"  failed: {f}")
        return 1 if self.failed else 0


ORDER: list = []          # shared across both fakes: cross-provider sequence


class FakeNamecheap:
    def __init__(self, nameservers=None, hosts=None, ns_error=None,
                 hosts_error=None, set_error=None):
        self.nameservers = list(nameservers if nameservers is not None else NC_NS)
        self.hosts = PARKED if hosts is None else hosts
        self.ns_error = ns_error
        self.hosts_error = hosts_error
        self.set_error = set_error
        self.calls = []

    def get_nameservers(self, domain):
        self.calls.append(("get_nameservers", domain)); ORDER.append("nc.get_nameservers")
        if self.ns_error:
            return {"error": self.ns_error}
        return {"domain": domain, "nameservers": list(self.nameservers),
                "using_registrar_dns": all(h.endswith("registrar-servers.com")
                                           for h in self.nameservers)}

    def get_dns_hosts(self, domain):
        self.calls.append(("get_dns_hosts", domain)); ORDER.append("nc.get_dns_hosts")
        if self.hosts_error:
            return {"error": self.hosts_error}
        return {"domain": domain, "hosts": list(self.hosts)}

    def set_custom_dns(self, domain, nameservers):
        self.calls.append(("set_custom_dns", domain, list(nameservers)))
        ORDER.append("nc.set_custom_dns")
        if self.set_error:
            return {"error": self.set_error, "state": "unchanged"}
        previous = list(self.nameservers)
        self.nameservers = list(nameservers)
        return {"updated": True, "domain": domain, "nameservers": list(nameservers),
                "previous": previous, "encoding": "NameServers"}

    def names(self):
        return [c[0] for c in self.calls]


class FakeCloudflare:
    CF_NS_SUFFIX = ".ns.cloudflare.com"
    DEFAULT_ACTIVATION_TIMEOUT = 600
    DEFAULT_POLL_INTERVAL = 15

    def __init__(self, create=None, active=True, found=False, wait=None):
        self._create = create
        self._active = active
        self._found = found
        self._wait = wait
        self.calls = []

    def find_zone(self, domain):
        self.calls.append(("find_zone", domain)); ORDER.append("cf.find_zone")
        if self._found:
            return {"found": True, "zone_id": ZONE, "domain": domain,
                    "status": "active" if self._active else "pending",
                    "active": self._active, "name_servers": CF_NS}
        return {"found": False, "domain": domain}

    def create_zone(self, domain, account_id=None):
        self.calls.append(("create_zone", domain)); ORDER.append("cf.create_zone")
        if self._create is not None:
            return dict(self._create)
        return {"zone_id": ZONE, "domain": domain,
                "status": "active" if self._active else "pending",
                "active": self._active, "name_servers": list(CF_NS),
                "created": True}

    def wait_until_active(self, zone_id, domain, timeout=None,
                          poll_interval=None, _sleep=None, _now=None):
        self.calls.append(("wait_until_active", zone_id, domain))
        ORDER.append("cf.wait_until_active")
        if self._wait is not None:
            return dict(self._wait)
        if self._active:
            return {"active": True, "timed_out": False, "polls": 1}
        return {"active": False, "timed_out": True,
                "message": "zone %s was still 'pending'" % domain}

    def names(self):
        return [c[0] for c in self.calls]


def with_doubles(nc=None, cf=None):
    ORDER.clear()
    nc = nc or FakeNamecheap()
    cf = cf or FakeCloudflare()
    delegate.namecheap_automation = nc
    delegate.cloudflare_dns = cf
    return nc, cf


# ── the guard ───────────────────────────────────────────────────────────────
def check_parking_classifier(r: Results) -> None:
    print("\n-- what counts as safe to delegate away --")
    safe = [
        ("no records at all", []),
        ("namecheap parking", PARKED),
        ("a parking URL record",
         [{"name": "@", "type": "URL301", "address": "http://www.parkingpage.namecheap.com/"}]),
        ("apex and www only",
         [{"name": "@", "type": "A", "address": "1.2.3.4"},
          {"name": "www", "type": "A", "address": "1.2.3.4"}]),
    ]
    for label, hosts in safe:
        parked, serving = delegate._looks_parked(hosts)
        r.check(f"{label} -> safe", parked, f"flagged {serving!r}")

    unsafe = [
        ("an MX record", [{"name": "@", "type": "MX", "address": "mail.example.com"}]),
        ("a TXT record",
         [{"name": "@", "type": "TXT", "address": "google-site-verification=x"}]),
        ("a real subdomain",
         [{"name": "api", "type": "A", "address": "1.2.3.4"}]),
        ("parking PLUS an MX",
         PARKED + [{"name": "@", "type": "MX", "address": "mail.example.com"}]),
    ]
    for label, hosts in unsafe:
        parked, serving = delegate._looks_parked(hosts)
        r.check(f"{label} -> refused", not parked, f"was treated as parked")
        r.check(f"...{label} is named in the reason", bool(serving), repr(serving))


# ── ordering and the happy path ─────────────────────────────────────────────
def check_happy_path_and_ordering(r: Results) -> None:
    print("\n-- a parked domain delegates, in the right order --")
    nc, cf = with_doubles()
    out = delegate.delegate_domain(DOMAIN)
    r.check("status is active", out.get("status") == "active", repr(out))
    r.check("the registrar was updated", out.get("registrar_updated") is True,
            repr(out))
    r.check("the nameservers set are the ones Cloudflare assigned",
            ("set_custom_dns", DOMAIN, CF_NS) in nc.calls, repr(nc.calls))
    r.check("the live registrar nameservers really changed",
            nc.nameservers == CF_NS, repr(nc.nameservers))
    order = nc.names()
    r.check("nameservers are read BEFORE the host records",
            order.index("get_nameservers") < order.index("get_dns_hosts"),
            f"order was {order!r} - a delegated domain has no host records, "
            f"so the other order turns the normal case into a failure")
    r.check("the zone is created BEFORE the registrar is written",
            ORDER.index("cf.create_zone") < ORDER.index("nc.set_custom_dns"),
            f"order was {ORDER!r} - writing nameservers before Cloudflare has "
            f"handed back the pair for THIS zone means writing a guess")
    r.check("activation is polled only after the registrar is written",
            ORDER.index("nc.set_custom_dns") < ORDER.index("cf.wait_until_active"),
            f"order was {ORDER!r}")
    r.check("the full cross-provider order is exactly as designed",
            ORDER == ["nc.get_nameservers", "nc.get_dns_hosts",
                      "cf.create_zone", "nc.set_custom_dns",
                      "cf.wait_until_active"],
            f"order was {ORDER!r}")
    r.check("the discarded records are reported",
            out.get("records_discarded") == PARKED, repr(out.get("records_discarded")))


def check_already_delegated(r: Results) -> None:
    print("\n-- a domain already on Cloudflare is not rewritten --")
    nc, cf = with_doubles(FakeNamecheap(nameservers=CF_NS),
                          FakeCloudflare(found=True, active=True))
    out = delegate.delegate_domain(DOMAIN)
    r.check("still reports active", out.get("status") == "active", repr(out))
    r.check("the registrar was NOT written", "set_custom_dns" not in nc.names(),
            repr(nc.calls))
    r.check("and its host records were never read",
            "get_dns_hosts" not in nc.names(),
            "a delegated domain has no host records at the registrar; asking "
            "would fail and look like a fault")


def check_guard_blocks_before_anything_is_created(r: Results) -> None:
    print("\n-- a domain that is serving something is refused, early --")
    serving = PARKED + [{"name": "@", "type": "MX", "address": "mail.example.com"}]
    nc, cf = with_doubles(FakeNamecheap(hosts=serving))
    out = delegate.delegate_domain(DOMAIN)
    r.check("status is refused_serving", out.get("status") == "refused_serving",
            repr(out))
    r.check("the offending record is named", any(
        s.get("type") == "MX" for s in (out.get("serving") or [])), repr(out))
    r.check("NO zone was created", "create_zone" not in cf.names(), repr(cf.calls))
    r.check("NOTHING was written to the registrar",
            "set_custom_dns" not in nc.names(), repr(nc.calls))
    r.check("the message says how to override", "force=True" in
            str(out.get("message", "")), repr(out))

    # force goes ahead
    nc, cf = with_doubles(FakeNamecheap(hosts=serving))
    out = delegate.delegate_domain(DOMAIN, force=True)
    r.check("force=True proceeds", out.get("status") == "active", repr(out))
    r.check("...and still reports what it discarded",
            out.get("records_discarded") == serving, repr(out))


def check_pending_is_not_done(r: Results) -> None:
    print("\n-- delegated but not yet active is its own status --")
    nc, cf = with_doubles(cf=FakeCloudflare(active=False))
    out = delegate.delegate_domain(DOMAIN)
    r.check("status is pending_activation",
            out.get("status") == "pending_activation", repr(out))
    r.check("it is NOT active", out.get("status") != "active", repr(out))
    r.check("it is NOT an error", out.get("status") != "error", repr(out))
    r.check("the registrar was still updated",
            out.get("registrar_updated") is True,
            "the delegation happened; only Cloudflare has not noticed yet")
    r.check("the message explains the wait",
            "propagat" in str(out.get("message", "")).lower()
            or "pending" in str(out.get("message", "")).lower(), repr(out))

    # wait=False short-circuits the poll
    nc, cf = with_doubles(cf=FakeCloudflare(active=False))
    out = delegate.delegate_domain(DOMAIN, wait=False)
    r.check("wait=False does not poll", "wait_until_active" not in cf.names(),
            repr(cf.calls))
    r.check("...and still reports pending_activation",
            out.get("status") == "pending_activation", repr(out))


def check_failures_stop_before_damage(r: Results) -> None:
    print("\n-- every upstream failure stops before touching the registrar --")
    cases = [
        ("the registrar's nameservers cannot be read",
         FakeNamecheap(ns_error="2019166: no such domain"), FakeCloudflare()),
        ("the registrar's host records cannot be read",
         FakeNamecheap(hosts_error="2019166: unavailable"), FakeCloudflare()),
        ("the zone cannot be created",
         FakeNamecheap(), FakeCloudflare(create={"error": "1049: not allowed"})),
    ]
    for label, nc_, cf_ in cases:
        nc, cf = with_doubles(nc_, cf_)
        out = delegate.delegate_domain(DOMAIN)
        r.check(f"{label} -> error", out.get("status") == "error", repr(out))
        r.check(f"...{label}: registrar NOT written",
                "set_custom_dns" not in nc.names(), repr(nc.calls))
        r.check(f"...{label}: the reason is carried",
                bool(out.get("error")), repr(out))

    # the registrar write itself failing
    nc, cf = with_doubles(FakeNamecheap(set_error="neither encoding worked"))
    out = delegate.delegate_domain(DOMAIN)
    r.check("a failed nameserver write is an error",
            out.get("status") == "error", repr(out))
    r.check("...and the registrar's own reason is carried",
            "neither encoding" in str(out.get("error", "")), repr(out))
    r.check("...and the nameservers are unchanged", nc.nameservers == NC_NS,
            repr(nc.nameservers))


# ── the stage ───────────────────────────────────────────────────────────────
def _run(cn, row, result):
    writes = []
    saved = (api_v2._update_excel_row, delegate.delegate_domain)
    try:
        api_v2._update_excel_row = lambda c, u: writes.append(dict(u))
        delegate.delegate_domain = lambda d, **kw: dict(result)
        out = api_v2._exec_stage(cn, "delegate", row)
        return out, {k: v for w in writes for k, v in w.items()}
    finally:
        api_v2._update_excel_row, delegate.delegate_domain = saved


def check_stage(r: Results) -> None:
    print("\n-- the stage records every outcome on the row --")
    row = {"Company Number": "00000001", "Company Name": "ACME LTD",
           "Domain": DOMAIN}

    out, wrote = _run("00000001", row,
                      {"status": "active", "zone_id": ZONE,
                       "assigned_nameservers": CF_NS, "records_discarded": PARKED})
    r.check("active -> the stage is done", out.get("status") == "done", repr(out))
    r.check("...and the row records active",
            wrote.get("DNS Delegation") == "active", repr(wrote))

    out, wrote = _run("00000002", dict(row, **{"Company Number": "00000002"}),
                      {"status": "pending_activation", "zone_id": ZONE,
                       "assigned_nameservers": CF_NS,
                       "message": "registry has not propagated"})
    r.check("pending -> unresolved, which halts a run-all chain",
            out.get("status") == "unresolved", repr(out))
    r.check("...and is NOT done", out.get("status") != "done", repr(out))
    r.check("...and the row records pending_activation",
            wrote.get("DNS Delegation") == "pending_activation", repr(wrote))
    r.check("...and the reason reaches the operator",
            "propagat" in str(out.get("error", "")), repr(out))

    out, wrote = _run("00000003", dict(row, **{"Company Number": "00000003"}),
                      {"status": "refused_serving",
                       "message": "still has 1 record(s)"})
    r.check("refused -> the stage fails", out.get("status") == "error", repr(out))
    r.check("...and the row records refused_serving",
            wrote.get("DNS Delegation") == "refused_serving", repr(wrote))
    r.check("...and the reason reaches the operator",
            "record" in str(out.get("error", "")), repr(out))

    out, wrote = _run("00000004", {"Company Number": "00000004",
                                   "Company Name": "ACME LTD"},
                      {"status": "active"})
    r.check("no domain -> the stage fails", out.get("status") == "error",
            repr(out))
    r.check("...and nothing is written", wrote == {}, repr(wrote))


def check_wiring(r: Results) -> None:
    print("\n-- the stage is wired into the pipeline --")
    r.check("delegate is in STAGE_ORDER", "delegate" in api_v2.STAGE_ORDER,
            repr(api_v2.STAGE_ORDER))
    i = api_v2.STAGE_ORDER.index
    r.check("it runs after domain", i("delegate") > i("domain"),
            repr(api_v2.STAGE_ORDER))
    r.check("it runs before email", i("delegate") < i("email"),
            "mail cannot be configured on a zone Cloudflare does not serve")
    r.check("the column exists", "DNS Delegation" in db.HEADER_TO_COL,
            "the stage has nowhere to record its result")

    stages = api_v2._compute_stages("00000001", {
        "Company Name": "ACME LTD", "Domain": DOMAIN,
        "DNS Delegation": "active"})
    r.check("active marks the stage complete", stages.get("delegate") is True,
            repr(stages))
    for partial in ("pending_activation", "refused_serving", "error", ""):
        stages = api_v2._compute_stages("00000001", {
            "Company Name": "ACME LTD", "Domain": DOMAIN,
            "DNS Delegation": partial})
        r.check(f"{partial!r} does NOT mark it complete",
                stages.get("delegate") is False,
                "mail would not arrive; marking it done is the silent success")

    html = open(os.path.join(REPO, "static", "app.html"), encoding="utf-8").read()
    r.check("the dashboard lists the stage", 'key: "delegate"' in html,
            "the operator would never see it")
    r.check("the dashboard can show its value", '"dns_delegation"' in html
            or "dns_delegation" in html, "no detail line for the stage")


def main() -> int:
    print("test_delegate_stage.py — moving a domain's DNS to Cloudflare")
    saved = (delegate.namecheap_automation, delegate.cloudflare_dns)
    r = Results()
    try:
        check_parking_classifier(r)
        check_happy_path_and_ordering(r)
        check_already_delegated(r)
        check_guard_blocks_before_anything_is_created(r)
        check_pending_is_not_done(r)
        check_failures_stop_before_damage(r)
        check_stage(r)
        check_wiring(r)
    finally:
        delegate.namecheap_automation, delegate.cloudflare_dns = saved
    return r.report()


if __name__ == "__main__":
    raise SystemExit(main())
