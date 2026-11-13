#!/usr/bin/env python3
"""Tests for the stages recovering by themselves instead of asking a human.

    python3 test_auto_recovery.py

Hermetic: no network, no database, no registrar. Every provider boundary is
stubbed, and the stubs are the point - each one reproduces a specific way the
real provider refuses.

The rule this file encodes, and the reason it is not "retry everything":

  * BEFORE a billed order, automate freely. Availability checks cost nothing,
    so widening the search and paging an API is pure gain.
  * AFTER a billed order, automate almost nothing. Namecheap does not
    document that a Status="ERROR" response to domains.create means nothing
    was charged, and its own error list includes 3050900 "Unknown response
    from provider" and 5050900 "Unknown exceptions". An automatic re-order on
    those is how a pipeline charges twice for one domain.

So a retry after a refusal is allowed ONLY for an error number that means the
order could not have been created at all, and never for an unknown one.
"""
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

from logging_setup import configure_logging

configure_logging()

import run_pipeline


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


# ── fixtures: a paged Companies House filing history ───────────────────────

def _filing(date: str, category: str = "accounts", doc: str | None = None,
            ftype: str = "AA") -> dict:
    item = {"category": category, "description": category, "type": ftype,
            "date": date, "links": {}}
    if doc:
        item["links"]["document_metadata"] = doc
    return item


class FakeCH:
    """Serves filing history in pages, and records every request."""

    def __init__(self, pages: list[list[dict]], total: int | None = None):
        self.pages, self.total = pages, total
        self.requests: list[dict] = []

    def ch_get(self, path, params=None):
        params = params or {}
        self.requests.append({"path": path, **params})
        if "/filing-history" not in path:
            return {}
        start = int(params.get("start_index") or 0)
        per = int(params.get("items_per_page") or 100)
        flat = [i for page in self.pages for i in page]
        items = flat[start:start + per]
        out = {"items": items, "start_index": start, "items_per_page": per}
        if self.total is not None:
            out["total_count"] = self.total
        return out


class FakeDoc:
    status_code = 200

    def __init__(self):
        self.url = None

    def __call__(self, url, headers=None, stream=False):
        self.url = url
        return self

    @staticmethod
    def iter_content(chunk_size=8192):
        yield b"%PDF-1.4\n%%EOF\n"


def _run_download(ch: FakeCH, doc: FakeDoc):
    saved = (run_pipeline.ch_get, run_pipeline.ch_get_raw)
    try:
        run_pipeline.ch_get = ch.ch_get
        run_pipeline.ch_get_raw = doc
        with tempfile.TemporaryDirectory() as td:
            return run_pipeline.download_certificate("14123456", td)
    finally:
        run_pipeline.ch_get, run_pipeline.ch_get_raw = saved


# ── A. the certificate stage pages the filing history ──────────────────────

def check_incorporation_beyond_the_first_page(r: Results) -> None:
    """A company with more than one page of filings still gets its certificate.

    The call asked for one page of 100 with no start_index, so for a company
    whose incorporation sits further back the stage answered "No documents
    available" and an operator had to fetch the PDF by hand.
    """
    print("\ncertificate: the incorporation filing is on a later page")
    # 120 ordinary filings ahead of the incorporation, so the single
    # 100-item request the stage used to make cannot reach it. A long-lived
    # company really does have this many: annual accounts, confirmation
    # statements, officer changes, charges and mortgages all land here.
    pages = [
        [_filing("2024-01-01", doc=f"/document/acc{i}") for i in range(120)],
        [_filing("2021-03-01", category="incorporation", ftype="NEWINC",
                 doc="/document/INC")],
    ]
    ch, doc = FakeCH(pages, total=121), FakeDoc()
    path, err = _run_download(ch, doc)
    r.check("a certificate was downloaded", bool(path) and err is None,
            f"path={path!r} err={err!r}")
    r.check("it is the INCORPORATION document, not a stray accounts filing",
            "INC" in str(doc.url), f"fetched {doc.url!r}")
    r.check("more than one page was requested",
            len([q for q in ch.requests if "/filing-history" in q["path"]]) > 1,
            f"requests: {ch.requests!r}")


def check_first_page_hit_costs_one_request(r: Results) -> None:
    """Paging must not spend the shared Companies House quota needlessly.

    The budget is 600 requests per 5 minutes and the pipeline stages share it
    with the dashboard's admission checks, so a company whose incorporation is
    on page one must still cost exactly one request.
    """
    print("\ncertificate: an incorporation on page one still costs one request")
    pages = [[_filing("2021-03-01", category="incorporation", ftype="NEWINC",
                      doc="/document/INC"),
              _filing("2024-01-01", doc="/document/acc1")]]
    ch, doc = FakeCH(pages, total=2), FakeDoc()
    path, err = _run_download(ch, doc)
    n = len([q for q in ch.requests if "/filing-history" in q["path"]])
    r.check("the certificate was downloaded", bool(path), f"err={err!r}")
    r.check("exactly one filing-history request was made", n == 1,
            f"{n} requests: {ch.requests!r}")


def check_fallback_picks_the_oldest_by_date(r: Results) -> None:
    """With no incorporation filing at all, fall back by DATE, not by position.

    Companies House does not document the ordering of filing history, so
    taking the last item of the list as "the earliest filing" is an assumption
    about the provider that the provider does not make.
    """
    print("\ncertificate: the fallback uses the oldest date, not list position")
    pages = [[
        _filing("2019-05-05", doc="/document/MIDDLE"),
        _filing("2015-01-01", doc="/document/OLDEST"),
        _filing("2024-09-09", doc="/document/NEWEST"),
    ]]
    ch, doc = FakeCH(pages, total=3), FakeDoc()
    path, err = _run_download(ch, doc)
    r.check("something was downloaded", bool(path), f"err={err!r}")
    r.check("the oldest-dated filing with a document was chosen",
            "OLDEST" in str(doc.url), f"fetched {doc.url!r}")


def check_no_documents_at_all_still_reports(r: Results) -> None:
    print("\ncertificate: a company with no documents at all")
    ch, doc = FakeCH([[_filing("2024-01-01"), _filing("2023-01-01")]], total=2), FakeDoc()
    path, err = _run_download(ch, doc)
    r.check("no path is returned", path is None, f"path={path!r}")
    r.check("the reason is reported", bool(err), f"err={err!r}")


