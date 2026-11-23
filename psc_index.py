"""An offline index of UK companies with a Kenyan person of significant control.

WHY THIS EXISTS. Companies House cannot be searched by nationality. Its
advanced company search takes thirteen parameters - name, status, type,
dissolution and incorporation dates, location, SIC codes, size, paging -
and not one of them touches officers. Nationality appears only on
/company/{n}/officers and /officers/{id}/appointments, both of which need
you to already know who you are asking about. So the dashboard could only
search by COMPANY NAME and then spend one officers request per candidate
to find out whether it qualified, 20 at a time, against a 600-per-5-minutes
budget. Searching "kimani" examined 8 companies out of 25 matches.

The batch path guessed Kenyan SURNAMES instead, which is a different kind
of wrong: of the real Kenyan PSCs measured below, "Charlene Pinto", "Anita
Jahenda" and "Salome Wanganga" would never have matched a surname list.

WHAT THIS USES INSTEAD. Companies House publishes a free daily snapshot of
every Person with Significant Control, as JSON lines, in 33 parts. Measured
on part 1 of 33 of the snapshot of 2026-11-22:

    total records                 500,000
    individual PSCs               461,710
    carrying a nationality        461,706   (100.0%)
    Kenyan                            188   across 180 distinct companies
    records with no company_number      0

Every record carries both `nationality` and a top-level `company_number`,
which is the whole trick: the snapshot is an index from nationality to
company, and it costs no API requests at all.

A PSC IS NOT A DIRECTOR, AND THIS MODULE NEVER PRETENDS OTHERWISE. A PSC
holds more than 25% of shares or voting rights. In a small company that is
usually also a director, but it is not the same fact and the pipeline's
admission rule is about directors. So this index is a CANDIDATE LIST: it
narrows roughly 5.7 million companies to a few thousand worth asking about,
and the director rule is then confirmed per company through the officers
endpoint, which is the only place that fact actually lives. Columns and UI
copy say "PSC" for that reason.

PRESENT DATA ONLY. Each ingest tags its rows with the snapshot date and
then deletes every row from an older one, so the table is always a picture
of the register as it stands rather than an accumulating history.

WHAT IS DELIBERATELY NOT STORED. Date of birth. Companies House publishes
the month and year, so it would be lawful to copy, but nothing here needs
it: the director check goes to the officers endpoint by company number. A
field that is not needed is not collected.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import urllib.request

import directors

SNAPSHOT_PAGE = "http://download.companieshouse.gov.uk/en_pscdata.html"
DOWNLOAD_BASE = "http://download.companieshouse.gov.uk/"
TABLE = "psc_candidates"
INDIVIDUAL = "individual-person-with-significant-control"


# ── Pure: what the download page offers ────────────────────────────────────

def snapshot_parts(html: str) -> tuple[str, list[str]]:
    """(snapshot_date, [part filenames]) from the download page.

    The page lists a single whole-snapshot file AND the same data split into
    33 parts. The parts are used: one is ~82MB zipped against ~2.4GB for the
    whole thing, so a failure costs one part rather than the lot, and the
    peak disk is one part at a time.

    Returns ("", []) rather than guessing when the page cannot be parsed.
    A date is never invented: it names which day's register the table holds.
    """
    names = sorted(set(re.findall(
        r"psc-snapshot-(\d{4}-\d{2}-\d{2})_(\d+)of(\d+)\.zip", html or "")))
    if not names:
        return "", []
    date = names[0][0]
    total = names[0][2]
    # Only parts from the newest date on the page, in numeric order.
    date = max(d for d, _i, _t in names)
    parts = sorted((int(i), f"psc-snapshot-{date}_{i}of{t}.zip")
                   for d, i, t in names if d == date)
    return date, [p for _i, p in parts]


# ── Pure: what one line of the snapshot means ──────────────────────────────

def parse_record(line: str, forms=None) -> dict | None:
    """One matching individual PSC as a row, or None.

    None covers four different non-matches - blank line, unparseable JSON,
    a corporate PSC, a nationality that is not the one asked for - because
    the caller does the same thing with all of them. Counting them apart is
    the ingest's job, not this function's.
    """
    forms = forms or directors.KENYAN_FORMS
    line = (line or "").strip()
    if not line:
        return None
    try:
        rec = json.loads(line)
    except Exception:
        return None
    data = rec.get("data") or {}
    if data.get("kind") != INDIVIDUAL:
        return None
    cn = str(rec.get("company_number") or "").strip()
    if not cn:
        return None
    raw = data.get("nationality")
    # Reuse the pipeline's own parser so the index and the admission rule
    # agree about what a nationality is: split on commas only, because
    # 'South African' is one value containing a space.
    if not (set(forms) & set(directors.nationalities({"nationality": raw}))):
        return None
    noc = data.get("natures_of_control") or []
    ne = data.get("name_elements") or {}
    dob = data.get("date_of_birth") or {}
    ivd = data.get("identity_verification_details") or {}
    return {
        "company_number": cn,
        "etag": str(data.get("etag") or ""),
        "psc_name": str(data.get("name") or "").strip(),
        "nationality": str(raw or "").strip(),
        "country_of_residence": str(data.get("country_of_residence") or "").strip(),
        "natures_of_control": "; ".join(str(n) for n in noc),
        "notified_on": str(data.get("notified_on") or ""),
        # A ceased PSC is not a person with significant control any more.
        # 18.6% of all individual records carry this, and 16.5% of the
        # Kenyan ones. Kept, because the record is kept whole and this is
        # real history, but it is what makes a row not current.
        "ceased_on": str(data.get("ceased_on") or ""),
        # Month and year only - Companies House publishes no day, so this
        # is the whole of it. Left as None when absent (14 records in part
        # 1 have none): a year of 0 would filter and sort as a real date.
        "dob_month": _int_or_none(dob.get("month")),
        "dob_year": _int_or_none(dob.get("year")),
        "title": str(ne.get("title") or "").strip(),
        "forename": str(ne.get("forename") or "").strip(),
        "middle_name": str(ne.get("middle_name") or "").strip(),
        "surname": str(ne.get("surname") or "").strip(),
        "address": _format_address(data.get("address") or {}),
        "identity_verified_on": str(ivd.get("identity_verified_on") or ""),
        # The source record, verbatim. Columns exist for what is queried;
        # this is so that a field nobody has needed yet - links, the rest
        # of identity_verification_details, anything Companies House adds
        # later - is not lost and cannot only be recovered by downloading
        # 2.4GB again.
        "raw": data,
    }


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# Observed across all 461,710 individual records in part 1, in the order
# they make sense on an envelope.
_ADDRESS_ORDER = ("care_of", "po_box", "premises", "address_line_1",
                  "address_line_2", "locality", "region", "postal_code",
                  "country")


def _format_address(addr: dict) -> str:
    return ", ".join(str(addr[k]).strip() for k in _ADDRESS_ORDER
                     if str(addr.get(k) or "").strip())


# ── Schema ─────────────────────────────────────────────────────────────────

def ensure_schema() -> None:
    import db
    with db._cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
              company_number       TEXT NOT NULL,
              etag                 TEXT NOT NULL DEFAULT '',
              psc_name             TEXT,
              nationality          TEXT,
              country_of_residence TEXT,
              natures_of_control   TEXT,
              notified_on          TEXT,
              snapshot_date        TEXT NOT NULL,
              company_name         TEXT,
              company_status       TEXT,
              director_verdict     TEXT,
              updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
              PRIMARY KEY (company_number, etag)
            )""")
        # Added after the table first shipped, so ADD COLUMN IF NOT EXISTS
        # rather than relying on CREATE TABLE - which does nothing when the
        # table is already there, leaving a live table missing every column
        # named in a later SELECT. Same reconciliation db.ensure_schema
        # does, for the same reason.
        for col, coltype in (("ceased_on", "TEXT"),
                             ("dob_month", "INTEGER"),
                             ("dob_year", "INTEGER"),
                             ("title", "TEXT"),
                             ("forename", "TEXT"),
                             ("middle_name", "TEXT"),
                             ("surname", "TEXT"),
                             ("address", "TEXT"),
                             ("identity_verified_on", "TEXT"),
                             ("raw", "JSONB")):
            cur.execute(f"ALTER TABLE {TABLE} "
                        f"ADD COLUMN IF NOT EXISTS {col} {coltype}")
        cur.execute(f"CREATE INDEX IF NOT EXISTS {TABLE}_nat_idx "
                    f"ON {TABLE} (nationality)")
        cur.execute(f"CREATE INDEX IF NOT EXISTS {TABLE}_snap_idx "
                    f"ON {TABLE} (snapshot_date)")
        cur.execute(f"CREATE INDEX IF NOT EXISTS {TABLE}_ceased_idx "
                    f"ON {TABLE} (ceased_on)")


