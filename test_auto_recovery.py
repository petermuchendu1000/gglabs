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
    return r.report()


if __name__ == "__main__":
    raise SystemExit(main())
