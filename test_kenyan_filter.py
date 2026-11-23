#!/usr/bin/env python3
"""Only companies with a Kenyan director may enter the pipeline.

    python3 test_kenyan_filter.py

Hermetic: no network, no database. The officer records below are real ones
captured from the live Companies House API, so the rule is exercised
against what the data actually looks like rather than what it ought to.

Why the rule costs what it does. /search/companies carries NO officer data
at all - measured, its item keys are address, address_snippet,
company_number, company_status, company_type, date_of_creation,
description, description_identifier, kind, links, matches, snippet, title -
and /advanced-search/companies silently IGNORES a nationality parameter
(hits came back 20075 with `nationality`, `officer_nationality`,
`director_nationality`, and with none). Nationality exists only on
/company/{cn}/officers, so each candidate costs one extra request against a
budget of 600 per 5 minutes.

The rule: admit iff at least one ACTIVE DIRECTOR is Kenyan AND no active
director's nationality is unrecorded. Both halves were chosen against real
cases, which are the fixtures below.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).parent
sys.path.insert(0, str(REPO))

os.environ.setdefault("DATABASE_URL", "postgresql://unused@127.0.0.1:1/none")
os.environ.setdefault("LOG_LEVEL", "CRITICAL")

import requests  # noqa: E402

import api_v2  # noqa: E402


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


def officer(nat, role="director", resigned=None) -> dict:
    return {"nationality": nat, "officer_role": role, "resigned_on": resigned}


# Captured live. Each name is a real company and each list is its real set
# of active directors' nationality values.
FC009164 = [officer("Canadian"), officer("Kenyan"), officer("Kenyan"),
            officer("Kenyan"), officer("Swedish")]              # KENYA AIRWAYS PLC
FC010541 = [officer("Kenyan")] * 11 + [officer("New Zealander")]  # KENYA FLAMINGO
SC461938 = [officer("British"), officer("British"),
            officer("British,Kenyan"), officer("Greek")]        # KWISA
ACME     = [officer("British")]                                 # ACME ADVISORY LTD
NAIROBI  = [officer("British,Irish"), officer(None)]            # NAIROBI BUSINESS ANGELS
# A confirmed Kenyan director sitting next to one whose nationality nobody
# filed. This is the case the rule change is about, and it is common: the
# docstring's own sample had 2 of 60 directors with no nationality on
# record, so any company with a handful of directors is a coin toss.
KENYAN_PLUS_BLANK = [officer("Kenyan"), officer(None)]
KENYAN_PLUS_TWO_BLANK = [officer("Kenyan"), officer(None), officer(None)]


# ── the nationality parser ──────────────────────────────────────────────
def check_parser(r: Results) -> None:
    print("\n-- nationality is free text and must be split on commas only --")
    n = api_v2._nationalities
    r.check("a single value", n(officer("Kenyan")) == ["kenyan"])
    r.check("a dual national splits on the comma",
            n(officer("British,Kenyan")) == ["british", "kenyan"])
    # 'South African' is one nationality containing a space. Splitting on
    # whitespace would invent two, and 'african' is not a nationality.
    r.check("a value containing a space stays one value",
            n(officer("South African")) == ["south african"],
            "splitting on whitespace would invent a nationality")
    r.check("None gives nothing", n(officer(None)) == [])
    r.check("empty gives nothing", n(officer("")) == [])
    r.check("case is normalised", n(officer("KENYAN")) == ["kenyan"])
    r.check("surrounding space is stripped", n(officer("  kenyan  ")) == ["kenyan"])
    r.check("spacing after the comma is tolerated",
            n(officer("British, Kenyan")) == ["british", "kenyan"])
    # Must not match a nationality that merely contains the letters.
    r.check("a value is matched whole, not as a substring",
            "kenyan" not in n(officer("NotKenyanAtAll")),
            "substring matching is the defect already fixed in stage 1")


# ── the admission rule, on real records ─────────────────────────────────
def check_rule(r: Results) -> None:
    print("\n-- the rule, against real Companies House records --")
    v = api_v2._kenyan_verdict

    ok, why = v(FC009164)
    r.check("KENYA AIRWAYS PLC is admitted (Kenyan, Canadian, Swedish)", ok, why)
    r.check("...and says how many", "3 of 5" in why, f"said {why!r}")

    ok, why = v(FC010541)
    r.check("KENYA FLAMINGO is admitted (11 Kenyan, 1 New Zealander)", ok, why)

    ok, why = v(SC461938)
    r.check("a dual national 'British,Kenyan' counts as Kenyan", ok, why)

    ok, why = v(ACME)
    r.check("a British-only company is refused", not ok, why)
    r.check("...and says why", "no Kenyan director" in why, f"said {why!r}")

    # THE RULE IS "at least one active director is Kenyan" AND NOTHING
    # ELSE. It used to also refuse any company where an active director's
    # nationality was unrecorded, which is a completeness rule, not the
    # admission rule: if one director is recorded Kenyan, the condition is
    # met, and what some other director did or did not file cannot unmeet
    # it. In practice that clause hid eligible companies - searching
    # "kimani" showed 8 candidates and admitted none.
    ok, why = v(KENYAN_PLUS_BLANK)
    r.check("a Kenyan director beside an unrecorded one is ADMITTED", ok,
            f"said {why!r} - an eligible company is being hidden because "
            f"somebody else's paperwork is incomplete")
    r.check("...and counts only what is known",
            "1 of 2" in why, f"said {why!r}")

    ok, why = v(KENYAN_PLUS_TWO_BLANK)
    r.check("still admitted with two unrecorded directors", ok, why)

    # Unchanged: no Kenyan director is still a refusal. NAIROBI has none -
    # British,Irish and one blank - so it stays out, and the reason should
    # now name the real cause rather than the paperwork.
    ok, why = v(NAIROBI)
    r.check("a company with no Kenyan director is still refused", not ok, why)
    r.check("...and the reason names the real cause",
            "no Kenyan director" in why, f"said {why!r}")

    print("\n-- the edges --")
    r.check("no officers at all is refused", not v([])[0])
    r.check("...and says so", "no active directors" in v([])[1])
    r.check("a resigned Kenyan director does not admit",
            not v([officer("Kenyan", resigned="2020-01-01"),
                   officer("British")])[0],
            "a director who has left cannot make a company eligible")
    r.check("a Kenyan SECRETARY does not admit",
            not v([officer("Kenyan", role="secretary"),
                   officer("British")])[0],
            "the rule is about directors")
    r.check("a company whose only officer is a resigned Kenyan is refused",
            not v([officer("Kenyan", resigned="2020-01-01")])[0])
    r.check("one Kenyan director alone is admitted", v([officer("Kenyan")])[0])
    r.check("role matching is case-insensitive",
            v([officer("Kenyan", role="DIRECTOR")])[0])
    # 'Kenyan' must not be matched inside another word.
    r.check("'South African' alone is refused",
            not v([officer("South African")])[0])
    # The verdict, not just the parser, must compare whole values. Matching
    # 'kenyan' as a substring is the defect already fixed in stage 1, where
    # 'ip' matched inside Description, Multiple and Equipment.
    for bogus in ("NotKenyan", "Kenyanese", "Un-Kenyan"):
        r.check(f"{bogus!r} does not admit a company",
                not v([officer(bogus)])[0],
                "a nationality must match whole, not as a substring")
    r.check("...while 'Kenyan' itself still does",
            v([officer("Kenyan")])[0])


# ── the fetch layer: cache, failure handling, budget ────────────────────
def check_fetch(r: Results) -> None:
    print("\n-- a lookup failure must refuse, never admit --")
    saved_key = os.environ.get("COMPANIES_HOUSE_API_KEY")
    try:
        os.environ.pop("COMPANIES_HOUSE_API_KEY", None)
        api_v2._nat_cache.clear()
        ok, why = api_v2._fetch_kenyan("00000001")
        r.check("no API key refuses rather than admits", not ok, why)
        r.check("...and names the variable",
                "COMPANIES_HOUSE_API_KEY" in why, f"said {why!r}")
        r.check("...and is NOT cached",
                "00000001" not in api_v2._nat_cache,
                "caching a configuration failure would keep refusing a "
                "company that is in fact eligible, long after the fix")
    finally:
        if saved_key is None:
            os.environ.pop("COMPANIES_HOUSE_API_KEY", None)
        else:
            os.environ["COMPANIES_HOUSE_API_KEY"] = saved_key

    print("\n-- a transport failure or an HTTP error must refuse, and not stick --")
    import requests as _rq

    class _Resp:
        def __init__(self, code): self.status_code = code; self.headers = {}
        def json(self): return {}

    saved_get = api_v2._ch_session.get
    saved_key3 = os.environ.get("COMPANIES_HOUSE_API_KEY")
    try:
        os.environ["COMPANIES_HOUSE_API_KEY"] = "test-key"

        # An exception out of requests must not be read as "eligible".
        api_v2._nat_cache.clear()
        api_v2._ch_session.get = lambda *a, **k: (_ for _ in ()).throw(
            requests.exceptions.ConnectTimeout("down"))
        ok, why = api_v2._fetch_kenyan("00000002")
        r.check("a transport failure refuses rather than admits", not ok, why)
        r.check("...and names the failure", "lookup failed" in why, f"said {why!r}")
        r.check("...and is NOT cached", "00000002" not in api_v2._nat_cache,
                "an outage would otherwise refuse this company for 24 hours")

        # A 429 says nothing about the company either.
        api_v2._nat_cache.clear()
        api_v2._ch_session.get = lambda *a, **k: _Resp(429)
        ok, why = api_v2._fetch_kenyan("00000003")
        r.check("HTTP 429 refuses", not ok, why)
        r.check("...and reports the status", "429" in why, f"said {why!r}")
        r.check("...and is NOT cached", "00000003" not in api_v2._nat_cache,
                "caching a rate-limit response would keep refusing an "
                "eligible company long after the window reset")

        # A 200 verdict IS worth caching.
        api_v2._nat_cache.clear()
        class _Ok(_Resp):
            def json(self): return {"items": [officer("Kenyan")]}
        api_v2._ch_session.get = lambda *a, **k: _Ok(200)
        ok, why = api_v2._fetch_kenyan("00000004")
        r.check("a real verdict is returned", ok, why)
        r.check("...and IS cached", "00000004" in api_v2._nat_cache,
                "without this the cache check below passes for a cache that "
                "is never populated")
    finally:
        if saved_key3 is None:
            os.environ.pop("COMPANIES_HOUSE_API_KEY", None)
        else:
            os.environ["COMPANIES_HOUSE_API_KEY"] = saved_key3
        api_v2._nat_cache.clear()

    print("\n-- the cache answers without a request --")
    api_v2._nat_cache.clear()
    api_v2._nat_cache["12345678"] = (time.time() + 3600, True, "cached yes")
    ok, why = api_v2._fetch_kenyan("12345678")
    r.check("a fresh cache entry is used", ok and why == "cached yes",
            f"got {(ok, why)!r}")
    api_v2._nat_cache["12345678"] = (time.time() - 1, True, "stale yes")
    saved_key2 = os.environ.get("COMPANIES_HOUSE_API_KEY")
    try:
        os.environ.pop("COMPANIES_HOUSE_API_KEY", None)
        ok, why = api_v2._fetch_kenyan("12345678")
        r.check("an EXPIRED entry is not used", why != "stale yes",
                f"got {why!r} - a stale verdict must be re-fetched")
    finally:
        if saved_key2 is not None:
            os.environ["COMPANIES_HOUSE_API_KEY"] = saved_key2
    api_v2._nat_cache.clear()


def check_search_pages_through_results(r: Results) -> None:
    """One page of 20 is not "the whole database".

    Searching "kimani" reported "25 companies match" and checked 8. Three
    separate things capped it: /search/companies was called once with
    items_per_page=20, so 5 of the 25 were never fetched at all; the active
    filter cut those 20 down; and the examine ceiling was 12.

    The search box is a type-ahead on a 120ms debounce, so the per-keystroke
    budget has to stay small - but a deliberate "check all of them" must be
    able to page through everything the query matches.
    """
    print("\n-- the search pages through Companies House --")
    calls = []

    def fake_ch_get(path, params=None):
        calls.append((path, dict(params or {})))
        start = int((params or {}).get("start_index", 0))
        per = int((params or {}).get("items_per_page", 20))
        # 25 active companies, numbered 00000001..00000025.
        items = [{"company_number": "%08d" % (i + 1),
                  "title": "KIMANI CO %d" % (i + 1),
                  "company_status": "active",
                  "date_of_creation": "2024-01-01"}
                 for i in range(start, min(start + per, 25))]
        return {"total_results": 25, "items": items}

    cands, total, scanned = api_v2._ch_search_candidates(
        "kimani", fake_ch_get, year_filter=None, seen=set(), want=25)

    r.check("every matching company was reached", len(cands) == 25,
            f"got {len(cands)} of 25 - companies past the first page are "
            f"invisible")
    r.check("the total is reported", total == 25, f"got {total}")
    r.check("...and it took more than one call", len(calls) > 1,
            f"calls={calls!r}")
    r.check("...and paged with start_index",
            any(c[1].get("start_index") for c in calls), f"calls={calls!r}")

    # It must stop as soon as it has what was asked for, not keep paging.
    calls.clear()
    cands, _total, _scanned = api_v2._ch_search_candidates(
        "kimani", fake_ch_get, year_filter=None, seen=set(), want=5)
    r.check("it stops once `want` candidates are collected", len(cands) == 5,
            f"got {len(cands)} - a type-ahead would spend the whole quota")
    r.check("...after a single page", len(calls) == 1, f"calls={calls!r}")

    # And it must not loop for ever when the results run out.
    calls.clear()

    def empty_ch_get(path, params=None):
        calls.append(path)
        return {"total_results": 0, "items": []}

    cands, total, _scanned = api_v2._ch_search_candidates(
        "nothing", empty_ch_get, year_filter=None, seen=set(), want=100)
    r.check("an empty result set stops immediately",
            cands == [] and len(calls) == 1, f"calls={len(calls)}")

    # Dissolved companies and ones already in the pipeline are skipped, but
    # skipping them must not end the paging early.
    def mixed_ch_get(path, params=None):
        start = int((params or {}).get("start_index", 0))
        per = int((params or {}).get("items_per_page", 20))
        out = []
        for i in range(start, min(start + per, 40)):
            out.append({"company_number": "%08d" % (i + 1),
                        "title": "CO %d" % (i + 1),
                        # every other one dissolved
                        "company_status": "active" if i % 2 == 0 else "dissolved",
                        "date_of_creation": "2024-01-01"})
        return {"total_results": 40, "items": out}

    cands, _t, scanned = api_v2._ch_search_candidates(
        "mixed", mixed_ch_get, year_filter=None, seen=set(), want=20)
    r.check("dissolved companies are skipped without ending the search",
            len(cands) == 20 and scanned == 40,
            f"got {len(cands)} candidates from {scanned} scanned - the "
            f"active filter must not be mistaken for the end of the results")


def check_filter(r: Results) -> None:
    print("\n-- the filter: what it keeps, hides and reports --")
    verdicts = {
        "00000001": (True,  "2 of 2 active directors Kenyan"),
        "00000002": (False, "no Kenyan director among 3 active"),
        "00000003": (False, "1 of 2 active directors have no nationality recorded"),
    }
    saved = api_v2._fetch_kenyan
    api_v2._fetch_kenyan = lambda cn: verdicts.get(cn, (False, "unknown"))
    saved_rem = api_v2._ch_remain[0]
    try:
        api_v2._ch_remain[0] = 600
        cands = [{"cn": c, "name": f"CO {c}"} for c in sorted(verdicts)]
        kept, excluded, note = api_v2._apply_kenyan_filter(cands)
        r.check("only the admissible candidate is kept",
                [k["cn"] for k in kept] == ["00000001"],
                f"kept {[k['cn'] for k in kept]}")
        r.check("the rest are counted as hidden", excluded == 2, f"got {excluded}")
        r.check("the note says how many were hidden",
                "2 hidden" in note, f"note={note!r}")
        r.check("every candidate carries its reason",
                all("kenyan_reason" in c for c in cands),
                "an operator must be able to see WHY one was hidden")

        # Nothing to do must not invent a note.
        kept, excluded, note = api_v2._apply_kenyan_filter([])
        r.check("no candidates gives no note",
                (kept, excluded, note) == ([], 0, ""), f"got {(kept, excluded, note)!r}")

        # The per-search ceiling, which is what stops one search eating the
        # whole 5-minute budget.
        many = [{"cn": "00000001", "name": "x"} for _ in range(30)]
        kept, excluded, note = api_v2._apply_kenyan_filter(many)
        r.check("no more than the ceiling is examined",
                len(kept) == api_v2._NAT_MAX_PER_SEARCH,
                f"examined {len(kept)}, ceiling {api_v2._NAT_MAX_PER_SEARCH}")
        r.check("...and the unchecked remainder is reported",
                "not checked" in note, f"note={note!r}")

        # A low quota must be visible, not silent. The value sits ABOVE the
        # reservation floor on purpose: below the floor nothing is spent at
        # all and the note is the reservation's, which is a different path
        # (check_quota_reservation covers that one).
        api_v2._ch_remain[0] = api_v2._NAT_MIN_REMAIN + 1
        kept, excluded, note = api_v2._apply_kenyan_filter(
            [{"cn": "00000001", "name": "x"}])
        r.check("a low Companies House quota is reported",
                "quota getting low" in note, f"note={note!r}")
        api_v2._ch_remain[0] = 600
        kept, excluded, note = api_v2._apply_kenyan_filter(
            [{"cn": "00000001", "name": "x"}])
        r.check("...and is not reported when the quota is healthy",
                "quota" not in note.lower(), f"note={note!r}")
    finally:
        api_v2._fetch_kenyan = saved
        api_v2._ch_remain[0] = saved_rem


def check_quota_reservation(r: Results) -> None:
    print("\n-- the quota floor is a reservation, not a warning --")
    # The 600-per-5-minutes window is shared with the pipeline stages. Below
    # the floor this must spend NOTHING, and it must return nothing rather
    # than returning candidates unfiltered - unfiltered would let an
    # operator add a company with no Kenyan director, which is the whole
    # thing the rule prevents.
    calls = []
    saved_fetch, saved_rem = api_v2._fetch_kenyan, api_v2._ch_remain[0]
    api_v2._fetch_kenyan = lambda cn: (calls.append(cn), (True, "yes"))[1]
    try:
        cands = [{"cn": "00000001", "name": "A"}, {"cn": "00000002", "name": "B"}]

        api_v2._ch_remain[0] = api_v2._NAT_MIN_REMAIN - 1
        kept, excluded, note = api_v2._apply_kenyan_filter(list(cands))
        r.check("below the floor, NOTHING is spent", calls == [],
                f"made {len(calls)} lookups - the floor must stop spending, "
                f"not merely warn")
        r.check("...and nothing is returned", kept == [], f"kept {kept}")
        r.check("...rather than returning them unfiltered",
                len(kept) != len(cands),
                "returning candidates unchecked would admit a company with "
                "no Kenyan director")
        r.check("...and the note says why", "quota" in note.lower(), f"note={note!r}")
        r.check("...and tells the operator what to do",
                "company number" in note, f"note={note!r}")

        # Exactly AT the floor must still spend: an off-by-one here silently
        # disables the filter for a whole window.
        calls.clear()
        api_v2._ch_remain[0] = api_v2._NAT_MIN_REMAIN
        kept, excluded, note = api_v2._apply_kenyan_filter(list(cands))
        r.check("at the floor exactly, it still runs", calls != [],
                "the reservation is `< floor`, so the floor itself spends")

        calls.clear()
        api_v2._ch_remain[0] = 600
        kept, excluded, note = api_v2._apply_kenyan_filter(list(cands))
        r.check("with a healthy quota it runs normally",
                len(calls) == 2 and len(kept) == 2, f"calls={calls} kept={kept}")
        r.check("...and says nothing about quota", "quota" not in note.lower(),
                f"note={note!r}")

        # Unknown remaining (nothing fetched yet) must not block the first
        # search of a process.
        calls.clear()
        api_v2._ch_remain[0] = None
        kept, _, _ = api_v2._apply_kenyan_filter(list(cands))
        r.check("an unknown quota does not block the first search",
                len(kept) == 2, f"kept {kept}")
    finally:
        api_v2._fetch_kenyan, api_v2._ch_remain[0] = saved_fetch, saved_rem


def check_pooled_session(r: Results) -> None:
    print("\n-- the lookup reuses one connection --")
    # Measured: 12 lookups took 2.26s through requests.get() (a new TCP+TLS
    # handshake each) and 0.49s through one pooled Session. The fix is
    # connection reuse; 12 workers was SLOWER than 4, so concurrency was
    # never the answer.
    s = api_v2._ch_session
    r.check("there is a module-level session", isinstance(s, requests.Session))
    ad = s.get_adapter("https://api.company-information.service.gov.uk/")
    r.check("...mounted for https", ad is not None)
    r.check("...with a connection pool",
            getattr(ad, "_pool_maxsize", 0) >= 4,
            f"pool_maxsize={getattr(ad, '_pool_maxsize', None)}")
    r.check("...and no retries, so a failure cannot park a request thread",
            getattr(ad.max_retries, "total", None) in (0, False),
            f"max_retries={ad.max_retries!r}")
    src = (REPO / "api_v2.py").read_text(encoding="utf-8")
    r.check("the lookup goes through the session, not requests.get",
            "_ch_session.get(" in src and "_rq.get(" not in src,
            "a per-call requests.get re-handshakes TLS every time")


def check_add_guard(r: Results) -> None:
    print("\n-- /company/add enforces the rule, not just the search --")
    # The search filter only hides ineligible companies from the dialog. This
    # is the route that writes the row, and _add_company_row is called
    # nowhere else, so this is the real enforcement point.
    from flask import Flask
    saved_fetch, saved_find = api_v2._fetch_kenyan, api_v2._find_row
    added: list[tuple] = []
    saved_add = api_v2._add_company_row
    api_v2._add_company_row = lambda cn, name, doc="": added.append((cn, name))
    api_v2._find_row = lambda cn: None
    app = Flask(__name__); app.register_blueprint(api_v2.api_v2)
    try:
        api_v2._fetch_kenyan = lambda cn: (False, "no Kenyan director among 2 active")
        with app.test_client() as c:
            res = c.post("/api/v2/company/add",
                         json={"cn": "08227886", "name": "ACME ADVISORY LTD"})
        r.check("an ineligible company is refused", res.status_code == 422,
                f"HTTP {res.status_code}")
        r.check("...and NO row is written", added == [], f"wrote {added}")
        body = res.get_json()
        r.check("...and the reason is returned",
                "no Kenyan director" in (body or {}).get("reason", ""),
                f"body={body!r}")
        r.check("...with the rule stated", "rule" in (body or {}), f"body={body!r}")

        api_v2._fetch_kenyan = lambda cn: (True, "3 of 5 active directors Kenyan")
        with app.test_client() as c:
            res = c.post("/api/v2/company/add",
                         json={"cn": "FC009164", "name": "KENYA AIRWAYS PLC"})
        r.check("an eligible company is added", res.status_code == 201,
                f"HTTP {res.status_code} body={res.get_json()!r}")
        r.check("...and the row IS written", added == [("FC009164", "KENYA AIRWAYS PLC")],
                f"wrote {added}")

        # A company already in the pipeline must not be re-checked or
        # refused: it is already there, and refusing would strand it.
        added.clear()
        api_v2._find_row = lambda cn: {"Company Name": "ALREADY HERE"}
        api_v2._fetch_kenyan = lambda cn: (False, "would refuse")
        with app.test_client() as c:
            res = c.post("/api/v2/company/add", json={"cn": "16940106", "name": "x"})
        r.check("a company already in the pipeline is not re-judged",
                res.status_code == 200 and res.get_json().get("status") == "exists",
                f"HTTP {res.status_code} body={res.get_json()!r}")
    finally:
        api_v2._fetch_kenyan = saved_fetch
        api_v2._find_row = saved_find
        api_v2._add_company_row = saved_add


def check_route(r: Results) -> None:
    print("\n-- the search route: local rows are never filtered --")
    # A company already in the pipeline must stay findable. Filtering those
    # would leave an operator unable to find their own companies, which is a
    # worse bug than showing one that no longer qualifies.
    saved_rows, saved_fetch = api_v2._all_rows, api_v2._fetch_kenyan
    api_v2._fetch_kenyan = lambda cn: (False, "would be refused")
    api_v2._all_rows = lambda: [{"Company Number": "16940106",
                                 "Company Name": "ACME ADVISORY CONSULTING",
                                 "Directors": "", "Date of Creation": "2024-01-01"}]
    try:
        from flask import Flask
        app = Flask(__name__)
        app.register_blueprint(api_v2.api_v2)
        with app.test_client() as c:
            body = c.get("/api/v2/company/search?q=acme").get_json()
        cns = [x["cn"] for x in body.get("results", [])]
        r.check("a company already in the pipeline is still returned",
                "16940106" in cns,
                f"got {cns} - the filter must not hide the operator's own rows")
        r.check("...and no filter note is attached to a local-only result",
                "kenyan_note" not in body, f"body keys {sorted(body)}")
    finally:
        api_v2._all_rows, api_v2._fetch_kenyan = saved_rows, saved_fetch


def check_breadth_reported(r: Results) -> None:
    print("\n-- how broad the search was must reach the operator --")
    # Without this an eligible company sitting past the examine ceiling is
    # simply invisible. total_results is a top-level key of the search
    # response (measured: 1 for a company number, 1014 for "kenya airways",
    # 10000 for "consulting"), so reporting it costs nothing.
    import run_pipeline
    from flask import Flask
    saved_rows, saved_fetch = api_v2._all_rows, api_v2._fetch_kenyan
    saved_get, saved_rem = run_pipeline.ch_get, api_v2._ch_remain[0]
    api_v2._all_rows = lambda: []
    api_v2._fetch_kenyan = lambda cn: (False, "no Kenyan director among 1 active")
    api_v2._ch_remain[0] = 600
    run_pipeline.ch_get = lambda path, params=None: {
        "total_results": 10000,
        "items": [{"company_number": f"{i:08d}", "title": f"CO {i}",
                   "company_status": "active", "date_of_creation": "2020-01-01"}
                  for i in range(20)]}
    app = Flask(__name__); app.register_blueprint(api_v2.api_v2)
    try:
        with app.test_client() as c:
            body = c.get("/api/v2/company/search?q=consulting").get_json()
        r.check("the total number of matches is reported",
                body.get("ch_total") == 10000, f"got {body.get('ch_total')!r}")
        adv = body.get("kenyan_advice", "")
        r.check("...with advice when more matched than were checked",
                bool(adv), f"body keys {sorted(body)}")
        r.check("...naming how many matched", "10000" in adv, f"advice={adv!r}")
        r.check("...and how many were actually checked",
                str(api_v2._NAT_MAX_PER_SEARCH) in adv, f"advice={adv!r}")
        r.check("...and what to do about it",
                "company number" in adv, f"advice={adv!r}")

        # One exact match must NOT nag about narrowing - that is the cheap,
        # precise path and telling the operator to narrow it is noise.
        run_pipeline.ch_get = lambda path, params=None: {
            "total_results": 1,
            "items": [{"company_number": "08227886", "title": "ACME ADVISORY LTD",
                       "company_status": "active", "date_of_creation": "2012-09-25"}]}
        with app.test_client() as c:
            body = c.get("/api/v2/company/search?q=08227886").get_json()
        r.check("an exact match reports no advice",
                "kenyan_advice" not in body, f"body keys {sorted(body)}")
        r.check("...but still reports the total", body.get("ch_total") == 1,
                f"got {body.get('ch_total')!r}")
    finally:
        api_v2._all_rows, api_v2._fetch_kenyan = saved_rows, saved_fetch
        run_pipeline.ch_get, api_v2._ch_remain[0] = saved_get, saved_rem


def main() -> int:
    r = Results()
    print(f"target: the Kenyan-director admission rule in {REPO / 'api_v2.py'}")
    check_parser(r)
    check_rule(r)
    check_fetch(r)
    check_search_pages_through_results(r)
    check_filter(r)
    check_quota_reservation(r)
    check_pooled_session(r)
    check_add_guard(r)
    check_route(r)
    check_breadth_reported(r)
    return r.report()


if __name__ == "__main__":
    raise SystemExit(main())