def check_paging_is_bounded(r: Results) -> None:
    """A provider that keeps answering must not loop forever."""
    print("\ncertificate: paging stops even if the API keeps answering")
    big = [[_filing(f"20{10 + (i // 300)}-01-01", doc=f"/document/d{i}")
            for i in range(i0, i0 + 100)] for i0 in range(0, 3000, 100)]
    ch, doc = FakeCH(big, total=None), FakeDoc()
    path, err = _run_download(ch, doc)
    n = len([q for q in ch.requests if "/filing-history" in q["path"]])
    r.check("it stopped", n <= 12, f"{n} filing-history requests")
    r.check("and still produced a document rather than giving up",
            bool(path), f"path={path!r} err={err!r}")


# ── B. the domain search tries enough names to find one ────────────────────

import namecheap_automation as nc


def _availability(available_names: set[str], premium: set[str] = frozenset()):
    """Stub namecheap.domains.check: only these names are available."""
    calls: list[list[str]] = []

    def check_domains(domain_list):
        calls.append(list(domain_list))
        return [{"domain": d,
                 "available": d in available_names,
                 "is_premium": d in premium,
                 "registration_price": "0", "icann_fee": "0"}
                for d in domain_list]

    return check_domains, calls


def _search(name: str, available: set[str], premium: set[str] = frozenset()):
    saved = nc.check_domains
    try:
        stub, calls = _availability(available, premium)
        nc.check_domains = stub
        return nc.find_cheapest_domain(name), calls
    finally:
        nc.check_domains = saved


def check_variants_when_the_plain_name_is_taken(r: Results) -> None:
    """The commonest reason the stage asks for a human is "all taken".

    It generated one name per cheap TLD - seven candidates - and an
    abbreviation only when the cleaned name was longer than 15 characters.
    For a company whose name is taken on all seven, the stage stopped and an
    operator had to pick a domain by hand. Availability checks are not billed,
    so there is no reason to stop at seven.
    """
    print("\ndomain: a variant is found when the plain name is taken everywhere")
    res, calls = _search("TAMU TRADERS LTD", {"tamu-traders-ltd.online"})
    r.check("a domain was found", "best" in res, f"result was {res!r}")
    if "best" in res:
        r.check("it is the available variant",
                res["best"]["domain"] == "tamu-traders-ltd.online",
                f"chose {res['best']['domain']!r}")
    r.check("the check was still a single batched call", len(calls) == 1,
            f"{len(calls)} calls of sizes {[len(c) for c in calls]}")


def check_candidate_budget(r: Results) -> None:
    """Namecheap takes 50 domains per domains.check call, so stay inside one."""
    print("\ndomain: the candidate list fits one API call")
    res, calls = _search("A VERY LONG COMPANY NAME OF MANY WORDS LIMITED", set())
    n = len(calls[0]) if calls else 0
    r.check("candidates were generated", n > 7, f"only {n} candidates")
    r.check("and there are at most 50 of them", n <= 50, f"{n} candidates")
    r.check("every candidate is distinct", n == len(set(calls[0])),
            f"{n - len(set(calls[0]))} duplicates")


def check_exact_name_still_wins(r: Results) -> None:
    print("\ndomain: the exact name still beats a variant at the same price")
    res, _ = _search("TAMU TRADERS LTD",
                     {"tamu-traders.online", "tamutraders.online",
                      "tamu-traders-ltd.online", "tt.online"})
    r.check("the exact cleaned name was chosen",
            res.get("best", {}).get("domain") == "tamu-traders.online",
            f"chose {res.get('best', {}).get('domain')!r}")


def check_choice_does_not_depend_on_response_order(r: Results) -> None:
    """Namecheap does not promise to answer in the order it was asked.

    Without an explicit tie-break the choice rode on the order the candidates
    happened to be generated in and on the sort being stable, which is a
    property of this code and of CPython, not of the provider.
    """
    print("\ndomain: the choice does not depend on the provider's ordering")
    saved = nc.check_domains
    try:
        stub, _ = _availability({"tamu-traders.online", "tamutraders.online",
                                 "tamu-traders-ltd.online", "tt.online"})
        nc.check_domains = lambda lst: list(reversed(stub(lst)))
        res = nc.find_cheapest_domain("TAMU TRADERS LTD")
    finally:
        nc.check_domains = saved
    r.check("the exact cleaned name is still chosen",
            res.get("best", {}).get("domain") == "tamu-traders.online",
            f"chose {res.get('best', {}).get('domain')!r} when the provider "
            f"answered in reverse")


def check_cheaper_tld_still_wins(r: Results) -> None:
    print("\ndomain: a cheaper TLD still beats a dearer one")
    # .online is first in CHEAP_TLDS, .com last.
    res, _ = _search("TAMU TRADERS LTD",
                     {"tamu-traders.com", "tamu-traders-ltd.online"})
    r.check("the cheaper TLD was chosen",
            str(res.get("best", {}).get("domain", "")).endswith(".online"),
            f"chose {res.get('best', {}).get('domain')!r}")


def check_no_oversized_labels(r: Results) -> None:
    """A DNS label cannot exceed 63 characters; such a candidate can only be
    refused, and refusals are what this whole exercise is about."""
    print("\ndomain: no candidate has an over-long label")
    long_name = "THE EXTRAORDINARILY LENGTHY AND UNNECESSARILY VERBOSE TRADING COMPANY OF GREAT BRITAIN LIMITED"
    res, calls = _search(long_name, set())
    bad = [d for d in (calls[0] if calls else []) if len(d.split(".")[0]) > 63]
    r.check("every candidate's label is <= 63 characters", not bad,
            f"over-long: {bad[:3]!r}")


def check_premium_still_excluded(r: Results) -> None:
    print("\ndomain: premium names are still never offered")
    res, _ = _search("TAMU TRADERS LTD",
                     {"tamu-traders.online", "tamu-traders-ltd.online"},
                     premium={"tamu-traders.online"})
    r.check("the premium name was not chosen",
            res.get("best", {}).get("domain") != "tamu-traders.online",
            f"chose {res.get('best', {}).get('domain')!r}")


def check_alternatives_are_offered(r: Results) -> None:
    print("\ndomain: alternatives are returned for a later attempt")
    res, _ = _search("TAMU TRADERS LTD",
                     {"tamu-traders.online", "tamutraders.online",
                      "tamu-traders.site"})
    r.check("alternatives are present", len(res.get("alternatives", [])) >= 2,
            f"alternatives: {res.get('alternatives')!r}")


def check_nothing_available_still_reports(r: Results) -> None:
    print("\ndomain: genuinely nothing available")
    res, _ = _search("TAMU TRADERS LTD", set())
    r.check("an error is returned", "error" in res, f"result was {res!r}")
    r.check("and it says what was checked", bool(res.get("checked")),
            f"result was {res!r}")


