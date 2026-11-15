#!/usr/bin/env python3
"""Delegating a domain at the registrar — the call that can take a site off
the air.

    python3 test_delegation.py

Hermetic: no network, no registrar account. The double sits at the
`requests` layer inside namecheap_automation, so the module's real XML
parsing, namespace handling and error extraction all run.

namecheap.domains.dns.setCustom's parameter encoding could not be confirmed
from Namecheap's own documentation - www.namecheap.com serves a bot
challenge to the build environment, so the page was never read, and
secondary sources disagree about whether it takes one comma-separated
"NameServers" or numbered "Nameserver1..N".

set_custom_dns therefore tries both and decides by READING THE NAMESERVERS
BACK. These checks pin that behaviour, including the cases that matter most:
an encoding the registrar silently ignores must not be reported as success,
and a domain left on neither the requested nor the original nameservers must
be restored.
"""
import os
import sys
import xml.etree.ElementTree as ET

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

os.environ.setdefault("NAMECHEAP_API_USER", "test-user")
os.environ.setdefault("NAMECHEAP_API_KEY", "test-key")
os.environ.setdefault("NAMECHEAP_USERNAME", "test-user")
os.environ.setdefault("NAMECHEAP_CLIENT_IP", "127.0.0.1")

import namecheap_automation as nc

NSURL = "http://api.namecheap.com/xml.response"
ORIGINAL = ["dns1.registrar-servers.com", "dns2.registrar-servers.com"]
WANTED = ["houston.ns.cloudflare.com", "serena.ns.cloudflare.com"]


class Results:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def check(self, name: str, cond: bool, detail: str = "") -> bool:
        if cond:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed.append(name)
            print(f"  FAIL  {name}" + (f"\n          {detail}" if detail else ""))
        return bool(cond)

    def report(self) -> int:
        print()
        print(f"{self.passed} passed / {len(self.failed)} failed")
        for f in self.failed:
            print(f"  failed: {f}")
        return 1 if self.failed else 0


class FakeRegistrar:
    """A stateful Namecheap. `honours` decides which parameter encoding
    actually changes anything, which is the unknown under test."""

    def __init__(self, nameservers=None, honours="NameServers",
                 getlist_error=False, getlist_error_after=None,
                 corrupt_to=None):
        self.nameservers = list(nameservers if nameservers is not None
                                else ORIGINAL)
        self.honours = honours
        self.getlist_error = getlist_error
        self.getlist_error_after = getlist_error_after
        self.corrupt_to = corrupt_to
        self.calls: list[tuple] = []

    # -- the requests module's surface -----------------------------------
    def get(self, url, params=None, timeout=None):
        return self._handle(params or {})

    def post(self, url, data=None, timeout=None):
        return self._handle(data or {})

    def _handle(self, params):
        cmd = params.get("Command", "")
        self.calls.append((cmd, dict(params)))
        if cmd == "namecheap.domains.dns.getList":
            n = sum(1 for c, _ in self.calls
                    if c == "namecheap.domains.dns.getList")
            if self.getlist_error or (self.getlist_error_after is not None
                                      and n > self.getlist_error_after):
                return _Resp(self._error("Could not read"))
            inner = "".join("<Nameserver>%s</Nameserver>" % h
                            for h in self.nameservers)
            using = "true" if all(h.endswith("registrar-servers.com")
                                  for h in self.nameservers) else "false"
            return _Resp(
                f'<ApiResponse Status="OK" xmlns="{NSURL}"><CommandResponse>'
                f'<DomainDNSGetListResult Domain="x.site" '
                f'IsUsingOurDNS="{using}">{inner}'
                f'</DomainDNSGetListResult></CommandResponse></ApiResponse>')
        if cmd == "namecheap.domains.dns.setCustom":
            applied = self._apply(params)
            return _Resp(
                f'<ApiResponse Status="OK" xmlns="{NSURL}"><CommandResponse>'
                f'<DomainDNSSetCustomResult Domain="x.site" '
                f'Updated="{"true" if applied else "false"}"/>'
                f'</CommandResponse></ApiResponse>')
        return _Resp(self._error("unexpected command " + cmd))

    def _apply(self, params):
        if self.honours == "NameServers" and params.get("NameServers"):
            new = [h.strip() for h in params["NameServers"].split(",") if h.strip()]
        elif self.honours == "Nameserver1..N" and params.get("Nameserver1"):
            new = [params[k] for k in sorted(params)
                   if k.startswith("Nameserver") and k[10:].isdigit()]
        else:
            return False
        self.nameservers = self.corrupt_to if self.corrupt_to else new
        return True

    @staticmethod
    def _error(msg):
        return (f'<ApiResponse Status="ERROR" xmlns="{NSURL}">'
                f'<Errors><Error Number="2019166">{msg}</Error></Errors>'
                f'</ApiResponse>')

    def sets(self):
        return [p for c, p in self.calls
                if c == "namecheap.domains.dns.setCustom"]


