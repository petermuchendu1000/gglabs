"""
Per-company stage control API — v2.
Register with:  from api_v2 import api_v2; app.register_blueprint(api_v2)
"""
from __future__ import annotations

import io, json, os, re, shutil, sys, threading, time, traceback

import requests
from requests.adapters import HTTPAdapter
from datetime import datetime
from pathlib import Path

import openpyxl
from flask import Blueprint, jsonify, request, send_file, abort

import db  # Supabase (PostgreSQL) data-access layer — source of truth
import directors  # Companies House officer parsing, shared with run_pipeline
from safe_path import resolve_within

api_v2 = Blueprint("api_v2", __name__)

ROOT     = Path(__file__).parent
EXCEL    = ROOT / "pipeline_output" / "companies_pipeline.xlsx"
COMP_DIR = ROOT / "pipeline_output" / "companies"
DL_BASE  = ROOT / "pipeline_output"          # files must be under here to download

# Ensure the companies table exists on startup (idempotent).
try:
    db.ensure_schema()
except Exception as _e:
    print(f"  [db] ensure_schema at import failed: {_e}")

STAGE_ORDER = ["details", "duns", "certificate", "domain", "email", "director_id", "app"]

# UI field key -> spreadsheet/DB header label. Whitelist of manually-editable fields.
# Editing any of these overrides the value the pipeline would otherwise produce;
# because stage completion + the info panels are all derived from the company row,
# a manual edit propagates across the whole interface on the next fetch.
EDITABLE_FIELDS = {
    "name":                 "Company Name",
    "status":               "Status",
    "type":                 "Type",
    "date_of_creation":     "Date of Creation",
    "sic":                  "SIC Codes",
    "address":              "Address",
    "directors":            "Directors",
    "nationalities":        "Director Nationalities",
    "dobs":                 "Director DOB",
    "genders":              "Director Gender",
    "buvei_first":          "Buvei First Name",
    "buvei_last":           "Buvei Last Name",
    "duns":                 "DUNS Number",
    "duns_status":          "DUNS Status",
    "duns_email":           "DUNS Email Used",
    "dnb_name":             "D&B Legal Name",
    "dnb_address":          "D&B Address",
    "domain":               "Domain",
    "domain_status":        "Domain Status",
    "domain_cost":          "Domain Cost",
    "email":                "Assigned Email",
    "account_name":         "Account Name",
    "developer_email":      "Developer Email",
    "dev_email_forwarding": "Dev Email Forwarding",
    "google_txt_status":    "Google TXT Status",
    "organization_phone":   "Organization Phone",
    "certificate_path":     "Certificate Path",
    "certificate_downloaded": "Certificate Downloaded",
}

# ── Job state ──────────────────────────────────────────────────────────────
_JOBS: dict[str, dict] = {}   # key: "{cn8}:{stage}"
_LOCK = threading.Lock()
_CO_LOCKS: dict[str, threading.Lock] = {}   # per-company serialisation


def _co_lock(cn: str) -> threading.Lock:
    with _LOCK:
        if cn not in _CO_LOCKS:
            _CO_LOCKS[cn] = threading.Lock()
        return _CO_LOCKS[cn]


def _jk(cn: str, stage: str) -> str:
    return f"{cn}:{stage}"


def _job_get(cn: str, stage: str) -> dict:
    with _LOCK:
        j = dict(_JOBS.get(_jk(cn, stage), {}))
    if j.get("status") == "running":
        live = _TEE.get_log(_jk(cn, stage))
        if live:
            j["log"] = live
    return j


def _job_set(cn: str, stage: str, **kw) -> None:
    with _LOCK:
        _JOBS.setdefault(_jk(cn, stage), {}).update(kw)


# ── Stdout capture ─────────────────────────────────────────────────────────
# Single global _TEE is installed at sys.stdout once. Each worker thread calls
# _TEE.bind(key) so its prints are routed to its own buffer; no global
# sys.stdout replacement, no race condition between concurrent jobs.

_TEE_LOCK = threading.Lock()


class _Tee:
    """Global stdout multiplexer — installed once at module load."""

    def __init__(self):
        self._real = sys.__stdout__
        self._local = threading.local()
        self._logs: dict[str, list[str]] = {}  # key → chunks

    def bind(self, key: str) -> None:
        self._local.key = key
        with _TEE_LOCK:
            self._logs[key] = []

    def get_log(self, key: str) -> str:
        with _TEE_LOCK:
            return "".join(self._logs.get(key, []))

    def unbind(self) -> str:
        key = getattr(self._local, "key", None)
        log = ""
        if key:
            with _TEE_LOCK:
                log = "".join(self._logs.pop(key, []))
            self._local.key = None
        return log

    def write(self, s: str) -> int:
        if s:
            try: self._real.write(s); self._real.flush()
            except Exception: pass
            key = getattr(self._local, "key", None)
            if key:
                with _TEE_LOCK:
                    if key in self._logs:
                        self._logs[key].append(s)
        return len(s) if s else 0

    def flush(self):
        try: self._real.flush()
        except Exception: pass


_TEE = _Tee()
sys.stdout = _TEE


# A stage can fail by RETURNING a status instead of raising, and the money
# path does exactly that on purpose. register_company_domain reports a refused
# order as {"status": "error"} (run_pipeline.py:738) and an order whose outcome
# is UNKNOWN - possibly charged, claim deliberately kept - as
# {"status": "unresolved"} (run_pipeline.py:773); get_duns_number reports an
# exhausted retry as {"status": "error"} (run_pipeline.py:599).
#
# Only exceptions used to mark a job failed, so every one of those was recorded
# status="done": a green tick and no Error Log button for a domain that was
# never bought, with the explanation left unread in the job's `result`.
_FAILED_RESULT_STATUSES = {"error", "unresolved", "failed"}


def _result_failure(result) -> str | None:
    """The error message if a stage reported failure by return value, else None.

    Keyed on the status VALUE, never on the presence of an "error" key:
    download_certificate returns {"path": ..., "error": None} on success
    (run_pipeline.py:912), and a success must not read as a failure.
    """
    if not isinstance(result, dict):
        return None
    if str(result.get("status") or "") not in _FAILED_RESULT_STATUSES:
        return None
    return (str(result.get("error") or "")
            or f"stage reported status={result.get('status')!r} without an error message")


def _spawn(cn: str, stage: str, fn) -> None:
    """Run fn() in a daemon thread, tracking job state and capturing stdout."""
    key = _jk(cn, stage)
    _job_set(cn, stage, status="running", started_at=datetime.now().isoformat(),
             finished_at=None, error=None, log=None)

    def _worker():
        _TEE.bind(key)
        try:
            result = fn()
            log = _TEE.unbind()
            failure = _result_failure(result)
            _job_set(cn, stage,
                     status="failed" if failure else "done",
                     finished_at=datetime.now().isoformat(),
                     error=failure,
                     log=log, result=str(result)[:500])
        except Exception as e:
            log = _TEE.unbind()
            _job_set(cn, stage, status="failed",
                     finished_at=datetime.now().isoformat(),
                     error=str(e),
                     log=log + "\n" + traceback.format_exc())

    threading.Thread(target=_worker, daemon=True).start()