# ── C. when a human IS needed, give them the provider's error number ───────
#
# There is no automatic re-order after a refused one, deliberately. Namecheap
# does not document that a Status="ERROR" response to domains.create leaves
# nothing charged - its own list includes 3050900 "Unknown response from
# provider" and 5050900 "Unknown exceptions" - so re-ordering automatically
# would be the double charge the money path exists to prevent. What CAN be
# automated is giving the person who is called in something to look up.

NSU = "http://api.namecheap.com/xml.response"


class _FakeHTTP:
    def __init__(self, xml: str):
        self.xml, self.calls = xml, []

    def _resp(self, *a, **kw):
        self.calls.append((a, kw))
        outer = self

        class R:
            text = outer.xml

            @staticmethod
            def raise_for_status():
                return None
        return R()

    get = _resp
    post = _resp


def _with_http(xml: str, fn):
    saved = nc.requests
    try:
        nc.requests = _FakeHTTP(xml)
        return fn()
    finally:
        nc.requests = saved


_ERR_XML = (f'<?xml version="1.0" encoding="utf-8"?>'
            f'<ApiResponse Status="ERROR" xmlns="{NSU}"><Errors>'
            f'<Error Number="3050900">Unknown response from provider</Error>'
            f'</Errors></ApiResponse>')


def check_error_number_survives_parsing(r: Results) -> None:
    print("\nnamecheap: the provider's error NUMBER survives")
    resp = _with_http(_ERR_XML, lambda: nc._api_call("domains.create", {"X": "1"}))
    r.check("the call is reported as an error", resp["status"] == "ERROR",
            f"status={resp['status']!r}")
    r.check("the number is kept", "3050900" in str(resp.get("error_numbers")),
            f"error_numbers={resp.get('error_numbers')!r}")
    r.check("the message carries the number too",
            any("3050900" in e for e in resp["errors"]),
            f"errors={resp['errors']!r}")
    r.check("the error is reported once, not once per namespace guess",
            len(resp["errors"]) == 1, f"errors={resp['errors']!r}")


def check_register_domain_surfaces_the_number(r: Results) -> None:
    print("\nnamecheap: register_domain hands the number on")
    res = _with_http(_ERR_XML, lambda: nc.register_domain(
        "example.online", years=1,
        registrant_info={"FirstName": "A", "LastName": "B"}))
    r.check("an error is returned", "error" in res, f"{res!r}")
    r.check("the number reaches the caller", "3050900" in str(res.get("error")),
            f"error={res.get('error')!r}")
    r.check("and is available as data, not only prose",
            "3050900" in str(res.get("error_numbers")),
            f"error_numbers={res.get('error_numbers')!r}")


def check_operator_sees_the_number(r: Results) -> None:
    """The whole point: the person called in can look the code up."""
    print("\ndomain stage: the operator is given the code to look up")
    import db
    saved = (nc.check_domains, nc.get_existing_account_contact, nc.register_domain,
             db.claim_domain_purchase, db.update_row)
    try:
        nc.check_domains = lambda lst: [
            {"domain": lst[0], "available": True, "is_premium": False,
             "registration_price": "0", "icann_fee": "0"}]
        nc.get_existing_account_contact = lambda: {"source": "stub"}
        db.claim_domain_purchase = lambda cn: True
        db.update_row = lambda cn, updates: None
        nc.register_domain = lambda d, years=1, registrant_info=None: _with_http(
            _ERR_XML, lambda: saved[2](d, years=years, registrant_info=registrant_info))
        out = run_pipeline.register_company_domain(
            "TAMU TRADERS LTD", {"company_number": "14123456"})
        r.check("the stage reports an error", out.get("status") == "error",
                f"{out!r}")
        r.check("the provider's number is in the message shown to the operator",
                "3050900" in str(out.get("error")), f"error={out.get('error')!r}")
    finally:
        (nc.check_domains, nc.get_existing_account_contact, nc.register_domain,
         db.claim_domain_purchase, db.update_row) = saved


# ── D. a purchase claim resolves itself against the registrar ──────────────
#
# Keeping the claim on a refusal whose outcome is unknown is what stops a
# second charge. On its own it would also stop the pipeline until a person
# checked the Namecheap account by hand. So the thing that resolves a kept
# claim is the registrar itself: the pipeline asks what it owns before it
# ever asks for a person.
#
# The one answer that must never be guessed is "we do not own it". A listing
# that failed, or that may have been truncated, is UNKNOWN - because
# "not ours" is the answer that lets a second domain be bought.

import db


class FakeRow:
    """Just enough of db for the claim: one company, one domain_status."""

    def __init__(self, status: str = "", aged: bool = False):
        self.row = {"Domain Status": status, "Domain": "", "Domain Cost": ""}
        self.writes: list[dict] = []
        self.aged = aged                      # has the claim sat for 7 days?
        self.release_calls: list[int] = []

    def claim(self, cn):
        if self.row.get("Domain Status") == db.DOMAIN_CLAIM:
            return False                      # a claim is already outstanding
        self.row["Domain Status"] = db.DOMAIN_CLAIM
        return True

    def release_expired(self, cn, max_age_days):
        """Stands in for the single conditional UPDATE in db."""
        self.release_calls.append(max_age_days)
        if self.row.get("Domain Status") != db.DOMAIN_CLAIM or not self.aged:
            return False
        self.row["Domain Status"] = db.DOMAIN_CLAIM_EXPIRED
        return True

    def update(self, cn, updates):
        self.writes.append(dict(updates))
        self.row.update(updates)

    @property
    def claim_held(self):
        return self.row.get("Domain Status") == db.DOMAIN_CLAIM


