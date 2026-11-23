#!/usr/bin/env python3
"""What the Add Company dialog offers, and what it must never imply.

The dialog is the admission path: what it lists is what an operator can
add, and adding a company leads to a billed domain purchase. Two properties
matter more than anything else it does.

LISTING IS NOT ADMITTING. A row comes from the PSC index, which says a
Kenyan person CONTROLS the company. The pipeline's rule is about
DIRECTORS. Those usually agree and are not the same fact, so the rule is
enforced at the write in v2_add_company - and that enforcement must not be
quietly removed on the grounds that the list is already filtered. The
route's own comment says exactly this; the test holds it to it.

ALREADY-WORKED-ON COMPANIES MUST BE VISIBLE AND MARKED. Hiding them makes
an operator wonder whether a company was ever added; showing them
unmarked invites a second attempt. The count of completed stages is what
tells the two apart, and it has to be the real count, not the presence of
a row.
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

import flask

import api_v2


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


_APP = flask.Flask(__name__)

# Three candidates: one untouched, one part-way, one finished.
INDEX_ROWS = [
    {"company_number": "00000001", "psc_name": "Mr A Kariuki",
     "nationality": "Kenyan", "company_name": "", "company_status": "",
     "director_verdict": ""},
    {"company_number": "00000002", "psc_name": "Ms B Wanjiru",
     "nationality": "British,Kenyan", "company_name": "PARTWAY LTD",
     "company_status": "active", "director_verdict": "admissible"},
    {"company_number": "16811331", "psc_name": "Mr Simon Mwangi",
     "nationality": "Kenyan", "company_name": "SIMON MWANGI LTD",
     "company_status": "active", "director_verdict": "admissible"},
]

PIPELINE = {
    "00000002": {"Company Number": "00000002", "Company Name": "PARTWAY LTD"},
    "16811331": {"Company Number": "16811331",
                 "Company Name": "SIMON MWANGI LTD"},
}

STAGES = {
    "00000002": {s: i < 3 for i, s in enumerate(api_v2.STAGE_ORDER)},
    "16811331": {s: True for s in api_v2.STAGE_ORDER},
}


def call(url: str, index_rows=None, profiles=None):
    """Run the route with the index, pipeline and Companies House stubbed."""
    import psc_index
    saved = (psc_index.search, psc_index.status, psc_index.record_company_facts,
             api_v2._all_rows, api_v2._compute_stages, api_v2._profile_name)
    written: dict = {}
    rows = [dict(r) for r in (index_rows if index_rows is not None
                              else INDEX_ROWS)]
    psc_index.search = lambda q="", limit=25, offset=0, include_ceased=False: [
        r for r in rows
        if not q or q.lower() in r["company_number"].lower()
        or q.lower() in (r["psc_name"] or "").lower()]
    psc_index.status = lambda: {"rows": len(rows), "companies": len(rows),
                                "current": len(rows),
                                "current_companies": len(rows),
                                "ceased": 0, "snapshot_date": "2026-11-22"}
    psc_index.record_company_facts = (
        lambda cn, **kw: written.update({cn: kw}))
    api_v2._all_rows = lambda: list(PIPELINE.values())
    api_v2._compute_stages = lambda cn, row: STAGES.get(
        cn, {s: False for s in api_v2.STAGE_ORDER})
    api_v2._profile_name = lambda cn: (profiles or {}).get(cn, ("", ""))
    try:
        with _APP.test_request_context(url):
            resp = api_v2.v2_candidates()
        body = resp[0] if isinstance(resp, tuple) else resp
        return body.get_json(), written
    finally:
        (psc_index.search, psc_index.status, psc_index.record_company_facts,
         api_v2._all_rows, api_v2._compute_stages,
         api_v2._profile_name) = saved


def check_pipeline_state_is_shown(r: Results) -> None:
    print("\n-- companies already worked on are listed and marked --")
    data, _ = call("/api/v2/company/candidates")
    rows = {x["cn"]: x for x in (data or {}).get("rows", [])}

    r.check("every candidate is listed", len(rows) == 3, sorted(rows))
    r.check("an untouched company is listed",
            "00000001" in rows and rows["00000001"]["in_pipeline"] is False,
            repr(rows.get("00000001")))
    r.check("...with no stages done",
            rows["00000001"]["n_done"] == 0, repr(rows["00000001"]))

    r.check("a company already in the pipeline is STILL listed",
            "16811331" in rows,
            "hiding it makes an operator wonder whether it was ever added")
    r.check("...and is marked as in the pipeline",
            rows["16811331"]["in_pipeline"] is True, repr(rows["16811331"]))
    r.check("...and reports every stage done",
            rows["16811331"]["n_done"] == len(api_v2.STAGE_ORDER),
            f"got {rows['16811331']['n_done']} of "
            f"{len(api_v2.STAGE_ORDER)}")

    r.check("a part-finished company reports its real progress",
            rows["00000002"]["n_done"] == 3,
            f"got {rows['00000002']['n_done']} - the count must come from "
            f"_compute_stages, not from the row merely existing")
    r.check("...and is distinguishable from a finished one",
            rows["00000002"]["n_done"] < rows["16811331"]["n_done"],
            "in progress and completed would render identically")

    r.check("the total stage count is sent, not hardcoded in the page",
            all(x["total_stages"] == len(api_v2.STAGE_ORDER)
                for x in rows.values()),
            "a stage added later would make every badge read N/7 for ever")


def check_names_are_filled_but_bounded(r: Results) -> None:
    print("\n-- names are fetched for rows that lack one, and only those --")
    profiles = {"00000001": ("UNTOUCHED LTD", "active")}
    data, written = call("/api/v2/company/candidates", profiles=profiles)
    rows = {x["cn"]: x for x in (data or {}).get("rows", [])}

    r.check("a missing name is fetched",
            rows["00000001"]["name"] == "UNTOUCHED LTD", repr(rows["00000001"]))
    r.check("...and cached back into the index",
            written.get("00000001", {}).get("company_name") == "UNTOUCHED LTD",
            f"written={written!r} - without this the same lookup is paid "
            f"for on every open of the dialog")
    r.check("a name already held is not re-fetched",
            "00000002" not in written,
            f"written={written!r} - it already had PARTWAY LTD")

    # enrich=0 must spend nothing at all.
    calls: list = []
    import psc_index
    data, written = call("/api/v2/company/candidates?enrich=0",
                         profiles=profiles)
    rows = {x["cn"]: x for x in (data or {}).get("rows", [])}
    r.check("enrich=0 fetches no names", written == {}, repr(written))
    r.check("...and the row is still returned, just unnamed",
            rows["00000001"]["name"] == "",
            "a row without a name is still a company an operator can pick")


def check_empty_index_is_distinguishable(r: Results) -> None:
    """An index nobody built and an index with no match need different
    next steps, and the page can only tell them apart if the route says."""
    print("\n-- an unbuilt index is not the same as no match --")
    data, _ = call("/api/v2/company/candidates", index_rows=[])
    r.check("no rows come back", (data or {}).get("rows") == [], repr(data))
    r.check("...but the index status does",
            "index" in (data or {}),
            "the page cannot tell 'never built' from 'no match' and would "
            "send the operator to the wrong place")

    page = open(os.path.join(REPO, "static", "app.html"),
                encoding="utf-8").read()
    r.check("the page distinguishes them",
            "The company index is empty" in page
            and "Nothing in the index matches that" in page,
            "one message for two different problems")
    r.check("...and falls back to Companies House either way",
            page.count("doAddSearch(q)") >= 2,
            "the index only holds companies with a Kenyan PSC; a company "
            "with a Kenyan DIRECTOR and a foreign PSC is not in it, so "
            "losing the live search would lose reach")


def check_listing_does_not_admit(r: Results) -> None:
    """The rule is enforced at the write. Listing must not become the gate."""
    print("\n-- listing a company is not admitting it --")
    src = open(os.path.join(REPO, "api_v2.py"), encoding="utf-8").read()
    add = src[src.index("def v2_add_company"):]
    add = add[:add.index("\n@api_v2.route")]

    r.check("the add route still checks the director rule",
            "_fetch_kenyan(cn)" in add,
            "the only thing standing between a candidate list and an "
            "ineligible company in the pipeline")
    r.check("...and refuses with 422 when it fails",
            "422" in add, "an ineligible company would be added silently")
    r.check("the candidates route does NOT claim to have checked it",
            "_fetch_kenyan" not in src[src.index("def v2_candidates"):
                                       src.index("def v2_add_company")],
            "listing must not imply an admission it has not established")

    page = open(os.path.join(REPO, "static", "app.html"),
                encoding="utf-8").read()
    r.check("the dialog says the rule is checked on add",
            "director rule is checked as it is added" in page,
            "a filtered list reads as pre-approved unless it says otherwise")
    r.check("...and says a controlling shareholder is not a director",
            "not automatically a director" in page,
            "the PSC/director distinction is the one thing an operator "
            "could reasonably get wrong from this screen")


def check_the_dialog_opens_with_the_list(r: Results) -> None:
    print("\n-- the dialog shows the list without being typed into --")
    page = open(os.path.join(REPO, "static", "app.html"),
                encoding="utf-8").read()
    opener = page[page.index("function openAddModal"):]
    opener = opener[:opener.index("function closeAddModal")]
    # Comment lines are stripped before looking. A substring check matched
    # `// loadCandidates("")` just as happily as the real call, so a mutant
    # that commented the line out survived - the third time this session a
    # check has read commentary as code.
    live = "\n".join(l for l in opener.splitlines()
                     if not l.strip().startswith("//"))
    r.check("opening the dialog loads the candidates",
            "loadCandidates(" in live,
            "it opens blank and waits to be typed into, which hides the "
            "fact that the list is already held")
    r.check("an emptied search box shows everything again",
            'loadCandidates(q), q ? 220 : 0' in page,
            "deleting what you typed used to clear the dialog to nothing")
    r.check("the three states have distinct badges",
            all(s in page for s in ("not started", "in progress",
                                    "completed ")),
            "an operator cannot tell a finished company from an untouched "
            "one")


def main() -> int:
    r = Results()
    print("test_add_candidates.py - what the Add dialog offers")
    check_pipeline_state_is_shown(r)
    check_names_are_filled_but_bounded(r)
    check_empty_index_is_distinguishable(r)
    check_listing_does_not_admit(r)
    check_the_dialog_opens_with_the_list(r)
    return r.report()


if __name__ == "__main__":
    raise SystemExit(main())
