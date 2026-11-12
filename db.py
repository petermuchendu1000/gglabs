"""
Supabase (PostgreSQL) data-access layer — the single source of truth for the
company pipeline. Replaces the old Excel-backed store.

All rows are exchanged as dicts keyed by the SAME human header labels the rest
of the codebase already uses (e.g. "Company Number", "Company Name",
"Date of Creation"), so callers don't need to know about SQL columns.

Connection string is read from the environment (DATABASE_URL, falling back to
SUPABASE_DB_URL). Never hard-code credentials — put them in .env (git-ignored).

Public API:
    ensure_schema()                       -> create the companies table if absent
    all_rows()                            -> list[dict] keyed by header labels
    find_row(cn)                          -> dict | None
    add_company(cn, name, date="")        -> bool (True if inserted)
    update_row(cn, {header: value, ...})  -> None (upserts the company if new)
    save_results(results)                 -> int (upsert nested pipeline results)
    import_from_excel(path)               -> int (one-time migration helper)
    company_numbers()                     -> set[str]
"""
from __future__ import annotations

import contextlib
import logging
import os
import threading
import time

import psycopg2
import psycopg2.errors
import psycopg2.extras
import psycopg2.pool

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


# ── Column mapping ──────────────────────────────────────────────────────────
# (human header label, snake_case db column). Order defines display/export order.
COLUMNS: list[tuple[str, str]] = [
    ("No.",                     "row_no"),
    ("Company Number",          "company_number"),
    ("Company Name",            "company_name"),
    ("Short Name",              "short_name"),
    ("Status",                  "status"),
    ("Type",                    "type"),
    ("Date of Creation",        "date_of_creation"),
    ("SIC Codes",               "sic_codes"),
    ("Address",                 "address"),
    ("Directors",               "directors"),
    ("Director Nationalities",  "director_nationalities"),
    ("Director DOB",            "director_dob"),
    ("Director Gender",         "director_gender"),
    ("Buvei First Name",        "buvei_first_name"),
    ("Buvei Last Name",         "buvei_last_name"),
    ("DUNS Number",             "duns_number"),
    ("DUNS Status",             "duns_status"),
    ("DUNS Email Used",         "duns_email_used"),
    ("D&B Legal Name",          "dnb_legal_name"),
    ("D&B Address",             "dnb_address"),
    ("Certificate Downloaded",  "certificate_downloaded"),
    ("Certificate Path",        "certificate_path"),
    ("Domain",                  "domain"),
    ("Domain Status",           "domain_status"),
    ("Domain Cost",             "domain_cost"),
    ("Assigned Email",          "assigned_email"),
    ("Account Name",            "account_name"),
    ("Developer Email",         "developer_email"),
    ("Dev Email Forwarding",    "dev_email_forwarding"),
    ("Google TXT Status",       "google_txt_status"),
    ("Organization Phone",      "organization_phone"),
    ("Play Signup Missing",     "play_signup_missing"),
    ("Company Status",          "pipeline_status"),
    ("Company Notes",           "company_notes"),
    ("Archived",                "archived"),
]
HEADER_TO_COL = {h: c for h, c in COLUMNS}
COL_TO_HEADER = {c: h for h, c in COLUMNS}
TABLE = "companies"


# ── Connection ──────────────────────────────────────────────────────────────
def _dsn() -> str:
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL not set. Add it to .env "
            "(postgresql://...pooler.supabase.com:5432/postgres)."
        )
    return dsn


def _connect():
    """Open a brand-new connection.

    Only for seeding the pool and for the exhaustion fallback in _cursor().
    Everything else goes through _cursor().
    """
    return psycopg2.connect(_dsn(), connect_timeout=15,
                            cursor_factory=psycopg2.extras.RealDictCursor)


# ── Connection pool ─────────────────────────────────────────────────────────
# Every database call used to open a fresh connection. Measured on loopback,
# with no TLS and no network in the way, a connect costs 2.57 ms against
# 0.134 ms for a query on an already-open connection - 19x. Against a remote
# Supabase, where a connect also pays TCP, TLS and auth round trips, the
# connect dominates the request rather than the query does.
#
# _POOL_MAX must exceed the number of threads that can touch the database at
# once. gunicorn runs threads = 8 (gunicorn.conf.py) and each pipeline stage
# runs in its own daemon thread (api_v2.py:173), which is not a bounded set.
# Exhaustion therefore must NOT become an error: _cursor() falls back to a
# direct connection, which is precisely the pre-pool behaviour.
_POOL_MAX = int(os.getenv("DB_POOL_MAX", "16"))
_POOL = None
_POOL_LOCK = threading.Lock()