def _domain_stage(row: FakeRow, listing, register=None, available=True, rows=None,
                  prices=None, balance=(None, None), icann_fee="0",
                  available_tlds=None):
    """Run the real register_company_domain against stubbed boundaries.

    `rows` is what the rest of the company table looks like - reconciliation
    reads it to be sure a domain is not already another company's.
    """
    saved = (nc.check_domains, nc.get_existing_account_contact, nc.register_domain,
             getattr(nc, "list_account_domains", None),
             db.claim_domain_purchase, db.update_row, db.all_rows,
             getattr(db, "release_expired_claim", None),
             getattr(nc, "one_year_price", None),
             getattr(nc, "available_balance", None))
    ordered: list[str] = []
    try:
        db.all_rows = lambda use_cache=True: (
            rows if rows is not None
            else [{"Company Number": "14123456", "Domain": ""}])
        nc.check_domains = lambda lst: [
            {"domain": d, "available": available, "is_premium": False,
             "registration_price": "0", "icann_fee": icann_fee} for d in lst
            if available_tlds is None
            or any(d.endswith(t) for t in available_tlds)]
        nc.get_existing_account_contact = lambda: {"source": "stub"}
        nc.one_year_price = lambda tld: (prices or {}).get(tld, (None, None))
        nc.available_balance = lambda: balance

        def _register(d, years=1, registrant_info=None):
            ordered.append(d)
            return (register or (lambda dom: {"registered": True, "charged": "8.88",
                                              "domain_id": "d1", "order_id": "o1"}))(d)

        nc.register_domain = _register
        nc.list_account_domains = lambda: listing
        db.claim_domain_purchase = row.claim
        db.release_expired_claim = row.release_expired
        db.update_row = row.update
        out = run_pipeline.register_company_domain(
            "TAMU TRADERS LTD", {"company_number": "14123456"})
        return out, ordered
    finally:
        (nc.check_domains, nc.get_existing_account_contact, nc.register_domain,
         _, db.claim_domain_purchase, db.update_row, db.all_rows, _rel,
         _price, _bal) = saved
        if saved[3] is not None:
            nc.list_account_domains = saved[3]
        if saved[7] is not None:
            db.release_expired_claim = saved[7]
        if saved[8] is not None:
            nc.one_year_price = saved[8]
        if saved[9] is not None:
            nc.available_balance = saved[9]


_OWNED = {"domains": [{"name": "tamu-traders.online"}], "complete": True}
_EMPTY = {"domains": [], "complete": True}
_TRUNCATED = {"domains": [{"name": "somebody-else.online"}], "complete": False}
_BROKEN = {"error": "Failed: ['5019169: Unknown exceptions while retrieving Domain list']"}


def check_outstanding_claim_resolves_itself(r: Results) -> None:
    print("\nclaim: an outstanding claim whose domain DID land resolves itself")
    row = FakeRow(status=db.DOMAIN_CLAIM)
    out, ordered = _domain_stage(row, _OWNED)
    r.check("the stage succeeds without a person", out.get("status") == "registered",
            f"{out!r}")
    r.check("it is the domain the account actually holds",
            out.get("domain") == "tamu-traders.online", f"{out!r}")
    r.check("NO second order was issued", ordered == [],
            f"orders issued: {ordered!r}")
    r.check("the claim is resolved", not row.claim_held,
            f"Domain Status is {row.row.get('Domain Status')!r}")


def check_outstanding_claim_without_a_domain_stops(r: Results) -> None:
    print("\nclaim: an outstanding claim with nothing in the account still stops")
    row = FakeRow(status=db.DOMAIN_CLAIM)
    out, ordered = _domain_stage(row, _EMPTY)
    r.check("the stage does not succeed", out.get("status") != "registered", f"{out!r}")
    r.check("NO order was issued", ordered == [], f"orders issued: {ordered!r}")
    r.check("the claim is still held", row.claim_held,
            f"Domain Status is {row.row.get('Domain Status')!r}")
    r.check("the operator is told the account was checked",
            "account" in str(out.get("error", "")).lower(), f"{out!r}")


def check_truncated_listing_is_not_proof(r: Results) -> None:
    print("\nclaim: a truncated listing is UNKNOWN, never 'not ours'")
    row = FakeRow(status=db.DOMAIN_CLAIM)
    out, ordered = _domain_stage(row, _TRUNCATED)
    r.check("no order was issued", ordered == [], f"orders issued: {ordered!r}")
    r.check("the claim is still held", row.claim_held, f"{row.row!r}")
    # Asserted against the RECONCILIATION verdict, not the wrapper around it:
    # "could not be resolved automatically" appears either way, so matching it
    # proved nothing. Only the unknown path says the listing was incomplete,
    # and only the not_found path claims the account was read whole.
    err = str(out.get("error", "")).lower()
    r.check("the answer says the listing was incomplete",
            "incomplete" in err, f"{out!r}")
    r.check("...and does NOT claim the account was read whole",
            "whole namecheap account was read" not in err, f"{out!r}")


def check_failed_listing_is_not_proof(r: Results) -> None:
    print("\nclaim: a failed listing is UNKNOWN too")
    row = FakeRow(status=db.DOMAIN_CLAIM)
    out, ordered = _domain_stage(row, _BROKEN)
    r.check("no order was issued", ordered == [], f"orders issued: {ordered!r}")
    r.check("the claim is still held", row.claim_held, f"{row.row!r}")
    r.check("the provider's failure is passed on",
            "5019169" in str(out.get("error", "")), f"{out!r}")


def check_refusal_keeps_the_claim(r: Results) -> None:
    """AUTHORIZED CHANGE. A refused order used to release the claim, which let
    the next run buy again on the strength of 'Namecheap said no, so nothing
    was charged' - which Namecheap does not say."""
    print("\nrefusal: the claim is KEPT, not released")
    row = FakeRow()
    out, ordered = _domain_stage(
        row, _EMPTY,
        register=lambda d: {"error": "Registration failed: "
                                     "['3050900: Unknown response from provider']",
                            "error_numbers": ["3050900"]})
    r.check("one order was attempted", len(ordered) == 1, f"{ordered!r}")
    r.check("the stage reports an error", out.get("status") == "error", f"{out!r}")
    r.check("the claim is KEPT", row.claim_held,
            f"Domain Status is {row.row.get('Domain Status')!r}; releasing it is "
            f"what let a second order be issued")
    r.check("Domain Status was not overwritten with 'error'",
            all(w.get("Domain Status") != "error" for w in row.writes),
            f"writes: {row.writes!r}")


def check_unresolved_order_recovers_when_the_domain_appears(r: Results) -> None:
    """A NonRealTimeDomain registration completes asynchronously: Registered
    came back false, and the domain shows up in the account shortly after."""
    print("\nunresolved: the domain appears in the account, so it resolves itself")
    row = FakeRow()
    out, ordered = _domain_stage(
        row, _OWNED,
        register=lambda d: {"registered": False, "charged": "8.88", "order_id": "o9"})
    r.check("the stage ends up registered", out.get("status") == "registered", f"{out!r}")
    r.check("on the domain the account holds",
            out.get("domain") == "tamu-traders.online", f"{out!r}")
    r.check("exactly one order was issued", len(ordered) == 1, f"{ordered!r}")
    r.check("the claim is resolved", not row.claim_held, f"{row.row!r}")


