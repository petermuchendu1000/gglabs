#!/usr/bin/env python3
"""The declarations are statements to Google, so a wrong one is not a typo.

An inaccurate Data safety answer is a policy violation that can take the
developer account down and every app with it. The whole module therefore rests
on one claim - "the app has no INTERNET permission, so it cannot transmit
anything" - and the dangerous failure is not getting that wrong today. It is
getting it RIGHT today, storing the answer, and going on submitting it after
somebody adds a network permission or an analytics SDK.

So the central test here is not "does it say No". It is: when the app changes
underneath it, does the answer STOP being No. A module that cannot fail that
test is a module that will one day file a false declaration, correctly
formatted, on every company in the pipeline.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

import play_declarations as pd


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


MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">
%s
    <application android:allowBackup="false" android:label="@string/app_name">
        <activity android:name=".MainActivity" android:exported="true"/>
    </application>
</manifest>
"""

TOML = """[versions]
agp = "8.7.3"

[libraries]
%s
"""

CLEAN_LIBS = """androidx-core-ktx = { group = "androidx.core", name = "core-ktx" }
androidx-datastore = { group = "androidx.datastore", name = "datastore-preferences" }
"""


def make_app(tmp: Path, permissions=(), libs=CLEAN_LIBS) -> str:
    root = tmp / "template"
    (root / "app" / "src" / "main").mkdir(parents=True, exist_ok=True)
    (root / "gradle").mkdir(parents=True, exist_ok=True)
    perms = "\n".join(
        f'    <uses-permission android:name="{p}" />' for p in permissions)
    (root / "app" / "src" / "main" / "AndroidManifest.xml").write_text(
        MANIFEST % perms, encoding="utf-8")
    (root / "gradle" / "libs.versions.toml").write_text(
        TOML % libs, encoding="utf-8")
    return str(root)


# ── The real app ───────────────────────────────────────────────────────────

def check_the_shipped_app(r: Results) -> None:
    """Against the template that actually ships, not a fixture."""
    print("\n-- the real apps/template-shift-journal --")
    facts = pd.scan_app_facts()
    r.check("the shipped template is readable", facts["readable"] is True,
            repr(facts.get("errors")))
    r.check("it declares zero permissions", facts["permissions"] == [],
            f"found {facts['permissions']!r}; if a permission was added "
            f"deliberately, the declarations below must be re-derived")
    r.check("it has no network permission",
            facts["has_network_permission"] is False, repr(facts))
    r.check("it pulls in no tracking SDK",
            facts["tracking_dependencies"] == [],
            repr(facts["tracking_dependencies"]))
    r.check("every dependency is a recognised local-only group",
            facts["unknown_dependency_groups"] == [],
            f"unrecognised: {facts['unknown_dependency_groups']!r} - these "
            f"force the data-safety answers to needs_human, which is correct "
            f"but means nothing is pre-answered any more")
    r.check("allowBackup is off", facts["allow_backup"] is False,
            "app data would be copied to Google's backup servers, which "
            "changes the data-safety answer")

    d = pd.declarations_for(privacy_policy_url="https://example.invalid/p",
                            support_email="dev@example.invalid")
    r.check("data collection is answered No",
            d["data_safety_collection"]["answer"] == "No", repr(d["data_safety_collection"]))
    r.check("...as DERIVED, not as a default",
            d["data_safety_collection"]["status"] == pd.DERIVED,
            "an answer nobody can check is an answer nobody should send")
    r.check("...and it quotes the fact it rests on",
            "INTERNET" in d["data_safety_collection"]["basis"],
            repr(d["data_safety_collection"]["basis"]))
    r.check("ads is answered No", d["ads"]["answer"] == "No", repr(d["ads"]))
    r.check("nothing is left needing a human",
            d["summary"]["all_answered"] is True,
            f"outstanding: {d['summary']['needs_human']!r}")


# ── The failure that matters ───────────────────────────────────────────────

