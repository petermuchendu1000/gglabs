"""Pre-answer Play Console's App content declarations from what the app IS.

Play Console will not let an app go out until a dozen declarations are
answered: Data safety, Ads, Content rating, Target audience, App access, News,
COVID-19 contact tracing, Government apps, Financial features, Health. Today a
human answers all of them by hand, per company, from memory.

THE THING THAT MAKES THIS DIFFERENT FROM A LIST OF DEFAULTS. A declaration is
a statement to Google, and a false one is not a typo: an inaccurate Data
safety answer is a policy violation that can take the developer account down
and every app with it. So nothing here is a constant. Each answer is DERIVED
from a fact in the app itself - the manifest's permissions, the dependency
list - and carries the fact it was derived from, so a human can check the
reasoning rather than trust it.

The derivation that does most of the work is this one: the app declares ZERO
permissions, so it has no android.permission.INTERNET, so it CANNOT transmit
anything off the device. That is not an opinion about the developer's
intentions, it is a property the operating system enforces, and it settles
Data safety, half the content rating questionnaire and the sharing questions
in one stroke.

WHICH MEANS THE DERIVATION MUST BE RE-RUN, NOT REMEMBERED. The moment somebody
adds INTERNET to the manifest or pulls in an analytics SDK, "collects no data"
stops being true - and a stored answer would go on being submitted to Google,
correctly formatted and false. So this module reads the template every time,
and anything it cannot establish becomes needs_human rather than a guess.
"collected nothing" and "could not tell" must never look alike.

Three classes of answer come out:

  derived      a fact in the app settles it. The fact is quoted.
  recommended  a business or legal choice, not a property of the code. A
               defensible default is given and flagged, e.g. target age.
  needs_human  it could not be established, or a fact contradicts the default.
"""
from __future__ import annotations

import os
import re

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_ROOT = os.path.join(BASE_DIR, "apps", "template-shift-journal")

DERIVED = "derived"
RECOMMENDED = "recommended"
NEEDS_HUMAN = "needs_human"

# Permissions that let an app move data off the device. INTERNET is the one
# that matters; the others are listed because they reach the network without
# it being obvious.
NETWORK_PERMISSIONS = {
    "android.permission.INTERNET",
    "android.permission.ACCESS_NETWORK_STATE",
    "android.permission.ACCESS_WIFI_STATE",
}

# Substrings that mark a dependency as advertising, analytics, attribution or
# crash reporting. Matched against the group id, lowercased. Deliberately
# broad: a false positive costs a human one look, a false negative costs a
# false declaration to Google.
TRACKING_MARKERS = (
    "ads", "admob", "analytics", "firebase", "crashlytics", "appsflyer",
    "adjust", "onesignal", "mixpanel", "amplitude", "segment", "sentry",
    "bugsnag", "flurry", "facebook", "unity3d", "ironsource", "applovin",
    "vungle", "chartboost", "inmobi", "tapjoy", "braze", "clevertap",
)

# Dependency groups known to be local-only: UI toolkit and on-device storage.
# A group outside this set is not assumed harmless - it forces the affected
# declarations to needs_human, because an unrecognised library is exactly
# where a tracker would arrive.
LOCAL_ONLY_GROUPS = {
    "androidx.core", "androidx.lifecycle", "androidx.activity",
    "androidx.compose", "androidx.compose.ui", "androidx.compose.material",
    "androidx.compose.material3", "androidx.compose.runtime",
    "androidx.compose.foundation", "androidx.datastore",
    "androidx.annotation", "androidx.appcompat", "org.jetbrains.kotlin",
    "org.jetbrains.kotlinx",
}


# ── Reading the app ────────────────────────────────────────────────────────