# ── Excel helpers ──────────────────────────────────────────────────────────

def _cn8(v) -> str:
    return str(v or "").strip().zfill(8)


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "", name).strip()


def _co_dir(cn: str, name: str = "") -> Path:
    folder = f"{_cn8(cn)} - {_safe(name)}" if name else _cn8(cn)
    return COMP_DIR / folder


_ROWS_CACHE = None  # retained for backwards-compat; caching now lives in db.py


class DatabaseUnavailable(Exception):
    """The row store could not be reached.

    Deliberately NOT the same as "no such company". _all_rows used to answer
    an outage with [] and _find_row with None, so a dashboard with a dead
    database showed "No companies found" and every company answered 404 "Not
    found". Both are statements about the DATA, and neither was true: nothing
    was known either way.

    pipeline.py:180 carries the same warning for the Namecheap listing - an
    error response has no Domain elements, so an unchecked failure reads
    exactly like an account with nothing in it.
    """


def _all_rows() -> list[dict]:
    """All company rows from Supabase, keyed by the original header labels."""
    try:
        return db.all_rows()
    except Exception as e:
        raise DatabaseUnavailable(str(e)) from e


def _find_row(cn: str) -> dict | None:
    """The company's row, or None if there is genuinely no such company."""
    try:
        return db.find_row(cn)
    except Exception as e:
        raise DatabaseUnavailable(str(e)) from e


@api_v2.errorhandler(DatabaseUnavailable)
def _database_unavailable(e):
    # One handler rather than a try/except at nineteen call sites. A stage
    # running in a worker thread does not come through here: the job runner
    # catches it and the job reads failed, which is the same answer in the
    # place that answers it.
    return jsonify({
        "error": f"the database could not be reached: {e}",
        "hint": "this is not a statement about the data - nothing could be "
                "read either way",
    }), 503


def _update_excel_row(cn: str, updates: dict) -> None:
    """Update a company's columns in Supabase (name kept for call-site compat).

    RAISES on failure, deliberately. This used to catch everything, print
    "[db] update_row failed" and return None, which meant:

      - five routes answered 200 for a write that never happened, and the
        dashboard toasted "Saved N field(s)" over it. An operator clearing a
        stuck purchase claim by hand was told it had worked when it had not;
      - a stage could finish "done" without its result recorded - the email
        assigned from the pool, the certificate downloaded, the DUNS number
        found, none of it written.

    Same silent-success family as a refused domain order reading as done. A
    write that did not happen must not look like one that did.
    """
    db.update_row(cn, updates)


def _save_or_explain(cn: str, updates: dict):
    """Write a row on behalf of a ROUTE. None on success, else a JSON error.

    Routes answer a person, so they turn the failure into something the
    dashboard can show, rather than letting a 500 reach the UI as
    "HTTP 500" with the reason only in the server log.
    """
    try:
        _update_excel_row(cn, updates)
    except Exception as e:
        return jsonify({
            "error": f"the change was NOT saved: {e}",
            "cn": cn,
            "attempted": sorted(updates),
        }), 500
    return None


def _add_company_row(cn: str, name: str, date_of_creation: str = "") -> None:
    """Insert a new company into Supabase if it isn't there yet."""
    try:
        db.add_company(cn, name, date_of_creation)
    except Exception as e:
        print(f"  [db] add_company failed: {e}")


# ── Stage status ───────────────────────────────────────────────────────────

def _compute_stages(cn: str, row: dict) -> dict:
    name  = str(row.get("Company Name") or "")
    cdir  = _co_dir(cn, name)

    has_cert = bool(row.get("Certificate Downloaded"))
    if not has_cert:
        cd = cdir / "certificate"
        has_cert = cd.is_dir() and any(f.endswith("_cert.pdf") for f in os.listdir(cd))

    ad = cdir / "app" / "artifacts"
    has_app = ad.is_dir() and any(f.endswith((".apk", ".aab")) for f in os.listdir(ad))

    return {
        "details":     bool(name),
        "duns":        bool(row.get("DUNS Number")),
        "certificate": has_cert,
        "domain":      bool(row.get("Domain")),
        "email":       bool(row.get("Assigned Email")),
        "director_id": (cdir / "director_id" / "ID Front.jpeg").exists(),
        "app":         has_app,
    }


# ── File helpers ───────────────────────────────────────────────────────────

def _ls(d: Path, exts: list | None = None, names: list | None = None) -> list[dict]:
    if not d.is_dir(): return []
    out = []
    for f in sorted(d.rglob("*")):
        if not f.is_file(): continue
        rel = f.relative_to(d).as_posix()
        if exts and not any(rel.lower().endswith(e) for e in exts): continue
        if names and f.name not in names: continue
        out.append({"name": rel, "size": f.stat().st_size, "path": str(f)})
    return out


def _pp(cdir: Path) -> dict | None:
    for p in [cdir / "payment_profile" / "payment_profile.txt", cdir / "payment_profile.txt"]:
        if p.exists():
            return {"name": "payment_profile.txt", "size": p.stat().st_size, "path": str(p)}
    return None


def _version_file(cn: str, name: str) -> Path:
    return _co_dir(cn, name) / "app" / "version.json"


def _read_version(cn: str, name: str) -> dict:
    vf = _version_file(cn, name)
    if vf.exists():
        try:
            return json.loads(vf.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"version_code": 1, "version_name": "1.0"}


def _write_version(cn: str, name: str, version_code: int, version_name: str) -> None:
    vf = _version_file(cn, name)
    vf.parent.mkdir(parents=True, exist_ok=True)
    vf.write_text(
        json.dumps({"version_code": version_code, "version_name": version_name}, indent=2),
        encoding="utf-8",
    )


def _read_manifest(cn: str, name: str) -> dict:
    """Read the app's manifest.json; return {} if not found."""
    f = _co_dir(cn, name) / "app" / "manifest.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _pc_file(cn: str, name: str) -> Path:
    return _co_dir(cn, name) / "app" / "play_console.json"


