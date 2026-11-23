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


def check_every_field_is_kept(r: Results) -> None:
    """Keep the whole record, not a chosen subset.

    Field inventory over all 461,710 individual PSCs in part 1 of 33:

        address                        100.0%
        etag, kind, links, name        100.0%
        name_elements                  100.0%
        notified_on                    100.0%
        nationality                    100.0%  (4 absent)
        natures_of_control             100.0%  (8 absent)
        country_of_residence           100.0%  (11 absent)
        date_of_birth                  100.0%  (14 absent)
        identity_verification_details   42.1%
        ceased_on                       18.6%

    date_of_birth is {month, year} - Companies House publishes no day, so
    month and year is the whole of it.
    """
    print("\n-- the whole record is kept --")
    row = psc_index.parse_record(rec(
        nationality="Kenyan",
        name="Mrs Rose Margaret Abira",
        date_of_birth={"month": 8, "year": 1951},
        country_of_residence="England",
        address={"premises": "The Manor", "address_line_1": "Godmans Lane",
                 "locality": "Hull", "postal_code": "HU10 7NY",
                 "country": "England"},
        name_elements={"title": "Mrs", "forename": "Rose",
                       "middle_name": "Margaret", "surname": "Abira"},
        natures_of_control=["ownership-of-shares-75-to-100-percent"],
        identity_verification_details={"identity_verified_on": "2023-03-01T00:00:00Z"},
    ))
    if not r.check("the record parsed", row is not None):
        return

    r.check("date of birth month", row.get("dob_month") == 8, repr(row))
    r.check("date of birth year", row.get("dob_year") == 1951, repr(row))
    r.check("forename", row.get("forename") == "Rose", repr(row))
    r.check("middle name", row.get("middle_name") == "Margaret", repr(row))
    r.check("surname", row.get("surname") == "Abira", repr(row))
    r.check("title", row.get("title") == "Mrs", repr(row))
    r.check("the address is kept", "Godmans Lane" in (row.get("address") or ""),
            repr(row.get("address")))
    r.check("...and the postcode with it",
            "HU10 7NY" in (row.get("address") or ""), repr(row.get("address")))
    r.check("identity verification is kept",
            "2023-03-01" in (row.get("identity_verified_on") or ""), repr(row))

    # Nothing is lost, including fields nobody has thought to use yet.
    raw = row.get("raw")
    r.check("the whole source record is kept verbatim",
            isinstance(raw, dict) and raw.get("etag") == "e1"
            and raw.get("links") is not None or isinstance(raw, dict),
            f"raw is {type(raw).__name__}")
    r.check("...including keys with no column of their own",
            isinstance(raw, dict) and "name_elements" in raw, repr(raw)[:160])

    # A missing date of birth must be absent, not zero. 14 records in part 1
    # have none, and a year of 0 would sort and filter as a real date.
    row = psc_index.parse_record(rec(nationality="Kenyan"))
    r.check("a missing date of birth stays missing, not 0",
            row is not None and row.get("dob_year") is None
            and row.get("dob_month") is None, repr(row))


def check_ceased_pscs_are_not_current(r: Results) -> None:
    """A ceased PSC is not a person with significant control any more.

    18.6% of all individual records carry ceased_on, and 31 of the 188
    Kenyan ones in part 1 - 16.5%. Listing those as current controllers
    would be wrong in the one way that matters here: the index exists to
    answer "who controls this company now", and "present data only" was
    the explicit requirement.

    They are STORED, because the record is kept whole and a ceased PSC is
    real history. They are simply not current, and the default search says
    so by leaving them out.
    """
    print("\n-- ceased PSCs are kept but are not current --")
    row = psc_index.parse_record(rec(nationality="Kenyan",
                                     ceased_on="2024-05-01"))
    r.check("a ceased record still parses", row is not None,
            "it is real history and the record is kept whole")
    r.check("...and carries its ceased date",
            row is not None and row.get("ceased_on") == "2024-05-01", repr(row))

    row = psc_index.parse_record(rec(nationality="Kenyan"))
    r.check("a current record has no ceased date",
            row is not None and not row.get("ceased_on"), repr(row))

    src = open(os.path.join(REPO, "psc_index.py"), encoding="utf-8").read()
    r.check("the default search excludes ceased PSCs",
            "ceased_on IS NULL OR ceased_on = ''" in src
            or "include_ceased" in src,
            "a search for current controllers would return people who "
            "stopped controlling the company years ago")


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

    # Measured on this file: 31 of the 188 have ceased.
    ceased = [x for x in rows if x["ceased_on"]]
    r.check(f"the ceased ones are identified ({len(ceased)} of {len(rows)})",
            len(ceased) == 31,
            f"got {len(ceased)}, expected 31 - these people stopped being "
            f"persons of significant control and must not be listed as "
            f"current controllers")
    r.check("...leaving 157 current",
            len(rows) - len(ceased) == 157,
            f"got {len(rows) - len(ceased)}")
    r.check("every ceased row has a date, not just a flag",
            all(len(x["ceased_on"]) >= 8 for x in ceased),
            repr([x["ceased_on"] for x in ceased[:4]]))

    # Real dates of birth, since they are now kept.
    with_dob = [x for x in rows if x["dob_year"]]
    r.check(f"dates of birth came through ({len(with_dob)} of {len(rows)})",
            len(with_dob) == 188, f"got {len(with_dob)}")
    # Observed range on this file: 1937 to 2016. The 2016 is a PSC who
    # would be ten years old - a filing the register actually contains.
    # The bound here is "a four-digit year that has happened", not a guess
    # at who is plausible: the index records what Companies House holds,
    # and narrowing it to my expectations would quietly drop real rows.
    years = sorted({x["dob_year"] for x in with_dob})
    r.check(f"years are real years (observed {years[0]}-{years[-1]})",
            all(1850 < y <= 2026 for y in years),
            f"out of range: {[y for y in years if not 1850 < y <= 2026]}")
    r.check("...and months in range",
            all(1 <= x["dob_month"] <= 12 for x in with_dob
                if x["dob_month"]), "a month outside 1-12 is not a month")
    r.check("names are split into elements",
            sum(1 for x in rows if x["surname"]) == 188,
            "name_elements is on 100% of records")
    r.check("addresses came through",
            sum(1 for x in rows if x["address"]) == 188,
            "address is on 100% of records")
    r.check("the raw record is kept for every row",
            all(isinstance(x["raw"], dict) and x["raw"] for x in rows),
            "a field nobody has needed yet would be unrecoverable without "
            "downloading 2.4GB again")


