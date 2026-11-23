#!/usr/bin/env python3
"""Paging through the PSC index, in the admin panel and the Add dialog.

Both lists already took limit and offset and neither ever sent one, so
both showed a first page and nothing said there was a second. With 6,759
companies in the index that is not a cosmetic problem: an operator
searching for a company that sorts late simply cannot reach it, and the
screen gives no reason to suspect it exists.

The property that makes pagination either work or quietly lie is this:

THE COUNT AND THE ROWS MUST ANSWER THE SAME QUESTION. search() filters by
`q` and, by default, excludes ceased PSCs. A count that forgets either
filter produces "showing 1-50 of 6,759" above a list whose third page is
empty - and an operator reading that concludes the data is missing rather
than the arithmetic. The two must be built from one predicate, not two
that happen to agree today.

The second property is that paging must cover the set exactly once. Off
by one in either direction is invisible on screen: a skipped row looks
like a row that was never in the index, and a repeated one looks like
duplicate data.

The DB-backed checks need PostgreSQL. Without DATABASE_URL they skip, and
say so, rather than passing on no evidence:

    DATABASE_URL=postgresql://postgres@127.0.0.1:5432/scratch \\
        python3 test_pagination.py
"""
from __future__ import annotations

import os
import re
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

import flask

import psc_index


class Results:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.skipped = 0

    def check(self, name: str, cond: bool, detail: str = "") -> bool:
        if cond:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed += 1
            print(f"  FAIL  {name}")
            if detail:
                for line in str(detail).splitlines():
                    print(f"          {line}")
        return bool(cond)

    def skip(self, name: str, why: str) -> None:
        self.skipped += 1
        print(f"  SKIP  {name} - {why}")

    def report(self) -> int:
        print(f"\n{self.passed} passed / {self.failed} failed "
              f"/ {self.skipped} skipped")
        return 1 if self.failed else 0


# ── one predicate, not two ──────────────────────────────────────────────────
def check_one_predicate_feeds_both(r: Results) -> None:
    print("\n-- the count and the page are built from the same filter --")
    src = open(os.path.join(REPO, "psc_index.py"), encoding="utf-8").read()

    if not r.check("there is a count()", "def count(" in src,
                   "without one the page cannot know there is a next page"):
        return

    def body(name: str) -> str:
        i = src.index(f"def {name}(")
        j = src.index("\ndef ", i + 1)
        return "\n".join(l for l in src[i:j].splitlines()
                         if not l.strip().startswith("#"))

    search_src, count_src = body("search"), body("count")
    r.check("count() does not rebuild the WHERE clause itself",
            "ceased_on IS NULL" not in count_src,
            "a second copy of the filter is a second thing to keep in step, "
            "and the failure is silent: the total stays plausible while the "
            "last page goes empty")
    r.check("search() does not either",
            search_src.count("ceased_on IS NULL") == 0,
            "both must read the shared helper")
    r.check("both call the shared predicate builder",
            "_where(" in search_src and "_where(" in count_src,
            f"search uses _where: {'_where(' in search_src}, "
            f"count uses _where: {'_where(' in count_src}")
    r.check("count() asks the database to count, not len() of a page",
            "count(" in count_src.lower() and "LIMIT" not in count_src,
            "counting a limited page returns the page size, so the total "
            "would equal the page and Next would never appear")