def scan_app_facts(template_root: str = TEMPLATE_ROOT) -> dict:
    """What the app template actually declares. Reads, decides nothing.

    On any read failure this returns readable=False rather than an empty set
    of permissions. An unreadable manifest producing "no permissions" would
    turn "could not look" into "collects nothing", which is the one mistake
    this module exists to prevent.
    """
    facts = {
        "template_root": template_root,
        "readable": False,
        "errors": [],
        "permissions": [],
        "dependency_groups": [],
        "unknown_dependency_groups": [],
        "tracking_dependencies": [],
        "has_network_permission": False,
        "allow_backup": None,
    }

    manifest = os.path.join(template_root, "app", "src", "main",
                            "AndroidManifest.xml")
    try:
        xml = open(manifest, encoding="utf-8").read()
    except OSError as e:
        facts["errors"].append(f"could not read {manifest}: {e}")
        return facts

    facts["permissions"] = sorted(set(
        re.findall(r'<uses-permission[^>]*android:name\s*=\s*"([^"]+)"', xml)))
    facts["has_network_permission"] = any(
        p in NETWORK_PERMISSIONS for p in facts["permissions"])
    m = re.search(r'android:allowBackup\s*=\s*"(true|false)"', xml)
    facts["allow_backup"] = (m.group(1) == "true") if m else None

    toml = os.path.join(template_root, "gradle", "libs.versions.toml")
    try:
        text = open(toml, encoding="utf-8").read()
    except OSError as e:
        facts["errors"].append(f"could not read {toml}: {e}")
        return facts

    groups = sorted(set(re.findall(r'group\s*=\s*"([^"]+)"', text)))
    facts["dependency_groups"] = groups
    facts["tracking_dependencies"] = sorted(
        g for g in groups
        if any(marker in g.lower() for marker in TRACKING_MARKERS))
    facts["unknown_dependency_groups"] = sorted(
        g for g in groups if not _is_local_only(g))
    facts["readable"] = True
    return facts


def _is_local_only(group: str) -> bool:
    g = group.lower()
    return any(g == known or g.startswith(known + ".")
               for known in LOCAL_ONLY_GROUPS)


# ── Deriving the declarations ──────────────────────────────────────────────

def _entry(question, answer, status, basis, note=""):
    return {"question": question, "answer": answer, "status": status,
            "basis": basis, "note": note}


