#!/usr/bin/env python3
"""A published privacy policy is a legal claim about software.

The dangerous failure here is not getting the text wrong today. It is
publishing an accurate page, leaving it up, and letting the app change
underneath it - so a reviewer reads "this app cannot send data anywhere"
on the company's own domain while the Data safety form next to it says
something else. A policy that contradicts the declaration is worse than a
missing one: missing blocks a submission, contradictory invites a finding of
misrepresentation.

So the test that matters is the refusal. Both the publisher and the Worker
must stop when the facts stop supporting the sentences, and each is checked
separately, because the Worker also guards against a record edited into KV
by hand.
"""
from __future__ import annotations

import json
import os
import re
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

import play_declarations
import privacy_site


class Results:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, name: str, cond: bool, detail: str = "") -> bool:
        if cond:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed += 1
            print(f"  FAIL  {name}")
            if detail:
                print(f"          {detail}")
        return bool(cond)

    def report(self) -> int:
        print(f"\n{self.passed} passed / {self.failed} failed")
        return 1 if self.failed else 0


COMPANY = {
    "domain": "simon-mwangi.site",
    "company_name": "SIMON MWANGI LTD",
    "company_number": "16811331",
    "display_name": "Simon Mwangi",
    "support_email": "dev@simon-mwangi.site",
    "address": "4 Benson Close, Slough, SL2 5EH",
}

CLEAN_FACTS = {
    "readable": True, "errors": [], "permissions": [],
    "dependency_groups": ["androidx.core", "androidx.datastore"],
    "unknown_dependency_groups": [], "tracking_dependencies": [],
    "has_network_permission": False, "allow_backup": False,
}


def facts(**over):
    f = dict(CLEAN_FACTS)
    f.update(over)
    return f


def check_the_happy_record(r: Results) -> None:
    print("\n-- a clean app produces a record --")
    rec = privacy_site.build_record(COMPANY, facts(), today="2026-11-21")
    r.check("no error", "error" not in rec, repr(rec))
    r.check("collects_data is exactly False", rec.get("collects_data") is False,
            "the Worker refuses anything else, by design: " + repr(rec))
    for key, want in (("domain", "simon-mwangi.site"),
                      ("company_name", "SIMON MWANGI LTD"),
                      ("company_number", "16811331"),
                      ("contact_email", "dev@simon-mwangi.site"),
                      ("updated", "2026-11-21")):
        r.check(f"{key} carried through", rec.get(key) == want, repr(rec))
    r.check("the url is on the company's own domain",
            privacy_site.policy_url_for("simon-mwangi.site")
            == "https://simon-mwangi.site/privacy",
            privacy_site.policy_url_for("simon-mwangi.site"))


def check_it_refuses_when_the_facts_change(r: Results) -> None:
    """The whole point. Each of these makes a published sentence false."""
    print("\n-- it refuses to publish a claim the app no longer supports --")
    for over, phrase, why in (
        ({"has_network_permission": True,
          "permissions": ["android.permission.INTERNET"]},
         "network permission",
         "the page says the OS will not let it send data anywhere"),
        ({"tracking_dependencies": ["com.google.firebase"]},
         "may collect data",
         "the page says there is no analytics of any kind"),
        ({"unknown_dependency_groups": ["com.example.mystery"]},
         "not been assessed",
         "an unrecognised library is where a tracker arrives"),
        ({"readable": False, "errors": ["manifest unreadable"]},
         "could not be inspected",
         "'could not look' must not publish as 'collects nothing'"),
    ):
        rec = privacy_site.build_record(COMPANY, facts(**over))
        r.check(f"{list(over)[0]}: refused", "error" in rec,
                f"a false policy would be published: {rec!r}")
        r.check(f"{list(over)[0]}: the reason is specific",
                phrase in rec.get("error", ""), f"{why}: {rec.get('error')!r}")

    # The control: without it, "always refuse" would pass everything above.
    rec = privacy_site.build_record(COMPANY, facts())
    r.check("a clean app is NOT refused", "error" not in rec,
            "nothing could ever be published: " + repr(rec))


def check_it_refuses_incomplete_companies(r: Results) -> None:
    print("\n-- a company missing its own details is refused --")
    for drop, phrase in (("domain", "no domain"),
                         ("support_email", "contact address"),
                         ("company_name", "registered name")):
        co = dict(COMPANY)
        co[drop] = ""
        rec = privacy_site.build_record(co, facts())
        r.check(f"missing {drop} is refused", "error" in rec, repr(rec))
        r.check(f"...and says so ({drop})", phrase in rec.get("error", ""),
                repr(rec.get("error")))


def check_publish_refuses_before_touching_cloudflare(r: Results) -> None:
    """A refusal must not be reported as an outage, and must not write."""
    print("\n-- refusal happens before anything is written --")
    import cloudflare_dns

    calls = []
    saved = cloudflare_dns.find_zone
    cloudflare_dns.find_zone = lambda d: (calls.append(d) or
                                          {"found": True, "zone_id": "z"})
    try:
        out = privacy_site.publish_policy(
            COMPANY, facts(has_network_permission=True,
                           permissions=["android.permission.INTERNET"]))
    finally:
        cloudflare_dns.find_zone = saved

    r.check("status is 'refused', not 'error'", out.get("status") == "refused",
            "an outage and a false claim are different operator actions: "
            + repr(out))
    r.check("...and Cloudflare was never asked", calls == [],
            f"it went looking for a zone before deciding: {calls!r}")
    r.check("...and the reason is carried",
            "network permission" in str(out.get("reason")), repr(out))