# ── the routes ──────────────────────────────────────────────────────────────
def check_routes_report_the_total(r: Results) -> None:
    print("\n-- both routes tell the page how much there is --")
    app = flask.Flask(__name__)

    import admin_routes
    saved = (psc_index.search, psc_index.count)
    psc_index.search = lambda q="", limit=50, offset=0, include_ceased=False: [
        {"company_number": f"{i:08d}", "psc_name": "X", "nationality":
         "Kenyan", "ceased_on": None} for i in range(offset, offset + limit)]
    psc_index.count = lambda q="", include_ceased=False: 137
    try:
        with app.test_request_context(
                "/api/admin/psc/search?q=&limit=25&offset=50"):
            body = admin_routes.api_psc_search()
        d = body.get_json() if hasattr(body, "get_json") else body[0].get_json()
        r.check("admin search returns a total", d.get("total") == 137, repr(d)[:200])
        r.check("...and echoes the limit it used", d.get("limit") == 25, repr(d)[:200])
        r.check("...and the offset it used", d.get("offset") == 50, repr(d)[:200])
        r.check("...alongside the rows", len(d.get("rows", [])) == 25,
                f"{len(d.get('rows', []))} rows")
    finally:
        psc_index.search, psc_index.count = saved


def check_candidates_route_reports_the_total(r: Results) -> None:
    print("\n-- the Add dialog's route does the same --")
    import api_v2
    app = flask.Flask(__name__)
    saved = (psc_index.search_companies, psc_index.count_companies,
             psc_index.status, api_v2._all_rows, api_v2._compute_stages,
             api_v2._profile_name)
    psc_index.search_companies = (
        lambda q="", limit=25, offset=0, include_ceased=False: [
            {"company_number": f"{i:08d}", "psc_name": "X", "nationality":
             "Kenyan", "company_name": "N LTD", "company_status": "active",
             "director_verdict": "", "psc_count": 1}
            for i in range(offset, offset + limit)])
    psc_index.count_companies = lambda q="", include_ceased=False: 6759
    psc_index.status = lambda: {"rows": 7416, "companies": 6759,
                                "current": 6486, "current_companies": 6152,
                                "ceased": 930, "snapshot_date": "2026-11-22"}
    api_v2._all_rows = lambda: []
    api_v2._compute_stages = lambda cn, row: {}
    api_v2._profile_name = lambda cn: ("", "")
    try:
        with app.test_request_context(
                "/api/v2/company/candidates?limit=25&offset=25&enrich=0"):
            resp = api_v2.v2_candidates()
        d = (resp[0] if isinstance(resp, tuple) else resp).get_json()
        r.check("candidates returns a total", d.get("total") == 6759,
                repr(d)[:220])
        r.check("...and echoes limit and offset",
                d.get("limit") == 25 and d.get("offset") == 25, repr(d)[:220])
    finally:
        (psc_index.search_companies, psc_index.count_companies,
         psc_index.status, api_v2._all_rows, api_v2._compute_stages,
         api_v2._profile_name) = saved


# ── the pages ───────────────────────────────────────────────────────────────
def check_the_admin_page_can_page(r: Results) -> None:
    print("\n-- the admin panel has working controls --")
    src = open(os.path.join(REPO, "admin_routes.py"), encoding="utf-8").read()
    js = re.search(r"<script>(.*?)</script>", src, re.S).group(1)
    live = "\n".join(l for l in js.splitlines()
                     if not l.strip().startswith("//"))

    r.check("it tracks an offset", "PSC_OFFSET" in live,
            "without state the page cannot be on page 2")
    r.check("it sends the offset", "offset=" in live, "the route would "
            "always return the first page however the buttons behave")
    r.check("there are previous and next controls",
            "psc-prev" in src and "psc-next" in src, "no way to move")
    # Pinned to the condition, not just the assignment. `PSC_OFFSET = 0`
    # under `if (false)` satisfies a looser check and does nothing, which
    # is exactly the mutant that survived the first run of this test.
    r.check("a new search returns to the first page",
            re.search(r"if\s*\(\s*!\s*keepPage\s*\)\s*PSC_OFFSET\s*=\s*0",
                      live) is not None,
            "typing a new query on page 4 shows its fourth page, which "
            "usually means an empty screen for a query that has matches")
    r.check("...and paging moves by exactly one page",
            re.search(r"PSC_OFFSET\s*\+\s*delta\s*\*\s*PSC_LIMIT", live)
            is not None,
            "a step that is not the page size skips or repeats rows")
    r.check("the range is stated in words",
            "psc-range" in src,
            "a bare Next gives no idea how much is left, and no way to see "
            "that a filter narrowed 6,759 rows to 3")

    # Found while driving the paged panel in a browser: the table was only
    # ever filled when a FETCH JOB finished, so opening the page with an
    # index already built showed headings over nothing. That reads as an
    # empty index, which is the one conclusion the screen must not invite
    # when 6,759 rows are sitting in the table.
    init = live[live.rindex("pscStatus();"):]
    r.check("the list loads when the page opens",
            "pscSearch();" in init,
            "the table is populated only on job completion, so a visit "
            "with the index already built shows an empty table")