def _pool():
    """Build the pool on first use, so importing this module never requires
    DATABASE_URL to be set."""
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                # On failure _POOL stays None, so the next call retries
                # rather than caching a broken pool.
                _POOL = psycopg2.pool.ThreadedConnectionPool(
                    1, _POOL_MAX, dsn=_dsn(), connect_timeout=15,
                    cursor_factory=psycopg2.extras.RealDictCursor,
                )
    return _POOL


@contextlib.contextmanager
def _cursor():
    """Yield a cursor on a pooled connection.

    Commits on a clean exit, rolls back on an exception, and ALWAYS hands the
    connection back - including when the caller returns from inside the
    with-block, which add_company does.

    Never use `with _connect() as conn` for a pooled connection: psycopg2's
    connection context manager commits but does not release, so the pool
    would drain after _POOL_MAX calls and never recover. The commit matters
    for pooling too - without it a connection returns to the pool
    idle-in-transaction, holding its snapshot open.
    """
    pooled = True
    try:
        conn = _pool().getconn()
    except psycopg2.pool.PoolError:
        pooled = False          # exhausted: degrade, never fail
        conn = _connect()
    try:
        with conn.cursor() as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if pooled:
            _pool().putconn(conn)
        else:
            conn.close()


# ── Value normalisation ─────────────────────────────────────────────────────
def _cn8(v) -> str:
    s = str(v or "").strip()
    return s.zfill(8) if s.isdigit() else s