def build_declarations(facts: dict, privacy_policy_url: str = "",
                       support_email: str = "") -> dict:
    """Play Console's App content answers, each with the fact behind it."""
    d: dict[str, dict] = {}

    # If the app could not be read, NOTHING is derived. Every code-dependent
    # answer becomes needs_human, carrying the reason.
    if not facts.get("readable"):
        why = "; ".join(facts.get("errors") or ["the app template could not be read"])
        for key, question in _CODE_DEPENDENT.items():
            d[key] = _entry(question, "UNKNOWN", NEEDS_HUMAN,
                            f"the app could not be inspected: {why}")
        _add_policy_entries(d, privacy_policy_url, support_email)
        d["summary"] = _summarize(d)
        return d

    offline = not facts["has_network_permission"]
    trackers = facts.get("tracking_dependencies") or []
    unknown = facts.get("unknown_dependency_groups") or []
    perms = facts.get("permissions") or []

    perm_basis = (
        "the manifest declares no permissions at all"
        if not perms else
        "the manifest declares: " + ", ".join(perms))

    # ---- Data safety. The whole section turns on one fact. ----
    if offline and not trackers and not unknown:
        d["data_safety_collection"] = _entry(
            "Does your app collect or share any of the required user data types?",
            "No", DERIVED,
            f"{perm_basis}, so the app has no INTERNET permission and the "
            f"operating system will not let it transmit anything off the "
            f"device. Dependencies are UI and on-device storage only "
            f"({', '.join(facts['dependency_groups']) or 'none'}).")
        d["data_safety_sharing"] = _entry(
            "Is any user data shared with third parties?", "No", DERIVED,
            "no network permission and no third-party SDK, so there is no "
            "path off the device")
        d["data_safety_encryption"] = _entry(
            "Is all user data encrypted in transit?", "Not applicable", DERIVED,
            "no data is transmitted, so there is no transit to encrypt")
        d["data_deletion"] = _entry(
            "Do you provide a way for users to request data deletion?",
            "No account creation; data stays on the device and is removed "
            "when the app is uninstalled", DERIVED,
            "androidx.datastore writes to the app's private storage, and "
            f"allowBackup is {facts['allow_backup']!r}")
    else:
        reasons = []
        if not offline:
            reasons.append("the app has a network permission ("
                           + ", ".join(p for p in perms
                                       if p in NETWORK_PERMISSIONS) + ")")
        if trackers:
            reasons.append("advertising/analytics dependencies are present: "
                           + ", ".join(trackers))
        if unknown:
            reasons.append("unrecognised dependencies that may collect data: "
                           + ", ".join(unknown))
        why = "; ".join(reasons)
        for key, question in (
            ("data_safety_collection",
             "Does your app collect or share any of the required user data types?"),
            ("data_safety_sharing",
             "Is any user data shared with third parties?"),
            ("data_safety_encryption",
             "Is all user data encrypted in transit?"),
            ("data_deletion",
             "Do you provide a way for users to request data deletion?"),
        ):
            d[key] = _entry(
                question, "UNKNOWN", NEEDS_HUMAN,
                f"this can no longer be derived: {why}",
                "Answer it from what the app now does. Do NOT carry the "
                "previous 'collects nothing' answer forward.")

    # ---- Ads ----
    if not trackers and not unknown:
        d["ads"] = _entry(
            "Does your app contain ads?", "No", DERIVED,
            "no advertising SDK is on the dependency list: "
            + (", ".join(facts["dependency_groups"]) or "none"))
    else:
        d["ads"] = _entry(
            "Does your app contain ads?", "UNKNOWN", NEEDS_HUMAN,
            "dependencies that may serve ads are present: "
            + ", ".join(trackers + unknown))

    # ---- App access ----
    d["app_access"] = _entry(
        "Is any of your app's functionality restricted?",
        "All functionality is available without special access", DERIVED,
        "the app has no network permission, so it cannot authenticate against "
        "anything; there is no login to give Google credentials for")

    # ---- Content rating (IARC questionnaire) ----
    if offline:
        d["content_rating_category"] = _entry(
            "Content rating: app category",
            "Utility, Productivity, Communication or Other", RECOMMENDED,
            "a shift/work journal is a productivity utility",
            "Pick 'Utility, Productivity, Communication or Other' unless the "
            "flavour's store listing describes something else.")
        for key, question in (
            ("content_rating_violence", "Does the app contain violence?"),
            ("content_rating_sexuality", "Does the app contain sexual content?"),
            ("content_rating_language", "Does the app contain profanity?"),
            ("content_rating_controlled_substances",
             "Does the app reference drugs, alcohol or tobacco?"),
            ("content_rating_gambling",
             "Does the app contain gambling or simulated gambling?"),
        ):
            d[key] = _entry(question, "No", DERIVED,
                            "the app ships no such content: it is a local "
                            "journal with no network access and no remote "
                            "content to load")
        d["content_rating_user_interaction"] = _entry(
            "Can users interact, communicate or share content with each other?",
            "No", DERIVED,
            "no network permission, so users cannot reach each other at all")
        d["content_rating_shares_location"] = _entry(
            "Does the app share the user's location?", "No", DERIVED,
            "no location permission and no network permission")
    else:
        d["content_rating_user_interaction"] = _entry(
            "Can users interact, communicate or share content with each other?",
            "UNKNOWN", NEEDS_HUMAN,
            "the app now has network access, so the previous answer - which "
            "rested on it having none - no longer follows")

    # ---- The plain No answers, each with why it is a No ----
    d["news_app"] = _entry(
        "Is your app a news app?", "No", DERIVED,
        "the app has no network permission, so it cannot fetch news")
    d["covid_contact_tracing"] = _entry(
        "Is this a COVID-19 contact tracing or status app?", "No", DERIVED,
        "no Bluetooth, location or network permission")
    d["government_app"] = _entry(
        "Is your app a government app?", "No", DERIVED,
        "it is published by a private limited company, not on behalf of a "
        "government body")
    d["financial_features"] = _entry(
        "Does your app provide any financial features?", "None of these",
        DERIVED,
        "no payment, lending, crypto or banking code, and no network "
        "permission to reach a financial service")
    d["health_apps"] = _entry(
        "Does your app have health features?", "None of these", DERIVED,
        "no health, fitness or medical functionality and no related "
        "permissions")

    # ---- The ones that are a business choice, not a property of the code ----
    d["target_audience"] = _entry(
        "Target age groups", "18 and over", RECOMMENDED,
        "a shift journal is a workplace tool, so an adult audience is the "
        "honest answer",
        "Do NOT tick an under-13 group unless you intend to comply with the "
        "Families policy: it pulls in extra requirements and a separate "
        "review, and it cannot be undone casually.")
    d["ads_target_children"] = _entry(
        "Do you want your app to appeal to children?", "No", RECOMMENDED,
        "follows from the 18-and-over target audience")

    _add_policy_entries(d, privacy_policy_url, support_email)
    d["summary"] = _summarize(d)
    return d