# ── Ingest ─────────────────────────────────────────────────────────────────

def _http_get(url: str, dest: str) -> None:
    with urllib.request.urlopen(url, timeout=300) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)


def iter_part_records(zip_path: str, forms=None):
    """Stream one .zip part, yielding matching rows.

    `unzip -p` streams the member to stdout, so a 479MB part is never held
    in memory or written out decompressed. Python's zipfile would do the
    same, but unzip is already a dependency of the deploy and is faster
    here by enough to matter across 33 parts.
    """
    proc = subprocess.Popen(["unzip", "-p", zip_path], stdout=subprocess.PIPE)
    assert proc.stdout is not None
    try:
        for raw in proc.stdout:
            row = parse_record(raw.decode("utf-8", "replace"), forms)
            if row:
                yield row
    finally:
        proc.stdout.close()
        proc.wait()


def ingest(forms=None, on_progress=None, max_parts: int | None = None,
           dry_run: bool = False, fetch=None, page_html: str | None = None
           ) -> dict:
    """Rebuild the index from the current snapshot. Returns a summary.

    Streams part by part and deletes each download immediately, so peak
    disk is one part (~82MB) rather than the ~2.4GB of the whole snapshot.

    `fetch` and `page_html` are injection points for the tests, which run
    the real parser over a real part file without going near the network.
    """
    forms = forms or directors.KENYAN_FORMS
    fetch = fetch or _http_get

    def say(msg):
        if on_progress:
            on_progress(msg)
        print(f"  [psc] {msg}")

    if page_html is None:
        with urllib.request.urlopen(SNAPSHOT_PAGE, timeout=120) as r:
            page_html = r.read().decode("utf-8", "replace")
    date, parts = snapshot_parts(page_html)
    if not parts:
        return {"status": "error",
                "error": "no snapshot parts found on the download page; "
                         "Companies House may have changed its layout"}
    if max_parts:
        parts = parts[:max_parts]

    say(f"snapshot {date}: {len(parts)} parts")
    rows: list[dict] = []
    for i, name in enumerate(parts, 1):
        tmp = os.path.join(tempfile.gettempdir(), name)
        try:
            fetch(DOWNLOAD_BASE + name, tmp)
            found = list(iter_part_records(tmp, forms))
            rows.extend(found)
            say(f"part {i}/{len(parts)}: {len(found)} matched "
                f"({len(rows)} so far)")
        finally:
            # Deleted whether the part parsed or not. 33 parts left behind
            # is 2.4GB on a box whose disk the pipeline also writes to.
            if os.path.exists(tmp):
                os.remove(tmp)

    companies = {r["company_number"] for r in rows}
    summary = {"status": "ok", "snapshot_date": date, "parts": len(parts),
               "rows": len(rows), "companies": len(companies)}
    if dry_run:
        summary["dry_run"] = True
        summary["sample"] = rows[:10]
        return summary

    written, removed = _replace(date, rows)
    summary["written"] = written
    summary["removed_stale"] = removed
    say(f"wrote {written} rows for {len(companies)} companies; "
        f"removed {removed} from older snapshots")
    return summary