def check_the_add_dialog_can_page(r: Results) -> None:
    print("\n-- the Add Company dialog has them too --")
    page = open(os.path.join(REPO, "static", "app.html"),
                encoding="utf-8").read()
    live = "\n".join(l for l in page.splitlines()
                     if not l.strip().startswith("//"))
    r.check("it tracks an offset", "CAND_OFFSET" in live, "no page state")
    r.check("there are controls", "cand-prev" in page and "cand-next" in page,
            "6,759 companies behind a list that shows 25")
    r.check("a new search returns to the first page",
            re.search(r"if\s*\(\s*!\s*keepPage\s*\)\s*CAND_OFFSET\s*=\s*0",
                      live) is not None,
            "same trap as the admin panel, and the same surviving mutant")
    r.check("...and paging moves by exactly one page",
            re.search(r"CAND_OFFSET\s*\+\s*delta\s*\*\s*CAND_LIMIT", live)
            is not None,
            "a step that is not the page size skips or repeats rows")
    r.check("the range is shown", "cand-range" in page,
            "the operator cannot tell 25 of 25 from 25 of 6,759")


# ── against a real database ─────────────────────────────────────────────────
def check_against_postgres(r: Results) -> None:
    print("\n-- paging a real table --")
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        r.skip("count and paging against PostgreSQL",
               "DATABASE_URL is not set")
        return

    import db
    psc_index.ensure_schema()
    with db._cursor() as cur:
        cur.execute(f"DELETE FROM {psc_index.TABLE}")

    # 30 current, 7 ceased. Distinct, ordered company numbers.
    rows = []
    for i in range(37):
        rows.append({
            "company_number": f"{i:08d}",
            "etag": f"e{i}", "psc_name": f"Person {i}",
            "nationality": "Kenyan", "country_of_residence": "Kenya",
            "natures_of_control": "ownership-of-shares-25-to-50-percent",
            "notified_on": "2024-01-01",
            "ceased_on": "2025-02-02" if i >= 30 else None,
            "dob_month": 4, "dob_year": 1980, "title": "Mr",
            "forename": "A", "middle_name": "", "surname": f"S{i}",
            "address": "1 Road", "identity_verified_on": None,
            "raw": {"n": i},
        })
    psc_index._replace("2026-11-22", rows)

    total_current = psc_index.count()
    r.check("count excludes ceased by default", total_current == 30,
            f"got {total_current}, expected 30 - the same default search() "
            f"applies, or the total overstates what the list can show")
    r.check("count can include them", psc_index.count(include_ceased=True) == 37,
            f"got {psc_index.count(include_ceased=True)}")

    # A query that matches a known subset.
    # 11, not 10: the match is a substring, so 00000001 matches as well as
    # 00000010-00000019. Worth stating, because the obvious expectation
    # here is 10 and the code is the thing that is right.
    r.check("count respects q",
            psc_index.count(q="0000001") == 11,
            f"got {psc_index.count(q='0000001')}, expected 11 "
            f"(00000001 and 00000010-00000019, all current)")
    r.check("...and q agrees with what search returns",
            len(psc_index.search(q="0000001", limit=500))
            == psc_index.count(q="0000001"),
            "the total and the list must answer the same question")

    # Walk it in pages of 7 and demand exact coverage.
    seen: list[str] = []
    off = 0
    while True:
        page = psc_index.search(limit=7, offset=off)
        if not page:
            break
        seen += [x["company_number"] for x in page]
        off += 7
        if off > 500:
            break
    expected = [f"{i:08d}" for i in range(30)]
    r.check("paging covers every current row exactly once",
            seen == expected,
            f"{len(seen)} rows, {len(set(seen))} distinct; "
            f"missing={sorted(set(expected) - set(seen))} "
            f"extra={sorted(set(seen) - set(expected))}")
    r.check("...and the walk agrees with the count",
            len(seen) == total_current,
            f"walked {len(seen)}, count said {total_current}")

    past = psc_index.search(limit=7, offset=total_current)
    r.check("an offset past the end is empty, not an error", past == [],
            repr(past)[:160])

    with db._cursor() as cur:
        cur.execute(f"DELETE FROM {psc_index.TABLE}")