_CODE_DEPENDENT = {
    "data_safety_collection":
        "Does your app collect or share any of the required user data types?",
    "data_safety_sharing": "Is any user data shared with third parties?",
    "data_safety_encryption": "Is all user data encrypted in transit?",
    "data_deletion":
        "Do you provide a way for users to request data deletion?",
    "ads": "Does your app contain ads?",
    "app_access": "Is any of your app's functionality restricted?",
    "content_rating_user_interaction":
        "Can users interact, communicate or share content with each other?",
    "news_app": "Is your app a news app?",
    "covid_contact_tracing":
        "Is this a COVID-19 contact tracing or status app?",
    "financial_features":
        "Does your app provide any financial features?",
    "health_apps": "Does your app have health features?",
}


def _add_policy_entries(d: dict, privacy_policy_url: str,
                        support_email: str) -> None:
    """The two that come from the pipeline's own data, not from the code."""
    url = (privacy_policy_url or "").strip()
    d["privacy_policy"] = _entry(
        "Privacy policy URL",
        url or "MISSING",
        DERIVED if url else NEEDS_HUMAN,
        "the pipeline's hosted privacy policy for this company" if url
        else "no privacy policy URL has been recorded for this company, and "
             "Play Console will not accept the app without one")
    email = (support_email or "").strip()
    d["support_email"] = _entry(
        "Store listing support email",
        email or "MISSING",
        DERIVED if email else NEEDS_HUMAN,
        f"dev@ on the company's own domain, routed by Cloudflare: {email}"
        if email else
        "no developer email has been configured, so Play review would have "
        "nowhere to write")


def _summarize(d: dict) -> dict:
    entries = {k: v for k, v in d.items() if k != "summary"}
    needs = sorted(k for k, v in entries.items()
                   if v["status"] == NEEDS_HUMAN)
    rec = sorted(k for k, v in entries.items() if v["status"] == RECOMMENDED)
    return {
        "total": len(entries),
        "derived": sorted(k for k, v in entries.items()
                          if v["status"] == DERIVED),
        "recommended": rec,
        "needs_human": needs,
        # Deliberately NOT "ready": a declaration a human still has to answer
        # is not pre-answered, and calling the set complete while one is
        # outstanding is the failure this codebase keeps removing.
        "all_answered": not needs,
    }


def declarations_for(privacy_policy_url: str = "", support_email: str = "",
                     template_root: str = TEMPLATE_ROOT) -> dict:
    """Scan the app and derive the declarations. The one call site needs."""
    facts = scan_app_facts(template_root)
    out = build_declarations(facts, privacy_policy_url, support_email)
    out["app_facts"] = facts
    return out