def _read_pc(cn: str, name: str, application_id: str = "", manifest: dict | None = None,
             assigned_email: str = "", domain: str = "") -> dict:
    """Return saved Play Console data, pre-filled with low-review-risk defaults."""
    if manifest is None:
        manifest = {}

    role_noun        = str(manifest.get("role_noun") or "staff")
    role_verb_start  = str(manifest.get("role_verb_start") or "start")
    role_verb_end    = str(manifest.get("role_verb_end") or "end")
    export_title     = str(manifest.get("export_title") or "Work Log")
    display_name     = str(manifest.get("display_name") or name)
    support_email    = assigned_email or str(manifest.get("support_email") or "")
    domain_val       = domain or str(manifest.get("domain") or "")

    short_desc = f"{display_name[:22]} — {role_noun.capitalize()} shift & hours tracker"[:80]

    full_desc = (
        f"Manage {role_noun} schedules and track working hours with ease.\n\n"
        f"{display_name} provides a simple, reliable solution for UK businesses to "
        f"record {role_noun} shifts, monitor attendance, and export time logs for payroll.\n\n"
        f"Key features:\n"
        f"• Log shift {role_verb_start} and {role_verb_end} times instantly\n"
        f"• View weekly schedule at a glance\n"
        f"• Export {export_title} reports\n"
        f"• Secure and private — data stays on your device\n"
        f"• Designed for UK Working Time Regulations compliance\n\n"
        f"Simple, fast, and built for everyday reliability."
    )

    defaults: dict = {
        # 1. Create App
        "app_name":             name[:30],
        "package_name":         application_id or "",
        "default_language":     "en-GB",
        "app_type":             "app",
        "is_free":              True,
        "policy_confirmed":     False,
        "signing_tos_accepted": False,
        "export_laws_accepted": False,
        # 2. App Content
        "app_access":           "all_available",
        "contains_ads":         False,
        # 3. Content Ratings
        "ratings_category":     "all_other",
        "ratings_email":        support_email,
        "q_violence":           False,
        "q_sexual":             False,
        "q_teen_rating":        False,
        "q_profanity":          False,
        "q_controlled_drugs":   False,
        "q_gambling":           False,
        "q_user_generated":     False,
        "q_account_sharing":    False,
        "q_location_sharing":   False,
        "ratings_iarc_agreed":  False,
        # 4. Target Audience
        "target_age":           "18_and_over",
        "legal_compliance":     False,
        # 5. Data Safety
        "data_collection":      False,
        "families_policy":      False,
        # 6. Policy Declarations
        "is_government":        False,
        "financial_features":   "none",
        "uses_advertising_id":  False,
        "health_features":      "none",
        # 7. Store Listing
        "app_category":         "BUSINESS",
        "contact_email":        support_email,
        "contact_website":      f"https://{domain_val}" if domain_val else "",
        "short_description":    short_desc,
        "full_description":     full_desc[:4000],
        # 8. Release
        "release_name":         "",
        "release_notes":        (
            f"<en-US>\n"
            f"Initial release of {display_name}.\n\n"
            f"• {role_noun.capitalize()} shift {role_verb_start} and {role_verb_end} time logging\n"
            f"• Weekly schedule overview\n"
            f"• Export {export_title} reports\n"
            f"• Designed for UK Working Time Regulations compliance\n"
            f"</en-US>"
        ),
    }

    f = _pc_file(cn, name)
    if f.exists():
        try:
            saved = json.loads(f.read_text(encoding="utf-8"))
            defaults.update(saved)
        except Exception:
            pass

    if application_id and not defaults.get("package_name"):
        defaults["package_name"] = application_id

    return defaults


def _write_pc(cn: str, name: str, data: dict) -> None:
    f = _pc_file(cn, name)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _build_pc(cn: str, name: str, app_id: str, manifest: dict,
              assigned_email: str, domain: str) -> dict:
    """Assemble Play Console data, backfilling release_name from version.json."""
    pc = _read_pc(cn, name, app_id, manifest=manifest,
                  assigned_email=assigned_email, domain=domain)
    if not pc.get("release_name"):
        ver = _read_version(cn, name)
        pc["release_name"] = ver.get("version_name", "1.0")
    return pc


def _rmdir(path: Path) -> None:
    """Remove a directory and all its contents, silently."""
    try:
        if path.is_dir():
            shutil.rmtree(path)
    except Exception as e:
        print(f"  [rmdir] {path}: {e}")


def _clean_stage_output(cn: str, name: str, stage: str) -> None:
    """Delete prior output files for a stage before re-running it."""
    cdir = _co_dir(cn, name)
    if stage == "certificate":
        _rmdir(cdir / "certificate")
    elif stage == "director_id":
        _rmdir(cdir / "director_id")
    elif stage == "app":
        arts = cdir / "app" / "artifacts"
        if arts.is_dir():
            for f in arts.iterdir():
                if f.suffix in {".apk", ".aab"}:
                    try: f.unlink()
                    except Exception: pass


def _tree_node(path: Path) -> dict | None:
    """Recursive tree builder (no caching — called by _tree which caches root)."""
    if not path.exists():
        return None
    if path.is_file():
        return {"name": path.name, "type": "file",
                "size": path.stat().st_size, "path": str(path)}
    children: list[dict] = []
    try:
        for child in sorted(path.iterdir(),
                            key=lambda p: (p.is_file(), p.name.lower())):
            node = _tree_node(child)
            if node:
                children.append(node)
    except PermissionError:
        pass
    return {"name": path.name, "type": "dir", "children": children}


_TREE_CACHE: dict[str, tuple[float, dict | None]] = {}  # path → (ts, result)
_TREE_TTL = 10.0  # seconds


def _tree(path: Path) -> dict | None:
    """Return a nested tree dict, cached for _TREE_TTL seconds per root path."""
    key = str(path)
    now = time.monotonic()
    entry = _TREE_CACHE.get(key)
    if entry and now - entry[0] < _TREE_TTL:
        return entry[1]
    result = _tree_node(path)
    _TREE_CACHE[key] = (now, result)
    return result


# ── Retry helper ───────────────────────────────────────────────────────────

def _with_retry(fn, retries: int = 3, delay: float = 2.0):
    """Call fn(); on exception retry up to retries times with exponential backoff."""
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if attempt < retries - 1:
                wait = delay * (2 ** attempt)
                print(f"  [retry {attempt + 1}/{retries}] {e} — retrying in {wait:.0f}s")
                time.sleep(wait)
    raise last_exc  # type: ignore[misc]


# ── Core stage executor ────────────────────────────────────────────────────