def check_unresolved_without_the_domain_still_stops(r: Results) -> None:
    print("\nunresolved: still nothing in the account, so it still stops")
    row = FakeRow()
    out, ordered = _domain_stage(
        row, _EMPTY,
        register=lambda d: {"registered": False, "charged": "8.88", "order_id": "o9"})
    r.check("the stage reports unresolved", out.get("status") == "unresolved", f"{out!r}")
    r.check("the claim is kept", row.claim_held, f"{row.row!r}")
    r.check("the amount is still recorded",
            any(w.get("Domain Cost") for w in row.writes), f"{row.writes!r}")


def check_another_companys_domain_is_never_claimed(r: Results) -> None:
    """Two companies with similar names generate overlapping candidates.

    Found by the end-to-end run, not by reasoning: a refused order for one
    company was "resolved" with the domain the previous company had bought
    seconds earlier, because both generate the same candidate labels. That
    would record one company's domain against another, on the money path.
    """
    print("\nclaim: a domain another company already holds is never taken")
    row = FakeRow(status=db.DOMAIN_CLAIM)
    if True:
        out, ordered = _domain_stage(row, _OWNED, rows=[
            {"Company Number": "99999999", "Domain": "tamu-traders.online"},
            {"Company Number": "14123456", "Domain": ""},
        ])
        r.check("the other company's domain is not claimed",
                out.get("domain") != "tamu-traders.online", f"{out!r}")
        r.check("the stage does not report success",
                out.get("status") != "registered", f"{out!r}")
        r.check("no order was issued either", ordered == [], f"{ordered!r}")
        r.check("the claim is still held", row.claim_held, f"{row.row!r}")


def check_ambiguous_account_is_not_guessed(r: Results) -> None:
    print("\nclaim: two possible domains and no way to tell is UNKNOWN")
    row = FakeRow(status=db.DOMAIN_CLAIM)
    if True:
        listing = {"domains": [{"name": "tamu-traders.online"},
                               {"name": "tamutraders.online"}], "complete": True}
        out, ordered = _domain_stage(row, listing)
        r.check("nothing is guessed", out.get("status") != "registered", f"{out!r}")
        r.check("both candidates are named for the operator",
                "tamu-traders.online" in str(out.get("error"))
                and "tamutraders.online" in str(out.get("error")), f"{out!r}")
        r.check("the claim is still held", row.claim_held, f"{row.row!r}")


class _PagedHTTP:
    """A provider that serves the account listing in pages."""

    def __init__(self, names: list[str], honour_page: bool = True,
                 error: bool = False):
        self.names, self.honour_page, self.error = names, honour_page, error
        self.pages_asked: list[str] = []

    def _resp(self, url, params=None, data=None, timeout=None, **kw):
        p = params or data or {}
        self.pages_asked.append(p.get("Page", "?"))
        if self.error:
            body = (f'<ApiResponse Status="ERROR" xmlns="{NSU}"><Errors>'
                    f'<Error Number="5019169">Unknown exceptions while '
                    f'retrieving Domain list</Error></Errors></ApiResponse>')
        else:
            page = int(p.get("Page", "1")) if self.honour_page else 1
            chunk = self.names[(page - 1) * 100: page * 100]
            nodes = "".join(f'<Domain ID="{i}" Name="{n}" />'
                            for i, n in enumerate(chunk, 1))
            body = (f'<ApiResponse Status="OK" xmlns="{NSU}"><CommandResponse>'
                    f'<DomainGetListResult>{nodes}</DomainGetListResult>'
                    f'</CommandResponse></ApiResponse>')

        class R:
            text = body
            status_code = 200

            @staticmethod
            def raise_for_status():
                return None
        return R()

    get = _resp
    post = _resp


def _list_with(http):
    saved = nc.requests
    try:
        nc.requests = http
        return nc.list_account_domains()
    finally:
        nc.requests = saved


# ── E. a claim that nothing can resolve expires after a set time ───────────
#
# Policy, set by the operator of this pipeline, not derived from anything:
# SEVEN DAYS. A claim is released only when ALL of these hold:
#   - the WHOLE account was read and holds no domain this company could have
#     bought (not_found - never "unknown"),
#   - no other company holds one of those domains,
#   - and the row has not been touched for seven days, so the claim is at
#     least that old.
# A NonRealTimeDomain registration completes in hours, so a week of silence
# with nothing in the account is as close to "no purchase exists" as this API
# can get.

def check_young_claim_is_not_released(r: Results) -> None:
    print("\nexpiry: a claim younger than the limit is NOT released")
    row = FakeRow(status=db.DOMAIN_CLAIM, aged=False)
    out, ordered = _domain_stage(row, _EMPTY)
    r.check("release was attempted", row.release_calls == [7],
            f"release calls: {row.release_calls!r}")
    r.check("...and refused, so the claim is still held", row.claim_held,
            f"Domain Status is {row.row.get('Domain Status')!r}")
    r.check("NO order was issued", ordered == [], f"{ordered!r}")
    r.check("the stage does not succeed", out.get("status") != "registered", f"{out!r}")


def check_expired_claim_is_released_and_buys(r: Results) -> None:
    print("\nexpiry: a seven-day-old claim with nothing in the account releases")
    row = FakeRow(status=db.DOMAIN_CLAIM, aged=True)
    out, ordered = _domain_stage(row, _EMPTY)
    r.check("the seven-day limit was the one applied", row.release_calls == [7],
            f"release calls: {row.release_calls!r}")
    r.check("the stage completes by itself", out.get("status") == "registered",
            f"{out!r}")
    r.check("exactly ONE order was issued", len(ordered) == 1, f"{ordered!r}")
    r.check("the row records the new purchase",
            any(w.get("Domain Status") == "registered" for w in row.writes),
            f"{row.writes!r}")


def check_expiry_never_fires_on_unknown(r: Results) -> None:
    """The whole safety argument: 'unknown' is not 'nothing was bought'."""
    print("\nexpiry: never fires when the account could not be read")
    for label, listing in (("a truncated listing", _TRUNCATED),
                           ("a failed listing", _BROKEN)):
        row = FakeRow(status=db.DOMAIN_CLAIM, aged=True)
        out, ordered = _domain_stage(row, listing)
        r.check(f"{label}: release is not even attempted", row.release_calls == [],
                f"release calls: {row.release_calls!r}")
        r.check(f"{label}: the claim is still held", row.claim_held, f"{row.row!r}")
        r.check(f"{label}: no order was issued", ordered == [], f"{ordered!r}")


def check_expiry_never_fires_when_recovered(r: Results) -> None:
    print("\nexpiry: never fires when the domain was found in the account")
    row = FakeRow(status=db.DOMAIN_CLAIM, aged=True)
    out, ordered = _domain_stage(row, _OWNED)
    r.check("release is not attempted", row.release_calls == [],
            f"release calls: {row.release_calls!r}")
    r.check("the existing domain is used", out.get("domain") == "tamu-traders.online",
            f"{out!r}")
    r.check("and nothing is bought", ordered == [], f"{ordered!r}")