def _replace(date: str, rows: list[dict]) -> tuple[int, int]:
    """Write this snapshot's rows, then drop every older snapshot's.

    In one transaction: a half-applied refresh would leave the table a mix
    of two days, which is exactly the "present data only" promise broken.
    The delete runs AFTER the insert so a failed download never empties a
    working index.
    """
    import psycopg2.extras

    import db
    ensure_schema()
    # One statement per BATCH, not per row. The database is Supabase, which
    # is across a network: a few thousand sequential INSERTs is a few
    # thousand round-trips, and the write would take longer than the 2.4GB
    # download that produced the rows. execute_values sends them in pages.
    cols = ("company_number", "etag", "psc_name", "nationality",
            "country_of_residence", "natures_of_control", "notified_on",
            "ceased_on", "dob_month", "dob_year", "title", "forename",
            "middle_name", "surname", "address", "identity_verified_on",
            "raw", "snapshot_date")
    values = [
        (r["company_number"], r["etag"], r["psc_name"], r["nationality"],
         r["country_of_residence"], r["natures_of_control"], r["notified_on"],
         r["ceased_on"], r["dob_month"], r["dob_year"], r["title"],
         r["forename"], r["middle_name"], r["surname"], r["address"],
         r["identity_verified_on"], psycopg2.extras.Json(r["raw"]), date)
        for r in rows
    ]
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols
                        if c not in ("company_number", "etag"))
    with db._cursor() as cur:
        if values:
            psycopg2.extras.execute_values(
                cur,
                f"INSERT INTO {TABLE} ({', '.join(cols)}) VALUES %s "
                f"ON CONFLICT (company_number, etag) DO UPDATE SET "
                f"{updates}, updated_at = now()",
                values, page_size=500)
        cur.execute(f"DELETE FROM {TABLE} WHERE snapshot_date <> %s", (date,))
        removed = cur.rowcount or 0
    return len(rows), removed