def _exec_stage(cn: str, stage: str, row: dict):
    """Execute one stage. Called from thread. stdout already redirected."""
    name = str(row.get("Company Name") or "")
    sys.path.insert(0, str(ROOT))

    if stage == "details":
        from run_pipeline import get_company_details
        d = _with_retry(lambda: get_company_details(cn))
        # run_pipeline.company_row_updates owns the header -> value mapping.
        # This call site used to spell it out, and four of its ten keys
        # ("type", "date_of_incorporation", "director_dobs",
        # "director_genders") were not keys get_company_details returned -
        # so Type and both director detail columns were written as "", and
        # because db._norm("") is None and db.update_row has no blank-skip,
        # every run of this stage also erased the Date of Creation the add
        # route had stored. One mapping, in one place, cannot drift from
        # the function that feeds it.
        _update_excel_row(cn, d["row_updates"])
        # One line into the job log, so the drawer has something to show. The
        # COLUMNS are named, never their values: row_updates carries director
        # names and dates of birth, and the job log must not become a second
        # place personal data is printed.
        _cols = sorted(d["row_updates"])
        print(f"  details: wrote {len(_cols)} column(s) from Companies House: "
              f"{', '.join(_cols) or 'none'}")
        return d

    elif stage == "duns":
        from run_pipeline import get_duns_number
        r = get_duns_number(cn, headless=True)
        if r.get("duns_number"):
            _update_excel_row(cn, {"DUNS Number": r["duns_number"], "DUNS Status": "found"})
        elif r.get("status") == "submitted":
            _update_excel_row(cn, {"DUNS Status": "submitted",
                                   "DUNS Email Used": r.get("temp_email", "")})
        if r.get("duns_number"):
            print(f"  duns: {r['duns_number']} ({r.get('status') or 'found'})")
        else:
            print(f"  duns: no number (status={r.get('status') or 'unknown'})"
                  + (f": {r['error']}" if r.get("error") else ""))
        return r

    elif stage == "certificate":
        _clean_stage_output(cn, name, "certificate")
        from run_pipeline import download_certificate
        out = str(_co_dir(cn, name) / "certificate")
        path, err = _with_retry(lambda: download_certificate(cn, out))
        if not path:
            # download_certificate says "there is no document" by RETURNING
            # (None, reason) - run_pipeline.py:894 for a filing history with
            # no document, :904 for a non-200 - and never by raising. Without
            # a status this read as success, so a company with no certificate
            # got a green tick and the reason was discarded.
            print(f"  certificate: nothing downloaded: "
                  f"{err or 'no reason given'}")
            return {"status": "error",
                    "error": err or "no certificate was downloaded"}
        _update_excel_row(cn, {"Certificate Downloaded": True, "Certificate Path": path})
        print(f"  certificate: saved {path}")
        return {"status": "downloaded", "path": path, "error": err}

    elif stage == "domain":
        from run_pipeline import register_company_domain
        details = {
            "company_name": name, "company_number": cn,
            "sic_codes": str(row.get("SIC Codes") or ""),
            "address": str(row.get("Address") or ""),
        }
        # Persistence of Domain / Domain Status / Domain Cost belongs to
        # register_company_domain (run_pipeline.py), which owns the purchase
        # claim and must record the outcome in the same place it spends the
        # money. Writing them here as well gave two owners for one fact, and
        # this call site used the mere presence of a "domain" key as proof of
        # purchase - which the failure path also satisfied, so a rejected
        # order was recorded as a registered domain and the stage was marked
        # complete for good. Do not reintroduce a write here.
        result = register_company_domain(name, details)
        # The money logger narrates the billed order itself, and reaches this
        # log through logging_setup's stdout handler. This line covers the
        # paths that fail BEFORE the purchase claim is taken - no domain
        # available, no company number, a refused availability check - which
        # emit no money line at all.
        print(f"  domain: {result.get('status') or 'unknown'}"
              + (f" {result['domain']}" if result.get("domain") else "")
              + (f": {result['error']}" if result.get("error") else ""))
        return result

    elif stage == "email":
        from email_pool import EmailPool
        pool = EmailPool()
        ex_email, ex_first, ex_last = pool.get_assigned_email(cn)
        if ex_email:
            # Already assigned in pool — sync to Excel if missing
            if not str(row.get("Assigned Email") or ""):
                _update_excel_row(cn, {"Assigned Email": ex_email,
                                       "Account Name": f"{ex_first} {ex_last}"})
            print(f"  email: {ex_email} (already assigned to this company)")
            return {"email": ex_email, "status": "already_assigned"}
        email, first, last = pool.assign_next(company_number=cn, company_name=name)
        _update_excel_row(cn, {"Assigned Email": email,
                                "Account Name": f"{first} {last}"})
        print(f"  email: {email} assigned from the pool")
        return {"email": email, "status": "assigned"}

    elif stage == "director_id":
        _clean_stage_output(cn, name, "director_id")
        import pipeline as _pl
        _pl.stage_director_id(company_numbers=[cn])
        return {"status": "done"}

    elif stage == "app":
        _clean_stage_output(cn, name, "app")
        import pipeline as _pl
        _pl.stage_build(company_numbers=[cn])
        return {"status": "done"}

    raise ValueError(f"Unknown stage: {stage}")


# ── Routes ─────────────────────────────────────────────────────────────────

@api_v2.route("/app")
def app_page():
    return send_file(ROOT / "static" / "app.html")


@api_v2.route("/api/v2/companies")
def v2_companies():
    rows = _all_rows()
    archived_param = request.args.get("archived", "false").lower()
    show_archived = archived_param == "true"
    
    out = []
    for row in rows:
        cn = _cn8(row.get("Company Number"))
        if not cn or cn == "00000000": continue
        
        # Filter by archive status
        archived_val = str(row.get("Archived") or "").strip().lower()
        is_archived = archived_val in ("yes", "true", "1")
        if is_archived != show_archived:
            continue
        
        stages = _compute_stages(cn, row)
        running = [s for s in STAGE_ORDER if _job_get(cn, s).get("status") == "running"]
        company_status = str(row.get("Company Status") or "").strip() or "Active"
        
        out.append({
            "cn":              cn,
            "name":            str(row.get("Company Name") or ""),
            "domain":          str(row.get("Domain") or ""),
            "stages":          stages,
            "n_done":          sum(stages.values()),
            "running":         running,
            "company_status":  company_status,
            "notes":           str(row.get("Company Notes") or "").strip(),
            "archived":        is_archived,
        })
    return jsonify(out)


@api_v2.route("/api/v2/company/<cn>")
def v2_company(cn):
    row = _find_row(cn)
    if not row:
        return jsonify({"error": "Not found"}), 404

    name     = str(row.get("Company Name") or "")
    cdir     = _co_dir(cn, name)
    stages   = _compute_stages(cn, row)
    manifest    = _read_manifest(cn, name)
    app_id      = str(manifest.get("application_id") or "")
    privacy_url = str(manifest.get("privacy_policy_url") or "")
    a_email     = str(row.get("Assigned Email") or "")
    domain_val  = str(row.get("Domain") or "")

    jobs = {}
    for s in STAGE_ORDER:
        j = _job_get(cn, s)
        if j: jobs[s] = {k: v for k, v in j.items() if k != "log"}

    return jsonify({
        "cn":               cn,
        "name":             name,
        "status":           str(row.get("Status") or ""),
        "type":             str(row.get("Type") or ""),
        "sic":              str(row.get("SIC Codes") or ""),
        "address":          str(row.get("Address") or ""),
        "directors":        str(row.get("Directors") or ""),
        "nationalities":    str(row.get("Director Nationalities") or ""),
        "dobs":             str(row.get("Director DOB") or ""),
        "genders":          str(row.get("Director Gender") or ""),
        "buvei_first":      str(row.get("Buvei First Name") or ""),
        "buvei_last":       str(row.get("Buvei Last Name") or ""),
        "duns":             str(row.get("DUNS Number") or ""),
        "duns_status":      str(row.get("DUNS Status") or ""),
        "duns_email":       str(row.get("DUNS Email Used") or ""),
        "dnb_name":         str(row.get("D&B Legal Name") or ""),
        "dnb_address":      str(row.get("D&B Address") or ""),
        "date_of_creation": str(row.get("Date of Creation") or ""),
        "domain":           str(row.get("Domain") or ""),
        "domain_status":    str(row.get("Domain Status") or ""),
        "domain_cost":      str(row.get("Domain Cost") or ""),
        "email":            str(row.get("Assigned Email") or ""),
        "account_name":     str(row.get("Account Name") or ""),
        "developer_email":  str(row.get("Developer Email") or ""),
        "dev_email_forwarding": str(row.get("Dev Email Forwarding") or ""),
        "google_txt_status": str(row.get("Google TXT Status") or ""),
        "organization_phone": str(row.get("Organization Phone") or ""),
        "certificate_path": str(row.get("Certificate Path") or ""),
        "certificate_downloaded": str(row.get("Certificate Downloaded") or ""),
        "application_id":   app_id,
        "privacy_policy_url": privacy_url,
        "company_status":   str(row.get("Company Status") or "").strip() or "Active",
        "notes":            str(row.get("Company Notes") or "").strip(),
        "archived":         str(row.get("Archived") or "").strip().lower() in ("yes", "true", "1"),
        "stages":           stages,
        "files": {
            "certificate":   _ls(cdir / "certificate", exts=[".pdf"]),
            "director_id":   _ls(cdir / "director_id",
                                 names=["ID Front.jpeg", "ID Back.jpeg", "ID_Combined.jpeg"]),
            "app":           _ls(cdir / "app" / "artifacts", exts=[".apk", ".aab"]),
            "payment_profile": [_pp(cdir)] if _pp(cdir) else [],
        },
        "tree":             _tree(cdir),
        "version":          _read_version(cn, name),
        "play_console":     _build_pc(cn, name, app_id, manifest, a_email, domain_val),
        "jobs":             jobs,
    })