# ── F. what it costs, what we can spend, and a refusal that says no order ──

def _search_priced(name, available_names, prices):
    saved = (nc.check_domains, getattr(nc, "one_year_price", None))
    try:
        stub, _ = _availability(available_names)
        nc.check_domains = stub
        nc.one_year_price = lambda tld: prices.get(tld, (None, None))
        return nc.find_cheapest_domain(name)
    finally:
        nc.check_domains = saved[0]
        if saved[1] is not None:
            nc.one_year_price = saved[1]


def check_real_prices_decide(r: Results) -> None:
    """"Cheapest" came from a COMMENT, not from Namecheap.

    CHEAP_TLDS is ordered by prices written in a comment above it, and the
    sort key was position in that list. Nothing in the pipeline ever asked
    what a domain costs - which is how an account with $2.20 in it issued an
    order it could not pay for.
    """
    print("\nprice: the cheapest TLD is the one that is actually cheapest")
    res = _search_priced(
        "TAMU TRADERS LTD",
        {"tamu-traders.online", "tamu-traders.site", "tamu-traders.xyz"},
        {".online": (5.00, "USD"), ".site": (1.00, "USD"), ".xyz": (2.00, "USD")})
    r.check("the genuinely cheapest name is chosen",
            res.get("best", {}).get("domain") == "tamu-traders.site",
            f"chose {res.get('best', {}).get('domain')!r} - .online is first in "
            f"CHEAP_TLDS but costs five times as much here")
    r.check("its price comes back with it", res.get("price") == 1.00,
            f"price={res.get('price')!r}")
    r.check("...and the currency", res.get("currency") == "USD",
            f"currency={res.get('currency')!r}")


def check_account_rate_beats_list_price(r: Results) -> None:
    """YourPrice is what THIS account pays; Price and RegularPrice are list.

    Taking the list price would both pick the wrong "cheapest" and, now that
    the balance is checked against it, refuse to buy things the account can
    in fact afford.
    """
    print("\nprice: this account's rate is the one used")
    saved = nc.get_pricing
    try:
        nc._price_cache.clear()
        nc.get_pricing = lambda tld="com": {"tld": tld, "prices": [
            {"duration": "2", "duration_type": "YEAR", "your_price": "0.50",
             "price": "0.50", "regular_price": "0.50", "currency": "USD"},
            {"duration": "1", "duration_type": "YEAR", "your_price": "1.11",
             "price": "7.77", "regular_price": "9.99", "currency": "USD"},
        ]}
        price, currency = nc.one_year_price(".online")
        r.check("the one-year REGISTER rate is taken", price == 1.11,
                f"got {price!r} - 7.77 is the list price, 9.99 the regular "
                f"price, and 0.50 is a two-year rate")
        r.check("with its currency", currency == "USD", f"got {currency!r}")
    finally:
        nc.get_pricing = saved
        nc._price_cache.clear()


def _search_with_fees(name, available_names, prices, fees):
    """Availability results carrying a per-name ICANN fee, as domains.check does."""
    saved = (nc.check_domains, getattr(nc, "one_year_price", None))
    try:
        nc.check_domains = lambda lst: [
            {"domain": d, "available": d in available_names, "is_premium": False,
             "registration_price": "0",
             "icann_fee": fees.get("." + d.split(".", 1)[1], "0")} for d in lst]
        nc.one_year_price = lambda tld: prices.get(tld, (None, None))
        return nc.find_cheapest_domain(name)
    finally:
        nc.check_domains = saved[0]
        if saved[1] is not None:
            nc.one_year_price = saved[1]


def check_what_a_tld_actually_billed_is_used(r: Results) -> None:
    """The quote is not the bill.

    Measured in production: users.getPricing quoted USD 1.78 for .site,
    domains.check reported IcannFee 0.0, and Namecheap billed 1.98. The
    balance was therefore being compared against a number 20 cents short.

    Every past purchase is already recorded - Domain Cost holds what was
    actually charged - so what a TLD really bills is knowable from the
    pipeline's own records, with no new state and no guess about a response
    shape the provider does not document.
    """
    print("\nbalance: what the TLD actually billed last time is what is used")
    row = FakeRow()
    history = [{"Company Number": "99999999", "Domain": "someone.site",
                "Domain Cost": "1.9800"},
               {"Company Number": "14123456", "Domain": "", "Domain Cost": ""}]
    out, ordered = _domain_stage(row, _EMPTY, rows=history,
                                 prices={".site": (1.78, "USD")},
                                 balance=(1.85, "USD"), available_tlds=[".site"])
    r.check("NO order was issued", ordered == [],
            f"{ordered!r} - 1.78 quoted is affordable at 1.85, but .site last "
            f"billed 1.98 and that is what will be charged")
    r.check("NO claim was taken", not row.claim_held, f"{row.row!r}")
    r.check("the operator is shown the figure that was used",
            "1.98" in str(out.get("error", "")), f"{out.get('error')!r}")
    r.check("...and told where it came from",
            "billed" in str(out.get("error", "")).lower(), f"{out.get('error')!r}")


def check_observed_charge_never_lowers_the_estimate(r: Results) -> None:
    print("\nbalance: a cheaper past charge never lowers the estimate")
    row = FakeRow()
    history = [{"Company Number": "99999999", "Domain": "someone.site",
                "Domain Cost": "0.01"}]
    out, ordered = _domain_stage(row, _EMPTY, rows=history,
                                 prices={".site": (1.78, "USD")},
                                 balance=(1.00, "USD"), available_tlds=[".site"])
    r.check("the quote still refuses it", ordered == [],
            f"{ordered!r} - a stale cheap charge must not let an order through")


def check_absurd_past_charge_is_ignored(r: Results) -> None:
    print("\nbalance: a wildly out-of-band past charge is not trusted")
    row = FakeRow()
    history = [{"Company Number": "99999999", "Domain": "someone.site",
                "Domain Cost": "900.00"}]
    out, ordered = _domain_stage(row, _EMPTY, rows=history,
                                 prices={".site": (1.78, "USD")},
                                 balance=(2.22, "USD"), available_tlds=[".site"])
    r.check("the order still goes ahead", len(ordered) == 1,
            f"{ordered!r} - 900.00 against a 1.78 quote is not a price, it is "
            f"bad data, and must not block the pipeline")