def _norm(value):
    """Coerce a Python value into what we store (text, or None)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "Yes" if value else ""
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value).strip()
    return s if s != "" else None


def _derive_developer_email(set_cols: dict) -> None:
    """Fill developer_email from domain, in place.

    Developer Email is dev@ the company's own domain - the same formula
    play_console_readiness.developer_email_for_domain uses - so it is
    derived once here rather than at each of the places that write a
    domain: pipeline.py:251 (the match stage), run_pipeline.py:688 (after
    a purchase), wizard_routes.py:248 and :576, and an Excel import. Only
    one of those ever set it, so a company whose domain the match stage
    assigned carried no developer address at all until somebody saved the
    Play Console form by hand.

    A value already present always wins, so save_play_console's own
    developer_email and a spreadsheet's own column are untouched. Where
    only the domain is given, the derived address is correct by
    construction: dev@ the PREVIOUS domain does not exist any more.

    Values here have been through _norm, so "" is already None and a blank
    domain derives nothing.
    """
    domain = set_cols.get("domain")
    if domain and not set_cols.get("developer_email"):
        set_cols["developer_email"] = "dev@" + str(domain).strip().lower()


# ── Schema ──────────────────────────────────────────────────────────────────
def ensure_schema() -> None:
    cols_sql = []
    for header, col in COLUMNS:
        if col == "company_number":
            cols_sql.append(f"{col} TEXT PRIMARY KEY")
        elif col == "row_no":
            cols_sql.append(f"{col} INTEGER")
        else:
            cols_sql.append(f"{col} TEXT")
    ddl = (
        f"CREATE TABLE IF NOT EXISTS {TABLE} (\n  "
        + ",\n  ".join(cols_sql)
        + ",\n  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()\n);"
    )
    with _cursor() as cur:
        cur.execute(ddl)
        # CREATE TABLE IF NOT EXISTS does nothing when the table is already
        # there, so a column added to COLUMNS later would be absent from a
        # live table and every SELECT naming it would fail. Reconcile the
        # columns idempotently instead. ADD COLUMN IF NOT EXISTS on a
        # nullable column with no default is metadata-only in Postgres: no
        # table rewrite, so this stays cheap as the table grows.
        for header, col in COLUMNS:
            if col == "company_number":
                continue
            coltype = "INTEGER" if col == "row_no" else "TEXT"
            cur.execute(
                f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS {col} {coltype}"
            )
    _invalidate()


# ── Read cache (short TTL; invalidated on writes) ───────────────────────────
_CACHE: dict = {"ts": 0.0, "rows": None}
_LOCK = threading.Lock()
_TTL = 1.5  # seconds


def _invalidate() -> None:
    with _LOCK:
        _CACHE["rows"] = None
        _CACHE["ts"] = 0.0


def _row_to_headerdict(dbrow: dict) -> dict:
    out = {}
    for header, col in COLUMNS:
        if col == "row_no":
            out[header] = dbrow.get(col)
        else:
            out[header] = dbrow.get(col)
    return out


def all_rows(use_cache: bool = True) -> list[dict]:
    with _LOCK:
        if use_cache and _CACHE["rows"] is not None and (time.time() - _CACHE["ts"]) < _TTL:
            return _CACHE["rows"]
    cols = ", ".join(col for _, col in COLUMNS)
    with _cursor() as cur:
        cur.execute(
            f"SELECT {cols} FROM {TABLE} "
            f"ORDER BY row_no NULLS LAST, company_number"
        )
        rows = [_row_to_headerdict(r) for r in cur.fetchall()]
    with _LOCK:
        _CACHE["rows"] = rows
        _CACHE["ts"] = time.time()
    return rows


def find_row(cn: str) -> dict | None:
    """Fetch one company.

    This used to call all_rows() and scan the result in Python, so looking up
    a single company issued a full-table SELECT of all 34 columns and then
    compared every row. company_number is the TEXT PRIMARY KEY
    (ensure_schema), so a direct lookup is an index hit instead.

    The scan is kept as a FALLBACK, not as the primary path: it normalised
    BOTH sides with _cn8, so it matched a row stored unpadded ('1234567')
    against a padded query ('01234567'). Legacy rows imported from the
    spreadsheet can be stored that way, and dropping the fallback would turn
    those into a silent "company not found".
    """
    target = _cn8(cn)
    if not target:
        return None
    cols = ", ".join(col for _, col in COLUMNS)
    with _cursor() as cur:
        cur.execute(
            f"SELECT {cols} FROM {TABLE} WHERE company_number = %s", (target,)
        )
        row = cur.fetchone()
    if row:
        return _row_to_headerdict(row)
    for r in all_rows():
        if _cn8(r.get("Company Number")) == target:
            return r
    return None


def company_numbers() -> set[str]:
    return {_cn8(r.get("Company Number")) for r in all_rows() if r.get("Company Number")}


def _next_row_no(cur) -> int:
    cur.execute(f"SELECT COALESCE(MAX(row_no), 0) AS m FROM {TABLE}")
    return int(cur.fetchone()["m"]) + 1


# ── Writes ──────────────────────────────────────────────────────────────────
def add_company(cn: str, name: str = "", date_of_creation: str = "") -> bool:
    """Insert a new company. Returns True if inserted, False if it already exists."""
    cn = _cn8(cn)
    if not cn or cn == "00000000":
        return False
    with _cursor() as cur:
        cur.execute(f"SELECT 1 FROM {TABLE} WHERE company_number = %s", (cn,))
        if cur.fetchone():
            return False
        rn = _next_row_no(cur)
        cur.execute(
            f"INSERT INTO {TABLE} (company_number, company_name, date_of_creation, row_no) "
            f"VALUES (%s, %s, %s, %s) ON CONFLICT (company_number) DO NOTHING",
            (cn, _norm(name), _norm(date_of_creation), rn),
        )
    _invalidate()
    return True


def update_row(cn: str, updates: dict) -> None:
    """Update columns for a company (keys are header labels). Upserts if new."""
    cn = _cn8(cn)
    if not cn:
        return
    set_cols = {}
    for header, value in updates.items():
        col = HEADER_TO_COL.get(header)
        if col is None or col == "company_number":
            continue
        set_cols[col] = _norm(value)

    _derive_developer_email(set_cols)

    if not set_cols:
        return
    # One statement instead of two, and atomic with it. This used to SELECT
    # to find out whether the row existed and then INSERT or UPDATE: two
    # round trips for every write, on top of whatever lookup the caller had
    # already done. The INSERT also had no ON CONFLICT clause, so a
    # concurrent insert landing between the SELECT and the INSERT raised a
    # duplicate-key error.
    cols = ["company_number"] + list(set_cols.keys())
    exprs = ["%s"] * len(cols)
    params = [cn] + list(set_cols.values())
    if "row_no" not in set_cols:
        # Position is assigned only when the row is created, and row_no is
        # deliberately absent from the DO UPDATE list below, so an existing
        # row keeps its place.
        cols.append("row_no")
        exprs.append(f"COALESCE((SELECT MAX(row_no) FROM {TABLE}), 0) + 1")

    assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in set_cols)
    with _cursor() as cur:
        cur.execute(
            f"INSERT INTO {TABLE} ({', '.join(cols)}) "
            f"VALUES ({', '.join(exprs)}) "
            f"ON CONFLICT (company_number) DO UPDATE "
            f"SET {assignments}, updated_at = now()",
            params,
        )
    _invalidate()


# ── Domain purchase claim (money safety) ────────────────────────────────────
# Buying a domain is a billed, irreversible side effect. The record of it must
# exist BEFORE the money moves, otherwise a lost write looks identical to
# "never bought" and the next run buys a second domain.
DOMAIN_CLAIM = "purchasing"

# Audit channel for anything that can spend money. Deliberately a named
# logger rather than module-level, so it can be routed or kept when the rest
# of the application logging is turned down.
_money_log = logging.getLogger("money")


def claim_domain_purchase(cn: str) -> bool:
    """Atomically claim the exclusive right to buy a domain for `cn`.

    Sets domain_status = 'purchasing', but ONLY where no claim is already
    outstanding, in one statement. Doing this as a read-then-write would
    leave a window in which two attempts both see "no claim" and both go on
    to a billed purchase; a single conditional UPDATE cannot.

    Returns True when the claim is ours and the caller may spend money.

    False means one of:
      - another attempt holds the claim, or
      - an earlier attempt was never resolved, so a domain may already have
        been bought and charged while its outcome went unrecorded, or
      - the company has no row, so there is nowhere to record the purchase.
    In every case the correct response is to refuse to buy, not to retry.
    """
    cn = _cn8(cn)
    if not cn:
        return False
    # One statement, so there is no window between checking and claiming.
    # INSERT covers run_pipeline's batch path, which reaches the domain stage
    # (run_pipeline.py:1154) before it ever persists the row - its only db
    # writes are db.save_results at run_pipeline.py:706 and :802, both
    # outside the per-company loop. row_no is assigned here so a
    # batch-created company still sorts correctly in all_rows().
    with _cursor() as cur:
        cur.execute(
            f"INSERT INTO {TABLE} (company_number, row_no, domain_status) "
            f"VALUES (%s, COALESCE((SELECT MAX(row_no) FROM {TABLE}), 0) + 1, %s) "
            f"ON CONFLICT (company_number) DO UPDATE "
            f"   SET domain_status = %s, updated_at = now() "
            f" WHERE {TABLE}.domain_status IS NULL "
            f"    OR {TABLE}.domain_status <> %s",
            (cn, DOMAIN_CLAIM, DOMAIN_CLAIM, DOMAIN_CLAIM),
        )
        claimed = cur.rowcount == 1
    _invalidate()
    if claimed:
        _money_log.info("domain purchase claim TAKEN cn=%s", cn)
    else:
        _money_log.warning(
            "domain purchase claim REFUSED cn=%s: an earlier attempt is "
            "unresolved (domain_status=%s); a domain may already have been "
            "bought and charged", cn, DOMAIN_CLAIM)
    return claimed


# A claim nothing can resolve is released after this long, so one lost order
# cannot strand a company for ever. The number of days is the operator's
# policy choice and is passed in by the caller, not decided here.
DOMAIN_CLAIM_EXPIRED = "claim_expired"


def release_expired_claim(cn: str, max_age_days: int) -> bool:
    """Release a purchase claim that has sat untouched for max_age_days.

    ONE conditional statement, for the same reason claim_domain_purchase is:
    a read-then-write would let two attempts both decide the claim had expired
    and both go on to a billed purchase.

    Age is measured from updated_at, which is a LOWER BOUND on the claim's own
    age - claim_domain_purchase sets it when it takes the claim (:436) and
    every later write moves it forward (:387). So this can only ever release
    later than max_age_days after the claim was taken, never earlier, which is
    the safe direction to be wrong in.

    The caller must FIRST have established from the registrar that no domain
    for this company exists. This statement cannot check that and does not try.
    """
    cn = _cn8(cn)
    if not cn or not isinstance(max_age_days, int) or max_age_days < 1:
        return False
    with _cursor() as cur:
        cur.execute(
            f"UPDATE {TABLE} "
            f"   SET domain_status = %s, updated_at = now() "
            f" WHERE company_number = %s "
            f"   AND domain_status = %s "
            f"   AND updated_at < now() - make_interval(days => %s)",
            (DOMAIN_CLAIM_EXPIRED, cn, DOMAIN_CLAIM, max_age_days),
        )
        released = cur.rowcount == 1
    _invalidate()
    if released:
        _money_log.warning(
            "domain purchase claim EXPIRED cn=%s: released after %d days with "
            "no domain for this company in the Namecheap account, so the next "
            "run may buy one. If an order from before then is still pending, "
            "this is where a second charge would come from.", cn, max_age_days)
    return released


def domain_claim_held(cn: str) -> bool:
    """True if an unresolved purchase claim is outstanding for `cn`.

    Reads past the row cache deliberately: the caller is deciding whether
    money was already spent, which must not be answered from a snapshot that
    may be up to _TTL seconds old.
    """
    cn = _cn8(cn)
    if not cn:
        return False
    with _cursor() as cur:
        cur.execute(
            f"SELECT domain_status FROM {TABLE} WHERE company_number = %s",
            (cn,),
        )
        row = cur.fetchone()
    return bool(row) and row.get("domain_status") == DOMAIN_CLAIM


# Map a nested pipeline result dict (as produced by run_pipeline) to header labels.
def _result_to_headers(r: dict) -> dict:
    d   = r.get("details", {}) or {}
    dn  = r.get("duns", {}) or {}
    ce  = r.get("certificate", {}) or {}
    dm  = r.get("domain", {}) or {}
    em  = r.get("email", {}) or {}
    pc  = r.get("play_console", {}) or {}
    ph  = r.get("phone", {}) or {}
    cert_ok = (ce.get("status") == "downloaded") or bool(ce.get("path"))
    out = {
        "Company Number":         r.get("company_number", ""),
        "Company Name":           d.get("company_name", ""),
        "Status":                 d.get("company_status", ""),
        "Type":                   d.get("company_type", ""),
        "Date of Creation":       d.get("date_of_creation", ""),
        "SIC Codes":              d.get("sic_codes", ""),
        "Address":                d.get("address", ""),
        "Directors":              d.get("director_names", ""),
        "Director Nationalities": d.get("director_nationalities", ""),
        "DUNS Number":            dn.get("duns_number", ""),
        "DUNS Status":            dn.get("status", ""),
        "DUNS Email Used":        dn.get("temp_email", ""),
        "D&B Legal Name":         dn.get("dnb_name", ""),
        "D&B Address":            dn.get("dnb_address", ""),
        "Certificate Downloaded": "Yes" if cert_ok else "",
        "Certificate Path":       ce.get("path", "") or ce.get("error", ""),
        "Domain":                 dm.get("domain", ""),
        "Domain Status":          dm.get("status", ""),
        "Domain Cost":            dm.get("charged", ""),
        "Assigned Email":         em.get("email", ""),
        "Account Name":           em.get("account_name", ""),
        "Developer Email":        pc.get("developer_email", ""),
        "Dev Email Forwarding":   (pc.get("developer_email_forwarding", {}) or {}).get("status", ""),
        "Google TXT Status":      (pc.get("google_txt", {}) or {}).get("status", ""),
        "Organization Phone":     ph.get("phone_number", ""),
    }

    # get_company_details now carries the exact company-row columns
    # Companies House can fill (run_pipeline.company_row_updates): the
    # Kenyan director only, plus the DOB and gender none of the flat keys
    # above ever held. Preferring it means a full pipeline run writes the
    # same thing an add writes - without it, save_results would put every
    # active director back over the one the add chose. Absent on a result
    # rebuilt by _headers_to_result, where the flat keys above stand.
    out.update(d.get("row_updates") or {})
    return out


def _headers_to_result(row: dict) -> dict:
    """Inverse of _result_to_headers: header-keyed row -> nested pipeline result."""
    g = lambda h: row.get(h) or ""
    cert_dl = str(g("Certificate Downloaded")).strip().lower() in ("yes", "true", "1")
    return {
        "company_number": str(g("Company Number")),
        "details": {
            "company_name":           g("Company Name"),
            "company_status":         g("Status"),
            "company_type":           g("Type"),
            "date_of_creation":       g("Date of Creation"),
            "sic_codes":              g("SIC Codes"),
            "address":                g("Address"),
            "director_names":         g("Directors"),
            "director_nationalities": g("Director Nationalities"),
        },
        "duns": {
            "duns_number": g("DUNS Number"),
            "status":      g("DUNS Status"),
            "temp_email":  g("DUNS Email Used"),
            "dnb_name":    g("D&B Legal Name"),
            "dnb_address": g("D&B Address"),
        },
        "certificate": {
            "path":   g("Certificate Path") if cert_dl else "",
            "status": "downloaded" if cert_dl else "",
            "error":  g("Certificate Path") if not cert_dl else "",
        },
        "domain":  {"domain": g("Domain"), "status": g("Domain Status"), "charged": g("Domain Cost")},
        "email":   {"email": g("Assigned Email"), "account_name": g("Account Name")},
        "play_console": {
            "developer_email": g("Developer Email"),
            "developer_email_forwarding": {"status": g("Dev Email Forwarding")},
            "google_txt": {"status": g("Google TXT Status")},
        },
        "phone": {"phone_number": g("Organization Phone")},
    }


def all_results() -> list[dict]:
    """All companies as nested pipeline-result dicts (used by run_pipeline)."""
    return [_headers_to_result(r) for r in all_rows(use_cache=False) if r.get("Company Number")]


def save_results(results: list[dict]) -> int:
    """Upsert a list of nested pipeline result dicts. Returns count processed."""
    n = 0
    for r in results:
        cn = _cn8(r.get("company_number", ""))
        if not cn or cn == "00000000":
            continue
        headers = {k: v for k, v in _result_to_headers(r).items() if v not in (None, "")}
        headers.pop("Company Number", None)
        update_row(cn, headers)
        n += 1
    return n


# ── One-time migration from the old Excel file ──────────────────────────────
# Columns where a wrong value costs money rather than just being untidy.
# domain_status carries the purchase claim that stops a second billed
# namecheap.domains.create; Domain is what api_v2._compute_stages reads to
# decide the domain stage is finished; duns_number gates the DUNS stage.
MONEY_COLUMNS = ("domain", "domain_status", "domain_cost", "duns_number")

# Presentation only. The sheet's "No." column renumbers rows, so almost
# every company differs on it, and counting those alongside real changes
# would bury the handful that involve money under one line each.
ORDERING_COLUMNS = ("row_no",)


def _excel_records(path: str):
    """Yield (company_number, {column: value}) exactly as import_from_excel
    would write them.

    Shared by import_from_excel and diff_from_excel deliberately. A diff
    that reimplemented this parse would drift from the import, and a diff
    that disagrees with the operation it previews is worse than no diff:
    it is read immediately before a production write, so it would give
    false assurance.
    """
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        ws = wb.active
        it = ws.iter_rows(values_only=True)
        hdr_row = next(it, None)
        if not hdr_row:
            return
        hdr = [str(v or "").strip() for v in hdr_row]
        for row in it:
            # Drop wholly blank rows only. This used to be a POSITIONAL test
            # - `not row[1]` - which assumed Company Number is the second
            # column. Everything else in this parser is header-driven, and
            # the real company-number filter is six lines below, so the
            # positional test added nothing on a canonically ordered sheet
            # and silently dropped every row of a reordered one: with
            # Company Number moved, row[1] became some other column, and a
            # company with that column empty vanished from the import
            # without a word. Operator-supplied workbooks now arrive through
            # /api/import/preview, so column order is no longer ours to
            # assume. Behaviour on a canonical sheet is unchanged: a row with
            # no company number is still skipped, just by the check below.
            if not row or not any(v is not None and str(v).strip() != ""
                                  for v in row):
                continue
            rec = {hdr[i]: (row[i] if i < len(row) else None)
                   for i in range(len(hdr))}
            cn = _cn8(rec.get("Company Number"))
            if not cn or cn == "00000000":
                continue
            set_cols = {}
            for header, col in COLUMNS:
                if col == "company_number":
                    continue
                if header not in rec:
                    continue
                value = _norm(rec.get(header))
                if value is None:
                    # An import must never reduce information. A blank cell
                    # means "the spreadsheet does not know this", not "clear
                    # it". _norm maps "" to None, so without this skip the
                    # EXCLUDED assignments below wrote NULL over live values.
                    #
                    # That was not merely lossy, it was a money hazard:
                    # domain_status carries the purchase claim (DOMAIN_CLAIM)
                    # that stops a second billed namecheap.domains.create,
                    # and the Domain column is what api_v2._compute_stages
                    # reads to decide the domain stage is finished. Nulling
                    # either one lets run-all buy a second domain. Measured
                    # against a stale sheet with blank domain cells: the
                    # claim became None and a fresh claim then succeeded.
                    #
                    # save_results already filters empties this way; this
                    # function did not.
                    continue
                set_cols[col] = value
            # A sheet that brings a domain brings the developer address
            # with it. Derived here rather than in import_from_excel so
            # diff_from_excel, which reads the same generator, SHOWS it in
            # the preview instead of writing a field the operator never
            # saw. A Developer Email column in the sheet wins.
            _derive_developer_email(set_cols)
            yield cn, set_cols
    finally:
        wb.close()


def diff_from_excel(path: str) -> dict:
    """Preview what import_from_excel(path) would change. Writes nothing.

    Skipping blank cells stopped the import erasing live data, but it did
    not make the import harmless: a POPULATED but stale cell still
    overwrites a populated column, because that is what an import is for.
    Only a human can tell stale from authoritative, so this reports the
    field-level consequences and leaves the judgement where it belongs.

    Both sides are passed through _norm before comparison, so a value that
    differs only by surrounding whitespace is not reported as a change.

    Returns:
        new_companies  - company numbers the sheet would create
        additions      - count of fields filled where Postgres was empty
        overwrites     - count of populated fields the sheet would replace
        changes        - {cn: [{column, header, current, incoming, kind}]},
                         kind being "add" or "overwrite"; unchanged fields
                         are counted in `unchanged`, not listed
        unchanged      - count of fields the sheet matches already
        money_overwrites - the subset of overwrites touching MONEY_COLUMNS
        claims_at_risk - companies whose outstanding purchase claim
                         (DOMAIN_CLAIM) the sheet would overwrite. Treat a
                         non-empty list here as a stop sign: it means a
                         domain may have been bought and charged, and the
                         import would discard the record of it.
    """
    current = {}
    for row in all_rows():
        cn = _cn8(row.get("Company Number"))
        if cn:
            current[cn] = row

    out = {"new_companies": [], "additions": 0, "overwrites": 0,
           "changes": {}, "unchanged": 0, "money_overwrites": [],
           "claims_at_risk": [], "ordering_changes": 0}

    for cn, set_cols in _excel_records(path):
        existing = current.get(cn)
        if existing is None:
            out["new_companies"].append(cn)
            out["additions"] += sum(1 for c in set_cols
                                    if c not in ORDERING_COLUMNS)
            continue
        for col, incoming in set_cols.items():
            was = _norm(existing.get(COL_TO_HEADER[col]))
            if was == incoming:
                out["unchanged"] += 1
                continue
            entry = {"column": col, "header": COL_TO_HEADER[col],
                     "current": was, "incoming": incoming,
                     "kind": "add" if was is None else "overwrite"}
            # Recorded in `changes` whatever it is, so this stays a faithful
            # preview of what the import writes - the import does write
            # row_no. Only the headline counters treat it as noise.
            out["changes"].setdefault(cn, []).append(entry)
            if col in ORDERING_COLUMNS:
                out["ordering_changes"] += 1
                continue
            if was is None:
                out["additions"] += 1
                continue
            out["overwrites"] += 1
            if col in MONEY_COLUMNS:
                out["money_overwrites"].append({"company_number": cn, **entry})
            if col == "domain_status" and was == DOMAIN_CLAIM:
                out["claims_at_risk"].append(cn)

    out["new_companies"].sort()
    out["claims_at_risk"].sort()
    return out


def import_from_excel(path: str) -> int:
    imported = 0
    with _cursor() as cur:
        for cn, set_cols in _excel_records(path):
            cols = ["company_number"] + list(set_cols.keys())
            vals = [cn] + list(set_cols.values())
            placeholders = ", ".join(["%s"] * len(cols))
            updates = ", ".join(f"{c} = EXCLUDED.{c}"
                                for c in cols if c != "company_number")
            # A row carrying nothing but a company number still creates the
            # company, but must not generate "DO UPDATE SET " with an empty
            # assignment list, which is a syntax error.
            conflict = f"DO UPDATE SET {updates}" if updates else "DO NOTHING"
            cur.execute(
                f"INSERT INTO {TABLE} ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT (company_number) {conflict}",
                vals,
            )
            imported += 1
    _invalidate()
    return imported


if __name__ == "__main__":
    import sys

    _cmd = sys.argv[1] if len(sys.argv) > 1 else ""

    # --diff and --verify must not touch the schema. ensure_schema() issues
    # ALTER TABLE ... ADD COLUMN IF NOT EXISTS, so running it first would
    # mean a command documented as read-only could change the database, and
    # --verify in particular could not report the state it was asked about
    # because it would have just repaired it.
    _READ_ONLY_CMDS = ("--diff", "--verify")
    if _cmd not in _READ_ONLY_CMDS:
        ensure_schema()
        print(f"Schema ready. Table '{TABLE}' has {len(COLUMNS)} mapped columns.")

    if _cmd == "--verify":
        # Read-only. Answers the questions that cannot be checked anywhere
        # but against the live database.
        print(f"\nVerifying '{TABLE}' against db.COLUMNS ({len(COLUMNS)} "
              f"mapped columns). Nothing is written.\n")
        with _cursor() as cur:
            cur.execute(
                "select column_name, data_type from information_schema.columns "
                "where table_schema = 'public' and table_name = %s",
                (TABLE,),
            )
            live = {r["column_name"]: r["data_type"] for r in cur.fetchall()}

        if not live:
            print(f"  TABLE '{TABLE}' DOES NOT EXIST in the public schema.")
            print("  Run `python db.py` with no arguments to create it.")
            sys.exit(1)

        expected = [c for _, c in COLUMNS]
        missing = [c for c in expected if c not in live]
        extra = [c for c in live if c not in expected and c != "updated_at"]

        print(f"  columns live        : {len(live)}")
        print(f"  columns expected    : {len(expected)} (+ updated_at)")
        print(f"  missing from live   : {missing or 'none'}")
        print(f"  present but unmapped: {extra or 'none'}")
        if missing:
            print("\n  Missing columns mean every SELECT naming them fails.")
            print("  `python db.py` with no arguments adds them "
                  "(ADD COLUMN IF NOT EXISTS, metadata-only).")

        # Each query is guarded on the column existing. Querying domain_status
        # unconditionally would crash on exactly the schema this command is
        # meant to report on.
        with _cursor() as cur:
            cur.execute(f"select count(*) as n from {TABLE}")
            total = cur.fetchone()["n"]
            claims = None
            if "domain_status" in live:
                cur.execute(
                    f"select company_number from {TABLE} "
                    f"where domain_status = %s order by company_number",
                    (DOMAIN_CLAIM,),
                )
                claims = [r["company_number"] for r in cur.fetchall()]
            with_domain = None
            if "domain" in live:
                cur.execute(
                    f"select count(*) as n from {TABLE} "
                    f"where domain is not null and domain <> ''"
                )
                with_domain = cur.fetchone()["n"]

        print(f"\n  companies           : {total}")
        print(f"  with a domain       : "
              f"{with_domain if with_domain is not None else 'n/a (column missing)'}")
        if claims is None:
            print("  outstanding claims  : n/a (domain_status column missing)")
        elif claims:
            # Printed last, and loudly: each one may represent a domain that
            # was bought and charged without its outcome being recorded.
            print(f"\n  OUTSTANDING PURCHASE CLAIMS ('{DOMAIN_CLAIM}'): "
                  f"{len(claims)}")
            for cn in claims:
                print(f"    {cn}")
            print("  Each of these may already have been bought and charged. "
                  "Reconcile against\n  the Namecheap account before running "
                  "the domain stage for them again.")
        else:
            print("  outstanding claims  : none")
        print()
        # Non-zero when the schema does not match, so this is usable as a
        # gate in a deploy script rather than something a human must read.
        sys.exit(1 if missing else 0)
    DEFAULT_SHEET = "pipeline_output/companies_pipeline.xlsx"

    if len(sys.argv) > 1 and sys.argv[1] == "--diff":
        # Read-only. Run this before --import, every time.
        path = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_SHEET
        try:
            d = diff_from_excel(path)
        except psycopg2.errors.UndefinedColumn as e:
            # This command deliberately does not run ensure_schema, so a
            # table older than db.COLUMNS fails here instead of being
            # silently altered by a "read-only" command. Say so plainly
            # rather than printing a traceback.
            print(f"\nThe database is missing a column this code expects: {e}"
                  .rstrip())
            print("Run `python db.py --verify` to see which, then "
                  "`python db.py` with no arguments to add them.")
            sys.exit(1)
        print(f"\nDiff of {path} against the database (nothing written):")
        print(f"  companies the sheet would create : {len(d['new_companies'])}")
        print(f"  fields it would fill in          : {d['additions']}")
        print(f"  populated fields it would REPLACE: {d['overwrites']}")
        print(f"  fields already matching          : {d['unchanged']}")
        print(f"  row renumbering ('No.' only)     : "
              f"{d['ordering_changes']} (cosmetic)")
        if d["new_companies"]:
            print(f"\n  new: {', '.join(d['new_companies'][:20])}"
                  + (" ..." if len(d["new_companies"]) > 20 else ""))
        if d["overwrites"]:
            print("\n  Overwrites, by company. Each line replaces a value "
                  "that is already set:")
            for cn, entries in sorted(d["changes"].items()):
                over = [e for e in entries if e["kind"] == "overwrite"
                        and e["column"] not in ORDERING_COLUMNS]
                if not over:
                    continue
                print(f"    {cn}")
                for e in over:
                    flag = "  <-- MONEY" if e["column"] in MONEY_COLUMNS else ""
                    print(f"      {e['header']}: {e['current']!r} -> "
                          f"{e['incoming']!r}{flag}")
        if d["claims_at_risk"]:
            # Deliberately last, so it is the final thing on screen.
            print(f"\n  STOP. {len(d['claims_at_risk'])} company(ies) hold an "
                  f"outstanding purchase claim ('{DOMAIN_CLAIM}') that this "
                  f"sheet would overwrite:")
            for cn in d["claims_at_risk"]:
                print(f"    {cn}")
            print("  A domain may already have been bought and charged for "
                  "these. Importing discards the record of it and allows a "
                  "second purchase. Reconcile against Namecheap first.")
        print()

    if len(sys.argv) > 1 and sys.argv[1] == "--import":
        path = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_SHEET
        n = import_from_excel(path)
        print(f"Imported {n} rows from {path}.")
    print(f"Total rows in DB: {len(all_rows(use_cache=False))}")
