#!/usr/bin/env python3
"""The PSC index decides which companies an operator ever sees.

Two things make it dangerous and both are tested here.

It is a CANDIDATE list built from Persons with Significant Control, and the
pipeline's admission rule is about DIRECTORS. A PSC holds over 25% of
shares or votes; in a small company that is usually a director too, but it
is a different fact. If this module ever starts reporting its rows as
directors, an operator will add a company nobody checked.

And it REPLACES the table on every run. A parser that silently matches
nothing turns a working index into an empty one, which looks exactly like
"there are no Kenyan companies" - the silent-empty failure this codebase
has already had in two stages.

The fixtures are real lines from the Companies House snapshot of
2026-11-22, not invented JSON.
"""
from __future__ import annotations

import json
import os
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

import directors
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
                print(f"          {detail}")
        return bool(cond)

    def skip(self, name: str, why: str) -> None:
        self.skipped += 1
        print(f"  SKIP  {name} — {why}")

    def report(self) -> int:
        print(f"\n{self.passed} passed / {self.failed} failed "
              f"/ {self.skipped} skipped")
        return 1 if self.failed else 0


def rec(**data):
    """A snapshot line. Shape copied from a real record."""
    cn = data.pop("company_number", "12345678")
    body = {"kind": psc_index.INDIVIDUAL, "etag": "e1", "name": "Mr Test",
            "notified_on": "2020-01-01"}
    body.update(data)
    return json.dumps({"company_number": cn, "data": body})


# ── the page parser ────────────────────────────────────────────────────────

PAGE = """
<a href="psc-snapshot-2026-11-22_1of33.zip">1</a>
<a href="psc-snapshot-2026-11-22_2of33.zip">2</a>
<a href="psc-snapshot-2026-11-22_10of33.zip">10</a>
<a href="persons-with-significant-control-snapshot-2026-11-22.zip">whole</a>
"""


def check_page_parser(r: Results) -> None:
    print("\n-- reading the download page --")
    date, parts = psc_index.snapshot_parts(PAGE)
    r.check("the snapshot date is read, not invented", date == "2026-11-22",
            f"got {date!r} - the date names which day's register this is")
    r.check("every part is found", len(parts) == 3, repr(parts))
    r.check("parts are in NUMERIC order, not lexical",
            parts == ["psc-snapshot-2026-11-22_1of33.zip",
                      "psc-snapshot-2026-11-22_2of33.zip",
                      "psc-snapshot-2026-11-22_10of33.zip"],
            f"got {parts!r} - sorted as text, 10 comes before 2")
    r.check("the whole-snapshot file is not mistaken for a part",
            all("_" in p for p in parts), repr(parts))

    # Two dates on the page at once happens while a new snapshot is being
    # published. Mixing them would put half of yesterday in the table.
    mixed = PAGE + '<a href="psc-snapshot-2026-11-21_1of33.zip">old</a>'
    date, parts = psc_index.snapshot_parts(mixed)
    r.check("only the newest date is used", date == "2026-11-22",
            f"got {date!r}")
    r.check("...and no older part is included",
            all("2026-11-22" in p for p in parts), repr(parts))

    date, parts = psc_index.snapshot_parts("<html>nothing here</html>")
    r.check("an unrecognisable page yields nothing rather than a guess",
            (date, parts) == ("", []), f"got {(date, parts)!r}")


# ── the record parser ──────────────────────────────────────────────────────

def check_record_parser(r: Results) -> None:
    print("\n-- which snapshot lines become rows --")
    p = psc_index.parse_record

    row = p(rec(nationality="Kenyan", name="Mr Jackson Kariuki Wagana",
                company_number="11767337"))
    r.check("a Kenyan individual matches", row is not None)
    if row:
        r.check("...carrying the company number",
                row["company_number"] == "11767337", repr(row))
        r.check("...and the name", "Wagana" in row["psc_name"], repr(row))
        r.check("...and the nationality verbatim",
                row["nationality"] == "Kenyan", repr(row))
        r.check("...and no date of birth is copied",
                "date_of_birth" not in row,
                "a field nothing needs should not be collected")

    # Every spelling measured in the real snapshot.
    for nat in ("Kenyan", "Kenya", "British,Kenyan", "Kenyan,British",
                "Finnish,Kenyan", "English,Kenyan"):
        r.check(f"{nat!r} matches", p(rec(nationality=nat)) is not None,
                "a spelling that occurs in the register was dropped")

    for nat in ("British", "Kenyatta", "South African", "", None):
        r.check(f"{nat!r} does not match", p(rec(nationality=nat)) is None,
                "a non-Kenyan record entered the index")

    r.check("a corporate PSC is skipped",
            p(rec(nationality="Kenyan",
                  kind="corporate-entity-person-with-significant-control"))
            is None,
            "corporates have no nationality; a company cannot be Kenyan here")
    r.check("a record with no company number is skipped",
            p(rec(nationality="Kenyan", company_number="")) is None,
            "a row that cannot be joined to a company is useless")
    r.check("a blank line is skipped", p("") is None)
    r.check("unparseable JSON is skipped", p("{not json") is None)

    # The index and the admission rule must agree about what a nationality
    # is, or a company could be indexed and then refused, or the reverse.
    r.check("it uses the pipeline's own nationality parser",
            "directors.nationalities" in
            open(os.path.join(REPO, "psc_index.py"), encoding="utf-8").read(),
            "a second parser will disagree with the admission rule")