def check_not_delegated_is_not_an_error(r: Results) -> None:
    print("\n-- a domain with no zone yet is a state, not a failure --")
    import cloudflare_dns

    saved = cloudflare_dns.find_zone
    cloudflare_dns.find_zone = lambda d: {"found": False}
    try:
        out = privacy_site.publish_policy(COMPANY, facts())
    finally:
        cloudflare_dns.find_zone = saved
    r.check("status is not_delegated", out.get("status") == "not_delegated",
            "the delegate stage simply has not run yet: " + repr(out))

    cloudflare_dns.find_zone = lambda d: {"error": "CLOUDFLARE_API_TOKEN is not set"}
    try:
        out = privacy_site.publish_policy(COMPANY, facts())
    finally:
        cloudflare_dns.find_zone = saved
    r.check("a lookup failure IS an error, separately",
            out.get("status") == "error",
            "'could not ask' read as 'not delegated': " + repr(out))


def check_dry_run_writes_nothing(r: Results) -> None:
    print("\n-- dry_run reaches a URL without writing --")
    import cloudflare_dns

    saved = cloudflare_dns.find_zone
    cloudflare_dns.find_zone = lambda d: {"found": True, "zone_id": "z1"}
    touched = []
    saved_ns, saved_put = privacy_site.ensure_kv_namespace, privacy_site.put_record
    privacy_site.ensure_kv_namespace = lambda *a, **k: touched.append("ns")
    privacy_site.put_record = lambda *a, **k: touched.append("put")
    try:
        out = privacy_site.publish_policy(COMPANY, facts(), dry_run=True)
    finally:
        cloudflare_dns.find_zone = saved
        privacy_site.ensure_kv_namespace = saved_ns
        privacy_site.put_record = saved_put
    r.check("it reports published with dry_run",
            out.get("status") == "published" and out.get("dry_run") is True,
            repr(out))
    r.check("...and nothing was written", touched == [], repr(touched))
    r.check("...and the url is right",
            out.get("url") == "https://simon-mwangi.site/privacy", repr(out))


def check_worker_matches_the_facts(r: Results) -> None:
    """Every claim in the rendered page must trace to something scan_app_facts
    observed. A sentence with no fact behind it is the defect."""
    print("\n-- the Worker's claims match what is actually checked --")
    js = open(os.path.join(REPO, "workers", "pipeline_privacy.js"),
              encoding="utf-8").read()

    r.check("it refuses a host with no record",
            "no policy record for" in js,
            "a generic policy for an uninspected domain would be a legal "
            "claim about software nobody looked at")
    r.check("it refuses a record that does not assert collects_data false",
            "collects_data !== false" in js,
            "a record edited into KV by hand could publish anything")
    r.check("it serves only /privacy",
            '!== "/privacy"' in js, "the apex would serve something too")
    r.check("it escapes interpolated values", "function esc(" in js
            and "&lt;" in js,
            "a company name with a < in it would inject markup")
    r.check("a missing KV binding is a 500, not a default page",
            "no POLICIES binding" in js, "it would fall through silently")

    # The page claims no permissions and no network. The publisher must be
    # the thing that establishes that, not the page.
    rec = privacy_site.build_record(COMPANY, facts(), today="2026-11-21")
    r.check("the record carries the permission list the claim rests on",
            rec.get("permissions") == [], repr(rec))
    r.check("...and the dependency list", rec.get("dependencies"),
            "a reader cannot check 'no third-party SDKs' without it")


def check_it_is_wired_against_the_same_source(r: Results) -> None:
    """The policy and the Data safety answers must not have separate sources."""
    print("\n-- policy and declarations share one source of truth --")
    src = open(os.path.join(REPO, "privacy_site.py"), encoding="utf-8").read()
    r.check("build_record derives from play_declarations.scan_app_facts",
            "play_declarations.scan_app_facts()" in src,
            "a second source of truth will eventually disagree with the "
            "Data safety form, in front of the reviewer reading both")

    # And prove they agree right now on the real app.
    real = play_declarations.scan_app_facts()
    rec = privacy_site.build_record(COMPANY, real)
    decl = play_declarations.declarations_for(
        privacy_policy_url=privacy_site.policy_url_for("simon-mwangi.site"),
        support_email="dev@simon-mwangi.site")
    r.check("the real app still supports publishing", "error" not in rec,
            repr(rec.get("error")))
    r.check("...and Data safety still answers No",
            decl["data_safety_collection"]["answer"] == "No",
            "the two disagree: " + repr(decl["data_safety_collection"]))
    r.check("...and the policy URL makes the declaration derived",
            decl["privacy_policy"]["status"] == play_declarations.DERIVED,
            repr(decl["privacy_policy"]))
    r.check("...so nothing is left needing a human",
            decl["summary"]["all_answered"] is True,
            repr(decl["summary"]["needs_human"]))


def main() -> int:
    r = Results()
    print("test_privacy_site.py - a published policy is a claim about software")
    check_the_happy_record(r)
    check_it_refuses_when_the_facts_change(r)
    check_it_refuses_incomplete_companies(r)
    check_publish_refuses_before_touching_cloudflare(r)
    check_not_delegated_is_not_an_error(r)
    check_dry_run_writes_nothing(r)
    check_worker_matches_the_facts(r)
    check_it_is_wired_against_the_same_source(r)
    return r.report()


if __name__ == "__main__":
    raise SystemExit(main())