def check_no_history_uses_the_quote(r: Results) -> None:
    print("\nbalance: with no past purchase the quote is used, as before")
    row = FakeRow()
    out, ordered = _domain_stage(row, _EMPTY, rows=[],
                                 prices={".site": (1.78, "USD")},
                                 balance=(2.22, "USD"), available_tlds=[".site"])
    r.check("the order goes ahead", len(ordered) == 1, f"{ordered!r}")


def check_icann_fee_is_part_of_the_price(r: Results) -> None:
    """YourPrice does not include the ICANN fee for a gTLD; the bill does.

    domains.check already quotes IcannFee per name and the pipeline parsed it
    and threw it away, so the balance was compared against a number smaller
    than the charge.
    """
    print("\nprice: the ICANN fee counts")
    res = _search_with_fees("TAMU TRADERS LTD", {"tamu-traders.site"},
                            {".site": (1.78, "USD")}, {".site": "0.18"})
    r.check("the price is the whole charge", res.get("price") == 1.96,
            f"price={res.get('price')!r} - 1.78 + 0.18")
    r.check("the base price is still visible", res.get("base_price") == 1.78,
            f"base_price={res.get('base_price')!r}")
    r.check("and so is the fee", res.get("icann_fee") == 0.18,
            f"icann_fee={res.get('icann_fee')!r}")


def check_fee_can_change_the_choice(r: Results) -> None:
    print("\nprice: a big fee beats a small base price")
    res = _search_with_fees(
        "TAMU TRADERS LTD", {"tamu-traders.site", "tamu-traders.xyz"},
        {".site": (1.90, "USD"), ".xyz": (2.00, "USD")},
        {".site": "0.30", ".xyz": "0.00"})
    r.check("the cheaper TOTAL wins",
            res.get("best", {}).get("domain") == "tamu-traders.xyz",
            f"chose {res.get('best', {}).get('domain')!r} - .site is 1.90+0.30=2.20, "
            f".xyz is 2.00+0.00")


def check_no_fee_quoted_is_unchanged(r: Results) -> None:
    print("\nprice: no fee quoted means the total is the price")
    res = _search_with_fees("TAMU TRADERS LTD", {"tamu-traders.site"},
                            {".site": (1.78, "USD")}, {})
    r.check("the total is the base price", res.get("price") == 1.78,
            f"price={res.get('price')!r}")


def check_balance_check_includes_the_fee(r: Results) -> None:
    """The case this exists for: affordable on the base price, not on the bill."""
    print("\nbalance: the fee is what tips it over")
    row = FakeRow()
    out, ordered = _domain_stage(row, _EMPTY,
                                 prices={".online": (1.78, "USD")},
                                 balance=(1.90, "USD"), icann_fee="0.18")
    r.check("NO order was issued", ordered == [],
            f"{ordered!r} - 1.78 alone is affordable, 1.78 + 0.18 is not")
    r.check("NO claim was taken", not row.claim_held, f"{row.row!r}")
    r.check("the operator is shown the whole charge",
            "1.96" in str(out.get("error", "")), f"{out.get('error')!r}")


def check_price_fallback_is_the_old_order(r: Results) -> None:
    print("\nprice: with no prices available, the old TLD order still decides")
    res = _search_priced("TAMU TRADERS LTD",
                         {"tamu-traders.online", "tamu-traders.com"}, {})
    r.check("the hardcoded order is the fallback",
            str(res.get("best", {}).get("domain", "")).endswith(".online"),
            f"chose {res.get('best', {}).get('domain')!r}")
    r.check("and no price is claimed", res.get("price") is None,
            f"price={res.get('price')!r}")


def check_unpriced_tld_never_outranks_a_priced_one(r: Results) -> None:
    print("\nprice: a TLD whose price is unknown does not jump the queue")
    res = _search_priced("TAMU TRADERS LTD",
                         {"tamu-traders.online", "tamu-traders.site"},
                         {".site": (1.00, "USD")})       # .online unknown
    r.check("the known-priced name wins",
            res.get("best", {}).get("domain") == "tamu-traders.site",
            f"chose {res.get('best', {}).get('domain')!r}")


def check_insufficient_balance_never_orders(r: Results) -> None:
    """The point of the whole change: find out BEFORE taking a claim."""
    print("\nbalance: too little money means no claim and no order")
    row = FakeRow()
    out, ordered = _domain_stage(row, _EMPTY,
                                 prices={".online": (5.00, "USD")},
                                 balance=(2.20, "USD"))
    r.check("NO order was issued", ordered == [], f"{ordered!r}")
    r.check("NO claim was taken", not row.claim_held,
            f"Domain Status is {row.row.get('Domain Status')!r}; a claim taken "
            f"here is one a person has to clear afterwards")
    r.check("the stage reports an error", out.get("status") == "error", f"{out!r}")
    r.check("the operator is told both numbers",
            "2.2" in str(out.get("error")) and "5.0" in str(out.get("error")),
            f"{out.get('error')!r}")


def check_sufficient_balance_proceeds(r: Results) -> None:
    print("\nbalance: enough money and it carries on")
    row = FakeRow()
    out, ordered = _domain_stage(row, _EMPTY,
                                 prices={".online": (1.00, "USD")},
                                 balance=(2.20, "USD"))
    r.check("the order goes ahead", len(ordered) == 1, f"{ordered!r}")
    r.check("and succeeds", out.get("status") == "registered", f"{out!r}")


def check_unknown_balance_does_not_block(r: Results) -> None:
    """A balance the API will not give us must not stop the pipeline: the
    order itself is the backstop, and its refusal is handled."""
    print("\nbalance: an unreadable balance does not block the purchase")
    row = FakeRow()
    out, ordered = _domain_stage(row, _EMPTY,
                                 prices={".online": (1.00, "USD")},
                                 balance=(None, None))
    r.check("the order still goes ahead", len(ordered) == 1, f"{ordered!r}")


def check_currency_mismatch_does_not_block(r: Results) -> None:
    print("\nbalance: a currency we cannot compare is treated as unknown")
    row = FakeRow()
    out, ordered = _domain_stage(row, _EMPTY,
                                 prices={".online": (5.00, "USD")},
                                 balance=(2.20, "EUR"))
    r.check("the order goes ahead rather than being refused on a bad comparison",
            len(ordered) == 1, f"{ordered!r}")


_NO_ORDER_ERR = ("Registration failed: ['2528166: ORDER CREATION FAILED; "
                 "INSUFFICIENTFUNDS;Insufficient funds in account']")