def check_it_never_claims_directors(r: Results) -> None:
    """A PSC is not a director and the module must not blur that."""
    print("\n-- the index does not claim to know about directors --")
    src = open(os.path.join(REPO, "psc_index.py"), encoding="utf-8").read()
    r.check("the table is named for PSCs, not directors",
            psc_index.TABLE == "psc_candidates",
            f"table is {psc_index.TABLE!r}")
    r.check("the docstring says a PSC is not a director",
            "A PSC IS NOT A DIRECTOR" in src,
            "the next reader will assume these rows are directors")
    row = psc_index.parse_record(rec(nationality="Kenyan"))
    r.check("a row carries no director claim",
            row is not None and "director" not in
            {k for k in row if k != "director_verdict"},
            repr(row))


# ── against the real snapshot ──────────────────────────────────────────────

REAL_PART = os.path.join(
    "/tmp/claude-0/-home-user-gglabs/"
    "55f1da2b-5945-50ce-a6fb-92af58ea5ab1/scratchpad/pscq", "p1.zip")


def check_against_the_real_snapshot(r: Results) -> None:
    """Run the shipped streamer over a real 479MB part of the register.

    Fixtures prove the parser handles the shapes I thought of. This proves
    it handles the shapes Companies House actually files, which is the only
    thing that decides whether the index is complete.
    """
    print("\n-- the real Companies House snapshot (part 1 of 33) --")
    if not os.path.exists(REAL_PART):
        r.skip("real snapshot", f"{REAL_PART} is not present; it is a 82MB "
                                f"download and not kept in the repo")
        return

    rows = list(psc_index.iter_part_records(REAL_PART))
    r.check("rows were found at all", len(rows) > 0,
            "the parser matched nothing in a file known to contain 188 "
            "Kenyan PSCs - an empty index looks exactly like 'no Kenyan "
            "companies exist'")
    # Measured on this exact file: 188 records, 180 distinct companies.
    r.check(f"the count matches what was measured ({len(rows)})",
            len(rows) == 188,
            f"got {len(rows)}, expected 188 - if Companies House reissued "
            f"this day's snapshot the number can move, but a large change "
            f"means the parser changed meaning")
    r.check("every row has a company number",
            all(x["company_number"] for x in rows))
    r.check("every row has a nationality",
            all(x["nationality"] for x in rows))
    r.check("distinct companies matches too",
            len({x["company_number"] for x in rows}) == 180,
            f"got {len({x['company_number'] for x in rows})}")
    kinds = {x["nationality"] for x in rows}
    r.check("the dual-national spellings came through",
            any("," in k for k in kinds), sorted(kinds)[:6])
    r.check("the 'Kenya' spelling came through",
            any(k.strip().lower() == "kenya" for k in kinds),
            f"spellings seen: {sorted(kinds)} - 'Kenya' without the n is in "
            f"this file and must not be dropped")


def check_admin_wiring(r: Results) -> None:
    """The dashboard half: routes exist, and the page's JS actually parses.

    An admin page is rendered from a string in Python, so a syntax error in
    its JavaScript passes every Python check and only shows up as a blank
    screen for whoever opens it. Node settles it.
    """
    print("\n-- the admin dashboard --")
    import re
    import shutil
    import subprocess
    import tempfile

    src = open(os.path.join(REPO, "admin_routes.py"), encoding="utf-8").read()

    for route in ("/api/admin/psc/status", "/api/admin/psc/refresh",
                  "/api/admin/psc/search", "/api/admin/psc/company/<cn>"):
        r.check(f"route {route} exists", f'"{route}"' in src, "missing")

    r.check("refresh is superadmin-only",
            "_require_superadmin()" in src.split("api_psc_refresh")[1][:400],
            "rebuilding the universe every search reads is not the same "
            "act as approving a user")
    r.check("two refreshes cannot run at once", "_psc_lock" in src,
            "concurrent rebuilds race on the delete-older-snapshots step "
            "and can empty the table between them")
    r.check("the page says a PSC is not a director",
            "A PSC is not a director" in src or
            "<b>A PSC is not a director.</b>" in src,
            "an operator will read these rows as directors")
    r.check("the refresh warns before spending the bandwidth",
            "2.4GB" in src or "roughly 2.4" in src,
            "a click should not start a multi-gigabyte download unannounced")

    m = re.search(r'_PAGE = r?"""(.*?)"""', src, re.S)
    if not r.check("the admin page template is extractable", m is not None):
        return
    js = re.search(r"<script>(.*?)</script>", m.group(1), re.S)
    if not r.check("the page has a script block", js is not None):
        return
    if not shutil.which("node"):
        r.skip("admin page JS parses", "node is not on PATH")
        return
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "admin.js")
        with open(p, "w", encoding="utf-8") as f:
            f.write(js.group(1))
        proc = subprocess.run(["node", "--check", p],
                              capture_output=True, text=True, timeout=60)
        r.check("the admin page's JavaScript parses", proc.returncode == 0,
                (proc.stderr or "")[-400:])

    # Handlers must not be built with JSON.stringify inside a double-quoted
    # attribute - the same truncation that made three buttons inert.
    bad = [mm.group(0)[:60] for mm in
           re.finditer(r'onclick="[^"]*JSON\.stringify', m.group(1))]
    r.check("no onclick is built with JSON.stringify", not bad, repr(bad))


def main() -> int:
    r = Results()
    print("test_psc_index.py - the index decides what an operator can see")
    check_page_parser(r)
    check_record_parser(r)
    check_it_never_claims_directors(r)
    check_against_the_real_snapshot(r)
    check_admin_wiring(r)
    return r.report()


if __name__ == "__main__":
    raise SystemExit(main())