class _Resp:
    def __init__(self, text):
        self.text = text
        self.status_code = 200

    def raise_for_status(self):
        return None


def with_registrar(**kw):
    fake = FakeRegistrar(**kw)
    nc.requests = fake
    return fake


def check_get_nameservers(r: Results) -> None:
    print("\n-- reading the registrar's nameservers --")
    fake = with_registrar()
    out = nc.get_nameservers("simon-mwangi.site")
    r.check("the nameservers are parsed", out.get("nameservers") == ORIGINAL,
            repr(out))
    r.check("registrar DNS is detected",
            out.get("using_registrar_dns") is True, repr(out))

    fake = with_registrar(nameservers=WANTED)
    out = nc.get_nameservers("simon-mwangi.site")
    r.check("custom DNS is detected as not the registrar's",
            out.get("using_registrar_dns") is False, repr(out))
    r.check("...and the custom pair is returned",
            out.get("nameservers") == WANTED, repr(out))

    fake = with_registrar(getlist_error=True)
    out = nc.get_nameservers("simon-mwangi.site")
    r.check("a provider error is an error", "error" in out, repr(out))
    r.check("...and keeps the error number",
            "2019166" in str(out.get("error", "")), repr(out))


def check_refuses_a_bad_request(r: Results) -> None:
    print("\n-- it refuses to delegate to something unusable --")
    for bad, why in [([], "nothing"), (["only.one.ns"], "one nameserver"),
                     (["a.ns", "a.ns"], "the same host twice"),
                     ([None, ""], "blanks")]:
        fake = with_registrar()
        out = nc.set_custom_dns("simon-mwangi.site", bad)
        r.check(f"refuses {why}", "error" in out, repr(out))
        r.check(f"...and issues no setCustom for {why}", fake.sets() == [],
                f"sent {fake.sets()!r}")

    # If the CURRENT nameservers cannot be read there is no rollback target,
    # so nothing may be changed.
    fake = with_registrar(getlist_error=True)
    out = nc.set_custom_dns("simon-mwangi.site", WANTED)
    r.check("refuses when the current nameservers cannot be read",
            "error" in out, repr(out))
    r.check("...and issues no setCustom", fake.sets() == [],
            f"sent {fake.sets()!r}")


def check_happy_path_each_encoding(r: Results) -> None:
    print("\n-- whichever parameter encoding the registrar honours, wins --")
    for honoured in ("NameServers", "Nameserver1..N"):
        fake = with_registrar(honours=honoured)
        out = nc.set_custom_dns("simon-mwangi.site", WANTED)
        r.check(f"{honoured}: the delegation succeeds",
                out.get("updated") is True, repr(out))
        r.check(f"{honoured}: the live nameservers are the requested ones",
                fake.nameservers == WANTED, repr(fake.nameservers))
        r.check(f"{honoured}: it reports which encoding worked",
                out.get("encoding") == honoured, repr(out))
        r.check(f"{honoured}: the previous pair is reported for rollback",
                out.get("previous") == ORIGINAL, repr(out))

    fake = with_registrar(honours="Nameserver1..N")
    nc.set_custom_dns("simon-mwangi.site", WANTED)
    r.check("the ignored encoding was tried first, then the working one",
            len(fake.sets()) == 2, f"{len(fake.sets())} setCustom calls")