# ── one row per COMPANY, not per PSC record ─────────────────────────────────
def check_company_grouping_pairs_too(r: Results) -> None:
    """The Add dialog pages by COMPANY. Its count must do the same.

    search() answers "which PSC records match" and that is right for the
    admin panel, which is a PSC viewer. The Add dialog is choosing a
    COMPANY, and a company with two Kenyan PSCs is one choice, not two.

    Pairing search_companies with count() instead of count_companies()
    would be the same drift as before, one level up: "page 1 of 271" over
    a list that runs out at page 247, because the total counted records
    and the pages counted companies.
    """
    print("\n-- the company-grouped view has its own matching count --")
    src = open(os.path.join(REPO, "psc_index.py"), encoding="utf-8").read()
    if not r.check("there is a search_companies()", "def search_companies(" in src,
                   "the dialog would keep listing one card per PSC record"):
        return
    if not r.check("there is a count_companies()", "def count_companies(" in src,
                   "no honest total for a company-grouped page"):
        return

    def body(name):
        i = src.index(f"def {name}(")
        j = src.index("\ndef ", i + 1)
        return "\n".join(l for l in src[i:j].splitlines()
                          if not l.strip().startswith("#"))

    sc, cc = body("search_companies"), body("count_companies")
    r.check("search_companies groups by company_number",
            "GROUP BY company_number" in sc, sc[:300])
    r.check("count_companies counts DISTINCT company_number",
            "count(DISTINCT company_number)" in cc, cc[:300])
    r.check("both reuse the shared predicate",
            "_where(" in sc and "_where(" in cc,
            "a third and fourth copy of the ceased/query filter")
    r.check("the grouped page still pages",
            "LIMIT" in sc and "OFFSET" in sc,
            "grouping without paging loads 6,152 companies into the dialog")
    r.check("it reports how many PSCs each company has",
            "psc_count" in sc,
            "collapsing two rows into one silently loses the fact that two "
            "Kenyan people control the company")


def check_candidates_route_pages_by_company(r: Results) -> None:
    print("\n-- the Add route uses the company-grouped pair --")
    src = open(os.path.join(REPO, "api_v2.py"), encoding="utf-8").read()
    fn = src[src.index("def v2_candidates"):src.index("def v2_add_company")]
    live_src = "\n".join(l for l in fn.splitlines()
                          if not l.strip().lstrip("#").strip() == l.strip()
                          or not l.strip().startswith("#"))
    r.check("it calls search_companies", "search_companies(" in live_src, fn[:400])
    r.check("...and count_companies", "count_companies(" in live_src, fn[:400])
    r.check("it does NOT pair the grouped page with the record count",
            "psc_index.count(q=" not in live_src,
            "total would count PSC records while the pages count companies, "
            "so the last pages would be empty")
    r.check("psc_count reaches the dialog", "psc_count" in live_src,
            "the card cannot say '2 Kenyan PSCs' without it")