@api_v2.route("/api/v2/company/<cn>/fields", methods=["POST"])
def v2_update_fields(cn):
    """Manually override one or more company fields. Values are written to the
    company row; stage status and all info panels are recomputed on the next
    fetch, so a manual edit (e.g. setting Domain) propagates everywhere."""
    row = _find_row(cn)
    if not row:
        return jsonify({"error": "Not found"}), 404
    body = request.get_json(force=True, silent=True) or {}
    fields = body.get("fields", body)
    if not isinstance(fields, dict):
        return jsonify({"error": "fields must be an object"}), 400

    updates = {}
    for key, val in fields.items():
        header = EDITABLE_FIELDS.get(key)
        if header is None:
            continue
        updates[header] = "" if val is None else str(val).strip()

    if not updates:
        return jsonify({"error": "no editable fields provided"}), 400

    failed = _save_or_explain(cn, updates)
    if failed:
        return failed
    fresh  = _find_row(cn) or {}
    stages = _compute_stages(cn, fresh)
    return jsonify({"status": "ok", "cn": cn,
                    "updated": list(updates.keys()), "stages": stages}), 200


@api_v2.route("/api/v2/company/<cn>/stage/<stage>", methods=["POST"])
def v2_run_stage(cn, stage):
    if stage not in STAGE_ORDER:
        return jsonify({"error": f"Unknown stage: {stage}"}), 400

    row = _find_row(cn)
    if not row:
        return jsonify({"error": "Company not found"}), 404

    if _job_get(cn, stage).get("status") == "running":
        return jsonify({"error": "Already running"}), 409

    _spawn(cn, stage, lambda: _exec_stage(cn, stage, _find_row(cn) or row))
    return jsonify({"status": "started", "cn": cn, "stage": stage})


@api_v2.route("/api/v2/company/<cn>/run-all", methods=["POST"])
def v2_run_all(cn):
    """Run all pending stages in order for one company."""
    row = _find_row(cn)
    if not row:
        return jsonify({"error": "Company not found"}), 404

    running = [s for s in STAGE_ORDER if _job_get(cn, s).get("status") == "running"]
    if running:
        return jsonify({"error": f"'{running[0]}' is already running"}), 409

    def _seq():
        with _co_lock(cn):
            for stage in STAGE_ORDER:
                fresh = _find_row(cn) or {}
                if _compute_stages(cn, fresh).get(stage):
                    continue  # already done
                key = _jk(cn, stage)
                _job_set(cn, stage, status="running",
                         started_at=datetime.now().isoformat(),
                         finished_at=None, error=None, log=None)
                _TEE.bind(key)
                try:
                    result = _exec_stage(cn, stage, _find_row(cn) or fresh)
                    log = _TEE.unbind()
                    failure = _result_failure(result)
                    _job_set(cn, stage,
                             status="failed" if failure else "done",
                             finished_at=datetime.now().isoformat(),
                             error=failure,
                             log=log, result=str(result)[:500])
                    if failure:
                        # Stop for a reported failure exactly as for a raised
                        # one. Walking on ran the next stages - the billed
                        # domain purchase among them - on the strength of a
                        # stage that had not succeeded.
                        break
                except Exception as e:
                    log = _TEE.unbind()
                    _job_set(cn, stage, status="failed",
                             finished_at=datetime.now().isoformat(),
                             error=str(e),
                             log=log + "\n" + traceback.format_exc())
                    break  # stop on first failure

    threading.Thread(target=_seq, daemon=True).start()
    return jsonify({"status": "started"})


@api_v2.route("/api/v2/company/<cn>/job/<stage>")
def v2_job(cn, stage):
    j = _job_get(cn, stage)
    return jsonify(j if j else {"status": "idle"})


# ── Kenyan-director admission rule ──────────────────────────────────────────
# Only companies with a Kenyan director may enter the pipeline.
#
# Companies House cannot answer this in a search. Measured against the live
# API: /search/companies returns no officer data at all (its item keys are
# address, address_snippet, company_number, company_status, company_type,
# date_of_creation, description, description_identifier, kind, links,
# matches, snippet, title), and /advanced-search/companies silently IGNORES
# a nationality parameter - hits came back 20075 with `nationality`,
# `officer_nationality`, `director_nationality` and with none. Nationality
# exists only on /company/{cn}/officers, so deciding this costs one extra
# request per candidate.
#
# That is expensive and the cost is deliberate, so it is bounded and
# reported rather than hidden:
#   - the budget is 600 requests per 5 minutes (X-Ratelimit-Limit 600,
#     X-Ratelimit-Window 5m, read from a live response header), and it is
#     shared with the pipeline stages, so enrichment stops at a floor
#     instead of starving them;
#   - gunicorn runs workers=1 / threads=8, and workers=1 is a correctness
#     constraint (gunicorn.conf.py:26-32), so eight threads are the whole
#     app. run_pipeline.ch_get sleeps up to 30s per 429 retry, five times -
#     150s with a request thread held - so enrichment uses its own
#     short-timeout, no-retry fetch and treats a failure as "unknown";
#   - verdicts are cached, because a company's directors do not change
#     between keystrokes.
_NAT_TTL = 24 * 3600          # a directorship does not change hourly
_NAT_MAX_PER_SEARCH = 12      # hard ceiling on requests one search may spend
_NAT_MIN_REMAIN = 150         # a RESERVATION: never spend below this
_nat_cache: dict[str, tuple[float, bool, str]] = {}
_off_cache: dict[str, tuple[float, list]] = {}   # the officer payload itself
_nat_lock = threading.Lock()
_ch_remain: list[int | None] = [None]   # last seen X-Ratelimit-Remain