def check_a_silently_ignored_encoding_is_not_success(r: Results) -> None:
    print("\n-- an encoding the registrar ignores is NOT reported as done --")
    fake = with_registrar(honours=None)          # honours nothing
    out = nc.set_custom_dns("simon-mwangi.site", WANTED)
    r.check("it is an error, not a success", "error" in out, repr(out))
    r.check("...and says the domain is unchanged",
            out.get("state") == "unchanged", repr(out))
    r.check("...and the live nameservers really are untouched",
            fake.nameservers == ORIGINAL, repr(fake.nameservers))
    r.check("...having tried both encodings", len(fake.sets()) == 2,
            f"{len(fake.sets())} setCustom calls")
    r.check("...and reports what each attempt returned",
            len(out.get("attempts") or []) == 2, repr(out))


def check_already_delegated_is_a_noop(r: Results) -> None:
    print("\n-- a domain already on the right nameservers is left alone --")
    fake = with_registrar(nameservers=WANTED)
    out = nc.set_custom_dns("simon-mwangi.site", WANTED)
    r.check("reported as already delegated", out.get("already") is True,
            repr(out))
    r.check("...not as an update", out.get("updated") is False, repr(out))
    r.check("...and no setCustom is issued at all", fake.sets() == [],
            f"sent {fake.sets()!r}")


def check_a_wrong_outcome_is_rolled_back(r: Results) -> None:
    print("\n-- landing on the WRONG nameservers triggers a restore --")
    fake = with_registrar(honours="NameServers",
                          corrupt_to=["ns1.someone-else.com",
                                      "ns2.someone-else.com"])
    out = nc.set_custom_dns("simon-mwangi.site", WANTED)
    r.check("it is reported as an error", "error" in out, repr(out))
    r.check("...naming what it actually landed on",
            "someone-else" in str(out.get("error", "")), repr(out))
    r.check("...and a restore was attempted",
            out.get("state") in ("restored", "INCONSISTENT"), repr(out))
    r.check("...and the restore is reported honestly",
            out.get("restored_to") is not None, repr(out))


def check_an_unreadable_readback_stops_everything(r: Results) -> None:
    print("\n-- if the result cannot be read, do not pile on another write --")
    # First getList (the pre-read) works; every later one fails.
    fake = with_registrar(honours="NameServers", getlist_error_after=1)
    out = nc.set_custom_dns("simon-mwangi.site", WANTED)
    r.check("it is an error", "error" in out, repr(out))
    r.check("...and the state is declared unknown",
            out.get("state") == "unknown", repr(out))
    r.check("...and only ONE setCustom was issued", len(fake.sets()) == 1,
            f"{len(fake.sets())} setCustom calls - trying a second encoding "
            f"on top of an unknown state is how a domain gets lost")


def main() -> int:
    print("test_delegation.py — setting nameservers at the registrar")
    saved = nc.requests
    r = Results()
    try:
        check_get_nameservers(r)
        check_refuses_a_bad_request(r)
        check_happy_path_each_encoding(r)
        check_a_silently_ignored_encoding_is_not_success(r)
        check_already_delegated_is_a_noop(r)
        check_a_wrong_outcome_is_rolled_back(r)
        check_an_unreadable_readback_stops_everything(r)
    finally:
        nc.requests = saved
    return r.report()


if __name__ == "__main__":
    raise SystemExit(main())