def check_adding_internet_revokes_the_answer(r: Results) -> None:
    print("\n-- a network permission must REVOKE 'collects nothing' --")
    tmp = Path(tempfile.mkdtemp())
    try:
        # The control first: the fixture with no permissions behaves like the
        # real app, or everything below is measuring the fixture, not the code.
        root = make_app(tmp, permissions=())
        d = pd.declarations_for(template_root=root)
        r.check("the clean fixture answers No",
                d["data_safety_collection"]["answer"] == "No",
                "the fixture does not reproduce the shipped app, so the "
                "mutation below proves nothing")

        for perm in ("android.permission.INTERNET",
                     "android.permission.ACCESS_NETWORK_STATE"):
            shutil.rmtree(root, ignore_errors=True)
            root = make_app(tmp, permissions=(perm,))
            d = pd.declarations_for(template_root=root)
            for key in ("data_safety_collection", "data_safety_sharing",
                        "data_safety_encryption", "data_deletion"):
                r.check(f"{perm.split('.')[-1]}: {key} needs a human",
                        d[key]["status"] == pd.NEEDS_HUMAN,
                        f"a false declaration would be filed: {d[key]!r}")
            r.check(f"{perm.split('.')[-1]}: the answer is not 'No'",
                    d["data_safety_collection"]["answer"] != "No",
                    "the old answer survived a change that invalidates it")
            r.check(f"{perm.split('.')[-1]}: the reason names the permission",
                    perm in d["data_safety_collection"]["basis"],
                    repr(d["data_safety_collection"]["basis"]))
            r.check(f"{perm.split('.')[-1]}: the set is NOT all answered",
                    d["summary"]["all_answered"] is False,
                    "the dossier would report itself complete")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_a_tracking_sdk_revokes_the_answer(r: Results) -> None:
    print("\n-- an analytics or ads SDK must revoke it too --")
    tmp = Path(tempfile.mkdtemp())
    try:
        for group, label in (
            ("com.google.android.gms.ads", "an ads SDK"),
            ("com.google.firebase", "Firebase"),
            ("com.appsflyer", "an attribution SDK"),
            ("io.sentry", "crash reporting"),
        ):
            root = make_app(tmp, libs=CLEAN_LIBS +
                            f'x = {{ group = "{group}", name = "n" }}\n')
            d = pd.declarations_for(template_root=root)
            r.check(f"{label}: data collection needs a human",
                    d["data_safety_collection"]["status"] == pd.NEEDS_HUMAN,
                    repr(d["data_safety_collection"]))
            r.check(f"{label}: ads needs a human",
                    d["ads"]["status"] == pd.NEEDS_HUMAN, repr(d["ads"]))
            shutil.rmtree(root, ignore_errors=True)

        # An unrecognised dependency is not assumed harmless: a tracker that
        # does not match a marker arrives exactly this way.
        root = make_app(tmp, libs=CLEAN_LIBS +
                        'x = { group = "com.example.unknownthing", name = "n" }\n')
        d = pd.declarations_for(template_root=root)
        r.check("an unrecognised dependency also needs a human",
                d["data_safety_collection"]["status"] == pd.NEEDS_HUMAN,
                "a tracker whose name matches no marker would pass silently: "
                + repr(d["data_safety_collection"]))
        r.check("...and it is named in the reason",
                "com.example.unknownthing" in d["data_safety_collection"]["basis"],
                repr(d["data_safety_collection"]["basis"]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_unreadable_app_is_not_a_clean_app(r: Results) -> None:
    """"Could not look" must not read as "found nothing"."""
    print("\n-- an unreadable template answers nothing --")
    facts = pd.scan_app_facts("/nonexistent-template-root")
    r.check("readable is False", facts["readable"] is False, repr(facts))
    r.check("permissions is empty but readable says why",
            facts["permissions"] == [] and facts["errors"],
            "an empty permission list from an unreadable manifest is exactly "
            "how 'could not ask' becomes 'collects nothing'")

    d = pd.build_declarations(facts, "https://example.invalid/p", "a@b.invalid")
    r.check("data collection is NOT answered No",
            d["data_safety_collection"]["answer"] != "No",
            repr(d["data_safety_collection"]))
    r.check("every code-dependent answer needs a human",
            all(d[k]["status"] == pd.NEEDS_HUMAN for k in pd._CODE_DEPENDENT),
            repr({k: d[k]["status"] for k in pd._CODE_DEPENDENT}))
    r.check("the set is not all answered",
            d["summary"]["all_answered"] is False, repr(d["summary"]))


def check_missing_policy_and_email(r: Results) -> None:
    print("\n-- the two that come from pipeline data --")
    d = pd.declarations_for(privacy_policy_url="", support_email="")
    r.check("a missing privacy policy needs a human",
            d["privacy_policy"]["status"] == pd.NEEDS_HUMAN,
            "Play Console will not accept the app without one")
    r.check("a missing support email needs a human",
            d["support_email"]["status"] == pd.NEEDS_HUMAN,
            "Play review would have nowhere to write")
    r.check("...so the set is not all answered",
            d["summary"]["all_answered"] is False, repr(d["summary"]))

    d = pd.declarations_for(privacy_policy_url="https://x.invalid/p",
                            support_email="dev@x.invalid")
    r.check("both present are DERIVED",
            d["privacy_policy"]["status"] == pd.DERIVED
            and d["support_email"]["status"] == pd.DERIVED,
            "the control: otherwise the two checks above are vacuous")
    r.check("the policy URL is carried through verbatim",
            d["privacy_policy"]["answer"] == "https://x.invalid/p",
            repr(d["privacy_policy"]))


def check_business_choices_are_flagged(r: Results) -> None:
    """Age rating is a legal choice, not a property of the bytecode."""
    print("\n-- business choices are marked, not disguised as facts --")
    d = pd.declarations_for(privacy_policy_url="https://x.invalid/p",
                            support_email="dev@x.invalid")
    r.check("target audience is RECOMMENDED, not DERIVED",
            d["target_audience"]["status"] == pd.RECOMMENDED,
            "presenting an age rating as derived from the code invites "
            "somebody to tick it without reading")
    r.check("...and it warns about the under-13 trap",
            "Families" in d["target_audience"]["note"],
            repr(d["target_audience"]["note"]))
    r.check("recommended entries are listed in the summary",
            "target_audience" in d["summary"]["recommended"],
            repr(d["summary"]))
    r.check("a recommended answer does not block all_answered",
            d["summary"]["all_answered"] is True,
            "only needs_human blocks; a recommendation is answered")

    # Every entry must carry its reasoning, or none of this is checkable.
    entries = {k: v for k, v in d.items()
               if k not in ("summary", "app_facts")}
    r.check(f"all {len(entries)} entries carry a basis",
            all(v.get("basis") for v in entries.values()),
            repr([k for k, v in entries.items() if not v.get("basis")]))
    r.check("all entries carry a question",
            all(v.get("question") for v in entries.values()),
            repr([k for k, v in entries.items() if not v.get("question")]))
    r.check("every status is one of the three",
            all(v["status"] in (pd.DERIVED, pd.RECOMMENDED, pd.NEEDS_HUMAN)
                for v in entries.values()),
            repr({k: v["status"] for k, v in entries.items()}))


def check_wired_into_the_dossier(r: Results) -> None:
    print("\n-- the dossier carries them --")
    import play_console_readiness as pcr
    rec = pcr.build_readiness_record({
        "company_number": "16811331",
        "details": {"company_name": "SIMON MWANGI LTD", "address": "1 Test St"},
        "domain": {"domain": "simon-mwangi.site"},
        "email": {"email": "owner@example.invalid"},
        "play_console": {"developer_email": "dev@simon-mwangi.site",
                         "privacy_policy_url": "https://x.invalid/p"},
    })
    r.check("the record has a declarations section", "declarations" in rec,
            "a human would still answer all twelve by hand")
    d = rec["declarations"]
    r.check("the support email is the company's dev@ address",
            d["support_email"]["answer"] == "dev@simon-mwangi.site",
            repr(d["support_email"]))
    r.check("the declarations are all answered for this company",
            d["summary"]["all_answered"] is True,
            repr(d["summary"]["needs_human"]))
    r.check("the app facts are carried for checking",
            rec["declarations"]["app_facts"]["readable"] is True,
            "a human cannot verify the reasoning without them")

    # The readiness summary is about ACCOUNT signup and must not have been
    # silently widened by this change.
    r.check("readiness still reports on account signup only",
            "ready_for_human_signup" in rec["readiness"],
            repr(rec["readiness"].keys()))
    r.check("...and no declaration key leaked into its checks",
            not any(k.startswith(("data_safety", "content_rating"))
                    for k in rec["readiness"]["checks"]),
            repr(sorted(rec["readiness"]["checks"])))


def check_stale_provider_strings_are_gone(r: Results) -> None:
    """The dossier told a human to look at Namecheap for mail and DNS.

    Both moved to Cloudflare. A document whose job is to tell somebody what is
    true must not name a route that no longer exists."""
    print("\n-- the dossier names the right providers --")
    import play_console_readiness as pcr
    rec = pcr.build_readiness_record({
        "company_number": "16811331",
        "details": {"company_name": "SIMON MWANGI LTD"},
        "domain": {"domain": "simon-mwangi.site"},
        "email": {"email": "owner@example.invalid"},
        "play_console": {"developer_email": "dev@simon-mwangi.site"},
    })
    fwd = rec["developer_contact"]["email_forwarding"]
    r.check("mail forwarding names Cloudflare",
            "Cloudflare" in fwd["provider"], repr(fwd))
    r.check("...and no longer names Namecheap",
            "Namecheap" not in fwd["provider"] and "Namecheap" not in fwd["note"],
            "Namecheap forwarding requires Namecheap DNS and cannot survive "
            "the delegation this pipeline performs: " + repr(fwd))
    dns = rec["dns_verification"]
    r.check("TXT verification names Cloudflare",
            "Cloudflare" in dns["provider"], repr(dns))
    r.check("...and no longer names Namecheap",
            "Namecheap" not in dns["provider"] and "Namecheap" not in dns["note"],
            repr(dns))


def main() -> int:
    r = Results()
    print("test_play_declarations.py - a declaration is a statement to Google")
    check_the_shipped_app(r)
    check_adding_internet_revokes_the_answer(r)
    check_a_tracking_sdk_revokes_the_answer(r)
    check_unreadable_app_is_not_a_clean_app(r)
    check_missing_policy_and_email(r)
    check_business_choices_are_flagged(r)
    check_wired_into_the_dossier(r)
    check_stale_provider_strings_are_gone(r)
    return r.report()


if __name__ == "__main__":
    raise SystemExit(main())