# A pooled session, because the cost here was connection setup, not the API.
# Measured, 12 lookups through a 4-thread pool:
#     requests.get() per call      2.26s   (a new TCP+TLS handshake each time)
#     one pooled Session           0.49s
#     one pooled Session, 12 thr   0.76s
# So the fix is connection reuse, and MORE concurrency is actively worse -
# 12 workers came out slower than 4. pool_maxsize covers the 4 enrichment
# threads with headroom; max_retries=0 because a retry here would hold one
# of the eight gunicorn threads and a failure must refuse, not wait.
_ch_session = requests.Session()
_ch_session.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=8,
                                          max_retries=0))


# The officer parsing lives in directors.py, imported by run_pipeline too.
# The console decides who may be ADDED and the pipeline decides what gets
# WRITTEN, and both answers have to come from the same reading of the same
# officer list - two readings is how this project got two domain scorers
# that disagreed. The aliases keep the names at their call sites here.
_nationalities = directors.nationalities
_KENYAN = directors.KENYAN


def _kenyan_verdict(items: list[dict]) -> tuple[bool, str]:
    """Admit iff at least one ACTIVE DIRECTOR is Kenyan, and no active
    director's nationality is unrecorded.

    Both halves were chosen deliberately. "At least one" admits
    KENYA AIRWAYS PLC, whose active directors are Kenyan, Canadian and
    Swedish, and admits a dual national ('British,Kenyan'). The second half
    is why a company with a Kenyan director and another whose nationality is
    blank is refused: unknown is not Kenyan, and 2 of 60 sampled directors
    had no nationality on record, so this is a real case and not a
    hypothetical.

    Change this function to change the rule; nothing else encodes it.
    """
    actives = directors.active_directors(items)
    if not actives:
        return False, "no active directors on record"
    unknown = sum(1 for o in actives if not _nationalities(o))
    if unknown:
        return False, (f"{unknown} of {len(actives)} active directors have no "
                       f"nationality recorded")
    kenyan = sum(1 for o in actives if _KENYAN in _nationalities(o))
    if not kenyan:
        return False, f"no Kenyan director among {len(actives)} active"
    return True, f"{kenyan} of {len(actives)} active directors Kenyan"


def _fetch_kenyan(cn: str) -> tuple[bool, str]:
    """Is this company admissible? Cached; never blocks for long.

    Deliberately NOT run_pipeline.ch_get: that retries a 429 five times with
    sleeps up to 30s, which would hold one of the eight request threads for
    up to 150s. Here a failure is simply "unknown", which the rule refuses -
    an admission must be positively established, not assumed on an error.
    """
    now = time.time()
    with _nat_lock:
        hit = _nat_cache.get(cn)
        if hit and hit[0] > now:
            return hit[1], hit[2]

    key = os.getenv("COMPANIES_HOUSE_API_KEY", "")
    if not key:
        return False, "COMPANIES_HOUSE_API_KEY is not set"
    try:
        r = _ch_session.get(
            f"https://api.company-information.service.gov.uk/company/{cn}/officers",
            params={"items_per_page": 50}, auth=(key, ""), timeout=8)
    except Exception as e:
        return False, f"officers lookup failed: {type(e).__name__}"
    rem = r.headers.get("X-Ratelimit-Remain")
    if rem is not None:
        try:
            _ch_remain[0] = int(rem)
        except ValueError:
            pass
    if r.status_code != 200:
        # Not cached: a 429 or a 502 says nothing about the company, and
        # caching it would keep refusing a company that is in fact eligible.
        return False, f"officers lookup returned HTTP {r.status_code}"

    items = r.json().get("items") or []
    ok, why = _kenyan_verdict(items)
    with _nat_lock:
        _nat_cache[cn] = (now + _NAT_TTL, ok, why)
        # Kept separately from the verdict so the verdict cache stays the
        # 3-tuple its own tests pin. The point of holding the payload is
        # that the autofill which follows an add needs the SAME officer
        # list this admission check just paid for - so filling the four
        # director columns costs no request at all.
        _off_cache[cn] = (now + _NAT_TTL, items)
    return ok, why


def _autofill_company(cn: str) -> tuple[dict, str]:
    """Fill everything Companies House knows about a newly added company.

    Returns (what was written, why nothing more was). The pipeline is meant
    to be autonomous; db.add_company writes three columns, and until now the
    remaining seven waited for an operator to run the details stage by hand.

    ONE extra request. The officer list is the one the admission check in
    _fetch_kenyan just fetched and cached, so only the company profile is
    new - and the profile is where Status, Type, SIC codes and the
    registered office come from.

    Deliberately NOT run_pipeline.ch_get: that retries a 429 five times
    with sleeps up to 30s, and this runs on one of eight gunicorn request
    threads (gunicorn.conf.py:26-32). A failure here leaves the row as
    db.add_company made it, which the details stage can still fill later,
    so the add must never fail on it.

    company_row_updates omits anything Companies House does not give, so
    this cannot blank a column.
    """
    now = time.time()
    with _nat_lock:
        hit = _off_cache.get(cn)
    items = hit[1] if hit and hit[0] > now else None
    if items is None:
        # Only reachable if the verdict came from somewhere other than a
        # live fetch; the add route calls _fetch_kenyan first.
        return {}, "officer list not available"

    key = os.getenv("COMPANIES_HOUSE_API_KEY", "")
    if not key:
        return {}, "COMPANIES_HOUSE_API_KEY is not set"
    try:
        r = _ch_session.get(
            f"https://api.company-information.service.gov.uk/company/{cn}",
            auth=(key, ""), timeout=8)
    except Exception as e:
        return {}, f"profile lookup failed: {type(e).__name__}"
    rem = r.headers.get("X-Ratelimit-Remain")
    if rem is not None:
        try:
            _ch_remain[0] = int(rem)
        except ValueError:
            pass
    if r.status_code != 200:
        return {}, f"profile lookup returned HTTP {r.status_code}"

    from run_pipeline import company_row_updates
    updates = company_row_updates(r.json(), items)
    if not updates:
        return {}, "Companies House returned nothing to fill"
    try:
        db.update_row(cn, updates)
    except Exception as e:
        return {}, f"could not write the row: {type(e).__name__}: {e}"
    return updates, ""