def check_grouping_against_postgres(r: Results) -> None:
    print("\n-- grouping, against a real table --")
    if not os.environ.get("DATABASE_URL", "").strip():
        r.skip("company grouping against PostgreSQL", "DATABASE_URL is not set")
        return
    import db
    psc_index.ensure_schema()
    with db._cursor() as cur:
        cur.execute(f"DELETE FROM {psc_index.TABLE}")

    def row(cn, etag, name, nat="Kenyan", ceased=None):
        return {"company_number": cn, "etag": etag, "psc_name": name,
                "nationality": nat, "country_of_residence": "Kenya",
                "natures_of_control": "ownership", "notified_on": "2024-01-01",
                "ceased_on": ceased, "dob_month": 4, "dob_year": 1980,
                "title": "Mr", "forename": "A", "middle_name": "",
                "surname": "S", "address": "1 Road",
                "identity_verified_on": None, "raw": {}}

    # 00000001 has THREE Kenyan PSCs, 00000002 has one, 00000003 has one
    # ceased only, 00000004 has one current and one ceased.
    psc_index._replace("2026-11-22", [
        row("00000001", "a", "Ann K"), row("00000001", "b", "Ben K"),
        row("00000001", "c", "Cal K"),
        row("00000002", "d", "Dee K"),
        row("00000003", "e", "Eve K", ceased="2025-01-01"),
        row("00000004", "f", "Fay K"),
        row("00000004", "g", "Gus K", ceased="2025-01-01"),
    ])

    rows = psc_index.search_companies(limit=50)
    cns = [x["company_number"] for x in rows]
    r.check("one row per company, not per record",
            cns == ["00000001", "00000002", "00000004"],
            f"got {cns} - 7 records across 4 companies, of which 3 have a "
            f"CURRENT Kenyan PSC")
    r.check("...and the count agrees",
            psc_index.count_companies() == 3,
            f"count_companies()={psc_index.count_companies()}, "
            f"rows={len(rows)}")
    r.check("the per-record count still differs, as it should",
            psc_index.count() == 5,
            f"count()={psc_index.count()} - 5 current records")

    first = rows[0]
    r.check("a company with three PSCs says so",
            first.get("psc_count") == 3, repr(first)[:220])
    r.check("...and names all three",
            all(n in (first.get("psc_name") or "")
                for n in ("Ann K", "Ben K", "Cal K")), repr(first)[:220])
    r.check("a ceased PSC is not counted in its company's total",
            next(x for x in rows if x["company_number"] == "00000004")
            .get("psc_count") == 1,
            "00000004 has one current and one ceased")

    r.check("include_ceased brings the ceased-only company back",
            psc_index.count_companies(include_ceased=True) == 4,
            f"got {psc_index.count_companies(include_ceased=True)}")

    # Paging by company must cover each exactly once.
    seen, off = [], 0
    while True:
        page = psc_index.search_companies(limit=2, offset=off)
        if not page:
            break
        seen += [x["company_number"] for x in page]
        off += 2
        if off > 100:
            break
    r.check("paging covers every company exactly once",
            seen == ["00000001", "00000002", "00000004"],
            f"{seen}")

    r.check("a query matching one PSC surfaces their company once",
            [x["company_number"] for x in psc_index.search_companies(q="Ben K")]
            == ["00000001"],
            repr(psc_index.search_companies(q="Ben K")))

    with db._cursor() as cur:
        cur.execute(f"DELETE FROM {psc_index.TABLE}")


def main() -> int:
    r = Results()
    print("test_pagination.py - paging the PSC index")
    check_one_predicate_feeds_both(r)
    check_routes_report_the_total(r)
    check_candidates_route_reports_the_total(r)
    check_the_admin_page_can_page(r)
    check_the_add_dialog_can_page(r)
    check_against_postgres(r)
    check_company_grouping_pairs_too(r)
    check_candidates_route_pages_by_company(r)
    check_grouping_against_postgres(r)
    return r.report()


if __name__ == "__main__":
    raise SystemExit(main())