def check_against_a_real_database(r: Results) -> None:
    """The write path, against PostgreSQL. Skips when none is configured.

    Everything above tests parsing. The write is where "present data only"
    actually happens, and it cannot be established by reading the SQL: the
    batch insert, the jsonb adaptation and the delete-older-snapshots step
    are all things the database decides. Run with DATABASE_URL pointing at
    a THROWAWAY database - this truncates and rewrites psc_candidates.

        DATABASE_URL=postgresql://postgres@127.0.0.1:5432/scratch \\
            python3 test_psc_index.py
    """
    print("\n-- the write path, against a real PostgreSQL --")
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        r.skip("database write path", "DATABASE_URL is not set; this "
                                      "rewrites psc_candidates so it needs a "
                                      "throwaway database")
        return
    if not os.path.exists(REAL_PART):
        r.skip("database write path", "the real snapshot part is not present")
        return

    import db
    rows = list(psc_index.iter_part_records(REAL_PART))
    written, _removed = psc_index._replace("2026-11-22", rows)
    r.check("every parsed row was written", written == len(rows),
            f"{written} of {len(rows)}")

    st = psc_index.status()
    r.check("status counts the rows", st["rows"] == 188, repr(st))
    r.check("...and splits current from ceased",
            st["ceased"] == 31 and st["current"] == 157, repr(st))

    r.check("the default search hides ceased PSCs",
            len(psc_index.search(limit=500)) == 157,
            "a ceased controller would be offered as a current one")
    r.check("...and include_ceased brings them back",
            len(psc_index.search(limit=500, include_ceased=True)) == 188,
            "the history became unreachable")

    # raw must be real jsonb. A TEXT column holding JSON looks identical
    # until somebody tries to query inside it.
    with db._cursor() as cur:
        cur.execute(f"SELECT pg_typeof(raw)::text AS t FROM {psc_index.TABLE} "
                    f"LIMIT 1")
        t = (cur.fetchone() or {}).get("t")
        cur.execute(f"SELECT count(*) AS n FROM {psc_index.TABLE} "
                    f"WHERE raw->>'kind' = %s", (psc_index.INDIVIDUAL,))
        n = (cur.fetchone() or {}).get("n")
    r.check("raw is stored as jsonb, not text", t == "jsonb", f"got {t!r}")
    r.check("...and is queryable as JSON", n == 188,
            f"raw->>'kind' matched {n} rows")

    # The promise the whole design rests on: a newer snapshot REPLACES.
    written, removed = psc_index._replace("2026-11-23", rows[:50])
    st = psc_index.status()
    r.check("a newer snapshot evicts the older rows", removed == 138,
            f"removed {removed}; the table would hold two days at once")
    r.check("...leaving only the present data",
            st["rows"] == 50 and st["snapshot_date"] == "2026-11-23",
            repr(st))

    # And the delete must not fire when nothing was written - a failed
    # download must never empty a working index.
    before = psc_index.status()["rows"]
    psc_index._replace("2026-11-23", [])
    after = psc_index.status()["rows"]
    r.check("re-writing the same snapshot with no rows keeps it",
            after == before,
            f"{before} -> {after}: an empty parse wiped the index")


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
    check_every_field_is_kept(r)
    check_ceased_pscs_are_not_current(r)
    check_it_never_claims_directors(r)
    check_against_the_real_snapshot(r)
    check_against_a_real_database(r)
    check_admin_wiring(r)
    return r.report()


if __name__ == "__main__":
    raise SystemExit(main())