def _apply_kenyan_filter(cands: list[dict]) -> tuple[list[dict], int, str]:
    """Keep only admissible candidates. Returns (kept, excluded, note)."""
    if not cands:
        return [], 0, ""

    # A RESERVATION, not a warning. The 600-per-5-minutes window is shared
    # with the pipeline stages, and the `details` and `duns` stages calling
    # Companies House matter more than a type-ahead does. Below the floor
    # this spends nothing at all.
    #
    # It returns NOTHING rather than returning the candidates unfiltered.
    # Unfiltered would mean showing - and letting an operator add - a
    # company with no Kenyan director, which is the exact thing the rule
    # exists to prevent. Refusing to answer is recoverable; admitting the
    # wrong company is not.
    rem = _ch_remain[0]
    if rem is not None and rem < _NAT_MIN_REMAIN:
        return [], 0, (f"not checked: Companies House quota is down to {rem} "
                       f"for this 5-minute window, and {_NAT_MIN_REMAIN} are "
                       f"reserved for the pipeline stages. Try again shortly, "
                       f"or enter the company number.")

    budget = min(len(cands), _NAT_MAX_PER_SEARCH)
    examined, kept = cands[:budget], []
    unexamined = len(cands) - budget

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        verdicts = list(pool.map(lambda c: _fetch_kenyan(c["cn"]), examined))

    for cand, (ok, why) in zip(examined, verdicts):
        cand["kenyan_reason"] = why
        if ok:
            kept.append(cand)

    excluded = len(examined) - len(kept)
    bits = []
    if excluded:
        bits.append(f"{excluded} hidden (no Kenyan director)")
    if unexamined:
        bits.append(f"{unexamined} not checked (limit {_NAT_MAX_PER_SEARCH} "
                    f"per search)")
    rem = _ch_remain[0]
    if rem is not None and rem < _NAT_MIN_REMAIN * 2:
        bits.append(f"Companies House quota getting low ({rem} left this "
                    f"5-minute window)")
    return kept, excluded, "; ".join(bits)


@api_v2.route("/api/v2/company/search")
def v2_search():
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])

    ql = q.lower()

    # Optional year-of-registration filter: ?years=2020,2021,2022
    years_param = request.args.get("years", "").strip()
    year_filter = {y.strip() for y in years_param.split(",") if y.strip()}

    rows = _all_rows()
    results = []

    for row in rows:
        cn    = _cn8(row.get("Company Number"))
        name  = str(row.get("Company Name") or "")
        dirs  = str(row.get("Directors") or "")
        if ql in cn or ql in name.lower() or ql in dirs.lower():
            stages = _compute_stages(cn, row)
            doc    = str(row.get("Date of Creation") or "")
            results.append({
                "cn":        cn,
                "name":      name,
                "directors": dirs,
                "n_done":    sum(stages.values()),
                "stages":    stages,
                "source":    "local",
                "date_of_creation": doc,
                "year":      doc[:4] if doc else "",
            })

    # Apply year filter to local matches (so CH fallback still runs if none match)
    if year_filter:
        results = [r for r in results if (r.get("year") or "") in year_filter]

    # Fall back to CH API if nothing found locally
    ch_error = None
    kenyan_note = ""
    kenyan_excluded = 0
    ch_candidates: list[dict] = []
    ch_total = 0
    if not results:
        try:
            sys.path.insert(0, str(ROOT))
            from run_pipeline import ch_get
            data = ch_get(f"/search/companies?q={q}&items_per_page=20")
            ch_total = int(data.get("total_results") or 0)
            for item in (data.get("items") or []):
                if item.get("company_status", "").lower() != "active": continue
                item_cn = _cn8(item.get("company_number", ""))
                if any(c["cn"] == item_cn for c in results): continue
                inc = item.get("date_of_creation", "") or ""
                yr  = inc[:4] if inc else ""
                if year_filter and yr not in year_filter:
                    continue
                ch_candidates.append({
                    "cn":        item_cn,
                    "name":      item.get("title", ""),
                    "directors": "",
                    "n_done":    0,
                    "stages":    {s: False for s in STAGE_ORDER},
                    "source":    "ch_api",
                    "date_of_creation": inc,
                    "year":      yr,
                })
        except Exception as e:
            ch_error = str(e)
            print(f"  CH search error: {e}")

        # Only companies with a Kenyan director may enter the pipeline, so
        # only Companies House candidates are filtered. Local rows are
        # companies ALREADY in the pipeline - filtering those would make an
        # operator unable to find their own companies, which is a different
        # and worse bug than showing one that no longer qualifies.
        kept, kenyan_excluded, kenyan_note = _apply_kenyan_filter(ch_candidates)
        results.extend(kept)

    resp: dict = {"results": results[:10]}
    if ch_error:
        resp["ch_error"] = ch_error
    # Said out loud, always. An empty list that means "none of the matches
    # had a Kenyan director" must not read as "no such company" - that is
    # the same silent-empty-result failure this pipeline has already had in
    # stage 1 and stage 2.
    if kenyan_excluded or kenyan_note:
        resp["kenyan_filtered"] = kenyan_excluded
        resp["kenyan_note"] = kenyan_note
    if ch_candidates:
        resp["kenyan_rule"] = ("only companies with at least one Kenyan "
                               "director, and none whose nationality is "
                               "unrecorded")
        # How broad the query was. total_results is a top-level key of the
        # search response (measured: 1 for a company number, 1014 for
        # "kenya airways", 10000 for "consulting"), so this costs nothing.
        # Without it, an eligible company sitting past the examine ceiling
        # is invisible; with it the operator can see the search was broad
        # and narrow it, or enter the number - which returns exactly one
        # candidate and so costs two requests in total.
        resp["ch_total"] = ch_total
        if ch_total > len(ch_candidates):
            resp["kenyan_advice"] = (
                f"{ch_total} companies match. Only the first "
                f"{min(len(ch_candidates), _NAT_MAX_PER_SEARCH)} were checked "
                f"for a Kenyan director - narrow the search, or enter the "
                f"8-character company number to check one exactly.")
    return jsonify(resp)


@api_v2.route("/api/v2/company/add", methods=["POST"])
def v2_add_company():
    data = request.get_json(force=True) or {}
    cn   = _cn8(data.get("cn") or data.get("company_number") or "")
    name = str(data.get("name") or data.get("company_name") or "").strip()
    # Year of registration (accept full date, or bare year from the dropdown)
    doc  = str(data.get("date_of_creation") or data.get("year") or "").strip()
    if not cn or cn == "00000000":
        return jsonify({"error": "company_number required"}), 400

    existing = _find_row(cn)
    if existing:
        return jsonify({"status": "exists", "cn": cn, "name": str(existing.get("Company Name") or "")}), 200

    # The rule is enforced HERE as well as in the search, because this is
    # the route that actually writes the row. Filtering the search list only
    # hides ineligible companies from the dialog; a direct POST would still
    # add one, and this is the only place a company enters the pipeline
    # (_add_company_row is called nowhere else).
    #
    # The verdict is cached, so a company picked from a filtered search list
    # costs no extra request - it was just checked.
    ok, why = _fetch_kenyan(cn)
    if not ok:
        return jsonify({
            "error": "not eligible",
            "cn": cn,
            "reason": why,
            "rule": ("only companies with at least one Kenyan director, and "
                     "none whose nationality is unrecorded, may be added"),
        }), 422

    _add_company_row(cn, name, doc)

    # Fill the row from Companies House straight away rather than leaving
    # seven columns for an operator to fetch by hand. One extra request -
    # the officer list came from the admission check above. A failure is
    # reported and otherwise ignored: the row exists either way, and the
    # details stage can still fill it.
    filled, fill_error = _autofill_company(cn)
    resp = {"status": "added", "cn": cn,
            "name": filled.get("Company Name") or name,
            "date_of_creation": filled.get("Date of Creation") or doc,
            "autofilled": sorted(filled)}
    if fill_error:
        resp["autofill_error"] = fill_error
    return jsonify(resp), 201