def check_order_creation_failed_releases_the_claim(r: Results) -> None:
    """AUTHORIZED. Namecheap says in words that no order was created, and
    reconciliation agrees the account holds nothing. Two independent things
    have to say so before the claim goes."""
    print("\nrefusal: ORDER CREATION FAILED releases the claim")
    row = FakeRow()
    out, ordered = _domain_stage(
        row, _EMPTY, register=lambda d: {"error": _NO_ORDER_ERR,
                                         "error_numbers": ["2528166"]})
    r.check("one order was attempted", len(ordered) == 1, f"{ordered!r}")
    r.check("the claim is RELEASED", not row.claim_held,
            f"Domain Status is {row.row.get('Domain Status')!r}")
    r.check("the operator is told it was released",
            "released" in str(out.get("error", "")).lower(), f"{out!r}")


def check_order_creation_failed_keeps_claim_when_unknown(r: Results) -> None:
    """Both conditions, not either. If the account could not be read, the
    provider's wording is not enough on its own."""
    print("\nrefusal: ...but not when the account could not be read")
    row = FakeRow()
    out, ordered = _domain_stage(
        row, _TRUNCATED, register=lambda d: {"error": _NO_ORDER_ERR,
                                             "error_numbers": ["2528166"]})
    r.check("the claim is KEPT", row.claim_held,
            f"Domain Status is {row.row.get('Domain Status')!r}")


def check_other_refusals_still_keep_the_claim(r: Results) -> None:
    print("\nrefusal: an unknown-outcome code still keeps the claim")
    row = FakeRow()
    out, ordered = _domain_stage(
        row, _EMPTY,
        register=lambda d: {"error": "Registration failed: "
                                     "['3050900: Unknown response from provider']",
                            "error_numbers": ["3050900"]})
    r.check("the claim is KEPT", row.claim_held,
            f"Domain Status is {row.row.get('Domain Status')!r}")


def check_account_listing_completeness(r: Results) -> None:
    """`complete` is what the whole safety argument rests on.

    "This domain is not in the account" is the answer that lets the pipeline
    buy one, so it may only be given when the account was actually read whole.
    """
    print("\naccount listing: complete means complete")

    short = _list_with(_PagedHTTP([f"d{i}.online" for i in range(5)]))
    r.check("a short account reads complete", short.get("complete") is True,
            f"{short!r}")
    r.check("...and holds every domain", len(short.get("domains", [])) == 5,
            f"{short!r}")

    http = _PagedHTTP([f"d{i}.online" for i in range(250)])
    many = _list_with(http)
    r.check("a long account is paged to the end", many.get("complete") is True,
            f"complete={many.get('complete')} after pages {http.pages_asked}")
    r.check("...and holds all 250", len(many.get("domains", [])) == 250,
            f"{len(many.get('domains', []))} domains")

    ignored = _list_with(_PagedHTTP([f"d{i}.online" for i in range(250)],
                                    honour_page=False))
    r.check("a provider that ignores paging reads INCOMPLETE",
            ignored.get("complete") is False,
            f"complete={ignored.get('complete')!r} - treating this as complete "
            f"would let a domain be bought twice")

    broken = _list_with(_PagedHTTP([], error=True))
    r.check("an error is an error, not an empty account", "error" in broken,
            f"{broken!r}")
    r.check("...carrying the provider's number", "5019169" in str(broken),
            f"{broken!r}")


def check_success_path_unchanged(r: Results) -> None:
    print("\nsuccess: the ordinary path is untouched")
    row = FakeRow()
    out, ordered = _domain_stage(row, _EMPTY)
    r.check("registered", out.get("status") == "registered", f"{out!r}")
    r.check("one order", len(ordered) == 1, f"{ordered!r}")
    r.check("the row records the purchase",
            any(w.get("Domain Status") == "registered" for w in row.writes),
            f"{row.writes!r}")
    r.check("no reconciliation was needed on the happy path",
            out.get("reconciled") is not True, f"{out!r}")


def main() -> int:
    print("test_auto_recovery.py — stages that recover without a human")
    print(f"  + {os.path.join(REPO, 'run_pipeline.py')} (download_certificate paging)")
    print(f"  + {os.path.join(REPO, 'namecheap_automation.py')} (candidate generation)")
    r = Results()
    check_incorporation_beyond_the_first_page(r)
    check_first_page_hit_costs_one_request(r)
    check_fallback_picks_the_oldest_by_date(r)
    check_no_documents_at_all_still_reports(r)
    check_paging_is_bounded(r)
    check_variants_when_the_plain_name_is_taken(r)
    check_candidate_budget(r)
    check_exact_name_still_wins(r)
    check_choice_does_not_depend_on_response_order(r)
    check_cheaper_tld_still_wins(r)
    check_no_oversized_labels(r)
    check_premium_still_excluded(r)
    check_alternatives_are_offered(r)
    check_nothing_available_still_reports(r)
    check_error_number_survives_parsing(r)
    check_register_domain_surfaces_the_number(r)
    check_operator_sees_the_number(r)
    check_outstanding_claim_resolves_itself(r)
    check_outstanding_claim_without_a_domain_stops(r)
    check_truncated_listing_is_not_proof(r)
    check_failed_listing_is_not_proof(r)
    check_refusal_keeps_the_claim(r)
    check_unresolved_order_recovers_when_the_domain_appears(r)
    check_unresolved_without_the_domain_still_stops(r)
    check_another_companys_domain_is_never_claimed(r)
    check_ambiguous_account_is_not_guessed(r)
    check_young_claim_is_not_released(r)
    check_expired_claim_is_released_and_buys(r)
    check_expiry_never_fires_on_unknown(r)
    check_expiry_never_fires_when_recovered(r)
    check_real_prices_decide(r)
    check_account_rate_beats_list_price(r)
    check_what_a_tld_actually_billed_is_used(r)
    check_observed_charge_never_lowers_the_estimate(r)
    check_absurd_past_charge_is_ignored(r)
    check_no_history_uses_the_quote(r)
    check_icann_fee_is_part_of_the_price(r)
    check_fee_can_change_the_choice(r)
    check_no_fee_quoted_is_unchanged(r)
    check_balance_check_includes_the_fee(r)
    check_price_fallback_is_the_old_order(r)
    check_unpriced_tld_never_outranks_a_priced_one(r)
    check_insufficient_balance_never_orders(r)
    check_sufficient_balance_proceeds(r)
    check_unknown_balance_does_not_block(r)
    check_currency_mismatch_does_not_block(r)
    check_order_creation_failed_releases_the_claim(r)
    check_order_creation_failed_keeps_claim_when_unknown(r)
    check_other_refusals_still_keep_the_claim(r)
    check_account_listing_completeness(r)
    check_success_path_unchanged(r)
    return r.report()


if __name__ == "__main__":
    raise SystemExit(main())