# ── Reading it back ────────────────────────────────────────────────────────

def status() -> dict:
    """What the index currently holds. Safe to call before any ingest."""
    import db
    try:
        ensure_schema()
        with db._cursor() as cur:
            # Current and ceased counted apart. One number covering both
            # would overstate how many companies are actually controlled
            # by a Kenyan person today by about a sixth.
            cur.execute(f"""
                SELECT count(*) AS n,
                       count(DISTINCT company_number) AS c,
                       max(snapshot_date) AS d,
                       count(*) FILTER (
                         WHERE ceased_on IS NOT NULL AND ceased_on <> ''
                       ) AS ceased,
                       count(DISTINCT company_number) FILTER (
                         WHERE ceased_on IS NULL OR ceased_on = ''
                       ) AS current_companies
                FROM {TABLE}""")
            row = cur.fetchone() or {}
        ceased = row.get("ceased") or 0
        total = row.get("n") or 0
        return {"rows": total, "companies": row.get("c") or 0,
                "snapshot_date": row.get("d") or "",
                "ceased": ceased, "current": total - ceased,
                "current_companies": row.get("current_companies") or 0}
    except Exception as e:
        return {"error": str(e)}


def search(q: str = "", limit: int = 50, offset: int = 0,
           include_ceased: bool = False) -> list[dict]:
    """Rows from the index. `q` matches a company number or a PSC name.

    CEASED PSCs ARE EXCLUDED BY DEFAULT. 18.6% of all individual records
    carry ceased_on, and 16.5% of the Kenyan ones: those people stopped
    being persons of significant control, sometimes years ago. The index
    answers "who controls this company now", so showing them as current
    would be wrong in the only way that matters. They stay in the table -
    the record is kept whole and this is real history - and
    include_ceased brings them back when the history is the question.
    """
    import db
    ensure_schema()
    sql = (f"SELECT company_number, psc_name, nationality, "
           f"country_of_residence, natures_of_control, notified_on, "
           f"ceased_on, dob_month, dob_year, title, forename, middle_name, "
           f"surname, address, identity_verified_on, "
           f"company_name, company_status, director_verdict, snapshot_date "
           f"FROM {TABLE}")
    where: list[str] = []
    args: list = []
    q = (q or "").strip()
    if q:
        where.append("(company_number ILIKE %s OR psc_name ILIKE %s)")
        args += [f"%{q}%", f"%{q}%"]
    if not include_ceased:
        where.append("(ceased_on IS NULL OR ceased_on = '')")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY company_number LIMIT %s OFFSET %s"
    args += [max(1, min(int(limit), 500)), max(0, int(offset))]
    with db._cursor() as cur:
        cur.execute(sql, args)
        return [dict(r) for r in cur.fetchall()]


def record_company_facts(cn: str, company_name: str = "",
                         company_status: str = "",
                         director_verdict: str = "") -> None:
    """Cache what the API said about a company, so a list can show names.

    The snapshot carries only the company NUMBER - the name belongs to the
    company record, not the PSC record - so a list of numbers is useless
    until something fills this in. Filled lazily, for the rows actually
    looked at, rather than by 5,900 requests at ingest time.
    """
    import db
    ensure_schema()
    sets, args = [], []
    for col, val in (("company_name", company_name),
                     ("company_status", company_status),
                     ("director_verdict", director_verdict)):
        if val:
            sets.append(f"{col} = %s")
            args.append(val)
    if not sets:
        return
    args.append(cn)
    with db._cursor() as cur:
        cur.execute(f"UPDATE {TABLE} SET {', '.join(sets)}, updated_at = now() "
                    f"WHERE company_number = %s", args)