@api_v2.route("/api/v2/company/<cn>/version", methods=["POST"])
def v2_set_version(cn):
    """Save version_code + version_name for a company (does NOT rebuild — caller triggers that)."""
    row = _find_row(cn)
    if not row:
        return jsonify({"error": "Company not found"}), 404
    body = request.get_json(force=True, silent=True) or {}
    vc = body.get("version_code")
    vn = str(body.get("version_name", "")).strip()
    if not isinstance(vc, int) or vc < 1:
        return jsonify({"error": "version_code must be a positive integer"}), 400
    if not vn:
        return jsonify({"error": "version_name is required"}), 400
    name = str(row.get("Company Name") or "")
    _write_version(cn, name, vc, vn)
    return jsonify({"status": "saved", "version_code": vc, "version_name": vn})


@api_v2.route("/api/v2/company/<cn>/play-console", methods=["POST"])
def v2_save_play_console(cn):
    """Save all Play Console form sections for a company."""
    row = _find_row(cn)
    if not row:
        return jsonify({"error": "Company not found"}), 404
    body = request.get_json(force=True, silent=True) or {}
    name = str(row.get("Company Name") or "")

    app_name = str(body.get("app_name", "")).strip()
    if not app_name:
        return jsonify({"error": "app_name is required"}), 400
    if len(app_name) > 30:
        return jsonify({"error": "app_name must be ≤ 30 characters"}), 400
    pkg = str(body.get("package_name", "")).strip()
    if len(pkg) > 150:
        return jsonify({"error": "package_name must be ≤ 150 characters"}), 400
    short_desc = str(body.get("short_description", ""))
    if len(short_desc) > 80:
        return jsonify({"error": "short_description must be ≤ 80 characters"}), 400
    full_desc = str(body.get("full_description", ""))
    if len(full_desc) > 4000:
        return jsonify({"error": "full_description must be ≤ 4000 characters"}), 400

    _STR = [
        "app_name", "package_name", "default_language", "app_type", "app_access",
        "ratings_category", "ratings_email", "target_age", "financial_features",
        "health_features", "app_category", "contact_email", "contact_website",
        "short_description", "full_description",
        "release_name", "release_notes",
    ]
    _BOOL = [
        "is_free", "policy_confirmed", "signing_tos_accepted", "export_laws_accepted",
        "contains_ads", "q_violence", "q_sexual", "q_teen_rating", "q_profanity",
        "q_controlled_drugs", "q_gambling", "q_user_generated", "q_account_sharing",
        "q_location_sharing", "ratings_iarc_agreed", "legal_compliance",
        "data_collection", "families_policy", "is_government", "uses_advertising_id",
    ]
    data: dict = {}
    for f in _STR:
        data[f] = str(body.get(f, "")).strip()
    for f in _BOOL:
        data[f] = bool(body.get(f, False))

    _write_pc(cn, name, data)
    return jsonify({"status": "saved", **data})


@api_v2.route("/api/v2/company/<cn>/rerun-id", methods=["POST"])
def v2_rerun_id(cn):
    """Delete existing director ID files and regenerate a new one via ibenty."""
    row = _find_row(cn)
    if not row:
        return jsonify({"error": "Company not found"}), 404
    if _job_get(cn, "director_id").get("status") == "running":
        return jsonify({"error": "Already running"}), 409
    _spawn(cn, "director_id", lambda: _exec_stage(cn, "director_id", _find_row(cn) or row))
    return jsonify({"status": "started", "cn": cn, "stage": "director_id"})


@api_v2.route("/api/v2/dl")
def v2_download():
    """Serve a file from pipeline_output (security: must be under DL_BASE).

    ?inline=1  — serve inline (for in-browser preview); default is attachment download.
    """
    path = request.args.get("path", "").strip()
    if not path:
        return abort(400)
    # A string prefix on abspath() accepted siblings such as
    # ".../pipeline_output_X" and followed symlinks out of the directory;
    # see safe_path for both reproductions.
    abs_path = resolve_within(DL_BASE, path)
    if abs_path is None:
        return abort(403)
    if not abs_path.is_file():
        return abort(404)
    inline = request.args.get("inline", "0") == "1"
    return send_file(abs_path, as_attachment=not inline)


@api_v2.route("/api/v2/company/<cn>/archive", methods=["POST"])
def v2_archive_company(cn):
    """Archive a company (hide from main view, show in archive tab)."""
    row = _find_row(cn)
    if not row:
        return jsonify({"error": "Company not found"}), 404
    failed = _save_or_explain(cn, {"Archived": "Yes"})
    if failed:
        return failed
    return jsonify({"status": "archived", "cn": cn, "archived": True})


@api_v2.route("/api/v2/company/<cn>/unarchive", methods=["POST"])
def v2_unarchive_company(cn):
    """Unarchive a company (show in main view)."""
    row = _find_row(cn)
    if not row:
        return jsonify({"error": "Company not found"}), 404
    failed = _save_or_explain(cn, {"Archived": "No"})
    if failed:
        return failed
    return jsonify({"status": "unarchived", "cn": cn, "archived": False})


@api_v2.route("/api/v2/company/<cn>/status", methods=["POST"])
def v2_update_status(cn):
    """Update company status (Active, Declined, Pending, On Hold)."""
    row = _find_row(cn)
    if not row:
        return jsonify({"error": "Company not found"}), 404
    
    body = request.json or {}
    status = str(body.get("status", "")).strip()
    valid_statuses = ["Active", "Declined", "Pending", "On Hold"]
    
    if status and status not in valid_statuses:
        return jsonify({"error": f"Invalid status. Must be one of: {', '.join(valid_statuses)}"}), 400
    
    failed = _save_or_explain(cn, {"Company Status": status})
    if failed:
        return failed
    return jsonify({"status": "updated", "cn": cn, "company_status": status})


@api_v2.route("/api/v2/company/<cn>/notes", methods=["POST"])
def v2_update_notes(cn):
    """Update company notes (max 1000 characters)."""
    row = _find_row(cn)
    if not row:
        return jsonify({"error": "Company not found"}), 404
    
    body = request.json or {}
    notes = str(body.get("notes", "")).strip()
    
    if len(notes) > 1000:
        return jsonify({"error": "Notes must be 1000 characters or less"}), 400
    
    failed = _save_or_explain(cn, {"Company Notes": notes})
    if failed:
        return failed
    return jsonify({"status": "updated", "cn": cn, "notes": notes})


@api_v2.route("/health")
def v2_health():
    return jsonify({"status": "ok", "ts": datetime.now().isoformat()})
