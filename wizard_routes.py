"""
Wizard API routes — bolt onto the existing app.py Flask server.

Add to the bottom of app.py (before `if __name__ == "__main__":`)
or import via:  from wizard_routes import register_wizard_routes
                register_wizard_routes(app)
"""
from flask import Blueprint, request, jsonify, send_file
import os
import json
import sys
import threading
import subprocess
import glob
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from datetime import datetime

import db  # Supabase (PostgreSQL) — the single source of truth for companies
from safe_path import resolve_within

wizard = Blueprint("wizard", __name__)

EXCEL_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "pipeline_output", "companies_pipeline.xlsx",
)
MANUAL_INPUTS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "pipeline_output", "manual_inputs.json",
)
MANUAL_INPUTS_FILE_GGPLAY = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "ggplay", "pipeline_output", "manual_inputs.json",
)

_PIPELINE_LOCK = threading.Lock()
_PIPELINE_RUN_STATE = {
    "process": None,
    "pid": None,
    "started_at": None,
    "finished_at": None,
    "exit_code": None,
    "command": None,
    "error": None,
    "log_path": None,
    "log_handle": None,
}


def _resolve_pipeline_script():
    """Path to the pipeline script POST /api/pipeline/run executes.

    This used to prefer ggplay/run_pipeline.py whenever that file existed,
    which made the forked copy the script that actually ran. That fork does
    not import db at all, so it had no purchase claim
    (db.claim_domain_purchase) protecting its billed
    namecheap.domains.create call, and it persisted to the spreadsheet rather
    than to Supabase. The dashboard's main action therefore bypassed both the
    money safety and the single source of truth.

    Always use the maintained copy next to this file.
    """
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "run_pipeline.py")


def _snapshot_pipeline_state_locked():
    proc = _PIPELINE_RUN_STATE.get("process")
    running = bool(proc and proc.poll() is None)

    if proc and not running and _PIPELINE_RUN_STATE.get("exit_code") is None:
        _PIPELINE_RUN_STATE["exit_code"] = proc.poll()
        _PIPELINE_RUN_STATE["finished_at"] = datetime.now().isoformat()
        _PIPELINE_RUN_STATE["process"] = None
        log_handle = _PIPELINE_RUN_STATE.get("log_handle")
        if log_handle:
            try:
                log_handle.flush()
                log_handle.close()
            except Exception:
                pass
            _PIPELINE_RUN_STATE["log_handle"] = None

    return {
        "running": running,
        "pid": _PIPELINE_RUN_STATE.get("pid"),
        "started_at": _PIPELINE_RUN_STATE.get("started_at"),
        "finished_at": _PIPELINE_RUN_STATE.get("finished_at"),
        "exit_code": _PIPELINE_RUN_STATE.get("exit_code"),
        "command": _PIPELINE_RUN_STATE.get("command"),
        "error": _PIPELINE_RUN_STATE.get("error"),
        "log_path": _PIPELINE_RUN_STATE.get("log_path"),
    }


def _latest_pipeline_log_path():
    logs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pipeline_output", "logs")
    pattern = os.path.join(logs_dir, "pipeline_run_*.log")
    candidates = [p for p in glob.glob(pattern) if os.path.isfile(p)]
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)

# ──────────────────────────────────────────────────────────────
# The canonical header row — must match run_pipeline.py + new cols
# ──────────────────────────────────────────────────────────────
HEADERS = [
    "No.", "Company Number", "Company Name", "Short Name",
    "Status", "Type", "Date of Creation", "SIC Codes", "Address",
    "Directors", "Director Nationalities",
    "DUNS Number", "DUNS Status", "DUNS Email Used",
    "Certificate Downloaded", "Certificate Path",
    "Domain", "Domain Status", "Domain Cost",
    "Assigned Email", "Account Name",
    "Company Status", "Company Notes", "Archived",
]


def _col_index(header_row, name):
    """Find column index by header name (case-insensitive)."""
    for i, h in enumerate(header_row):
        if h and str(h).strip().lower() == name.lower():
            return i
    return None


def _ensure_column(ws, header_name):
    """Ensure a column exists and return zero-based index."""
    header = [cell.value for cell in ws[1]]
    idx = _col_index(header, header_name)
    if idx is not None:
        return idx
    ws.cell(row=1, column=len(header) + 1, value=header_name)
    return len(header)


def _ensure_excel():
    """Create the pipeline Excel if it doesn't exist."""
    os.makedirs(os.path.dirname(EXCEL_FILE), exist_ok=True)
    if os.path.exists(EXCEL_FILE):
        return
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Companies Pipeline"
    ws.append(HEADERS)
    # Style headers
    hdr_fill = PatternFill(start_color="003078", end_color="003078", fill_type="solid")
    hdr_font = Font(color="FFFFFF", bold=True, size=11)
    for cell in ws[1]:
        cell.fill = hdr_fill
        cell.font = hdr_font
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"
    wb.save(EXCEL_FILE)


def _load_manual_inputs_db():
    for path in [MANUAL_INPUTS_FILE, MANUAL_INPUTS_FILE_GGPLAY]:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    return {}


def _save_manual_inputs_db(db):
    for path in [MANUAL_INPUTS_FILE, MANUAL_INPUTS_FILE_GGPLAY]:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(db, f, indent=2)
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────
# GET /wizard — the onboarding UI itself
# ──────────────────────────────────────────────────────────────
@wizard.route("/wizard")
def wizard_page():
    """Serve the onboarding wizard page.

    static/index.html previously had no route of its own. It was reachable
    only by knowing Flask's built-in static path, /static/index.html, so
    the page was undiscoverable while every API it calls was registered and
    live - /api/pipeline/*, /api/domains/*, /api/duns/*, all in this
    module. The page was the only part without a front door.

    Lives here rather than in app.py or api_v2.py because this module
    already owns every endpoint the page talks to; the page and its API
    stay in one place.

    Serving the same file from a second URL needs no change to the page:
    it references its assets absolutely (/static/style.css,
    /static/wizard.js), so they resolve identically from /wizard.

    Covered by the app-wide auth gate (app.py:84) like every other route,
    and it adds no exemption. Worth stating plainly: this makes the wizard
    exactly as privileged as the dashboard, because the app has one token
    and one privilege level, not a user model. Anyone who can reach this
    page can also run billed domain purchases.
    """
    return send_file(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "static", "index.html"))


# ──────────────────────────────────────────────────────────────
# POST /api/pipeline/add — add a company from the wizard
# ──────────────────────────────────────────────────────────────
@wizard.route("/api/pipeline/add", methods=["POST"])
def pipeline_add():
    data = request.get_json(force=True)

    cn = (data.get("company_number") or "").strip()
    if not cn:
        return jsonify({"success": False, "error": "company_number is required"}), 400

    # Writes go to Supabase, not to the spreadsheet. This route and
    # /api/v2/company/add used to write to two different stores, so a company
    # added in the wizard was invisible in the dashboard and vice versa.
    if db.find_row(cn):
        return jsonify({"success": False,
                        "error": f"Company {cn} already exists in the pipeline."}), 409

    name = data.get("company_name", "")
    duns_number = data.get("duns_number", "")
    duns_email = data.get("duns_email", "")

    db.add_company(cn, name, data.get("date_of_creation", ""))
    db.update_row(cn, {
        "Company Name":           name,
        "Short Name":             data.get("short_name", ""),
        "Status":                 data.get("status", ""),
        "Type":                   data.get("type", ""),
        "Date of Creation":       data.get("date_of_creation", ""),
        "SIC Codes":              data.get("sic_codes", ""),
        "Address":                data.get("address", ""),
        "Directors":              data.get("directors", ""),
        "Director Nationalities": data.get("nationalities", ""),
        "DUNS Number":            duns_number,
        # Same three outcomes as before: a number means found, an email
        # without a number means submitted, neither means blank.
        "DUNS Status":            ("found" if duns_number
                                   else ("submitted" if duns_email else "")),
        "DUNS Email Used":        duns_email,
        "Domain":                 data.get("domain", ""),
        "Domain Status":          "pending" if data.get("domain") else "",
        "Assigned Email":         data.get("email", ""),
    })

    row = db.find_row(cn) or {}
    return jsonify({
        "success": True,
        "row": row.get("No.") or 0,
        "company_number": cn,
        "message": f"Added {name or cn} to pipeline."
    })


# ──────────────────────────────────────────────────────────────
# GET /api/pipeline/list — list all companies in the pipeline
# ──────────────────────────────────────────────────────────────
@wizard.route("/api/pipeline/list")
def pipeline_list():
    # db.all_rows() already returns header-label-keyed dicts, which is the
    # exact shape this route built by hand from the spreadsheet's header row,
    # so static/wizard.js needs no change.
    return jsonify(db.all_rows())


# ──────────────────────────────────────────────────────────────
# GET /api/email-pool/status — email pool availability
# ──────────────────────────────────────────────────────────────
@wizard.route("/api/email-pool/status")
def email_pool_status():
    try:
        from email_pool import EmailPool
        pool = EmailPool()
        status = pool.status()
        available_emails = [e for e in pool._emails if e not in pool._assignments]
        return jsonify({
            "total": status["total"],
            "used": status["used"],
            "available": status["available"],
            "available_emails": available_emails[:50],  # cap at 50
        })
    except Exception as e:
        return jsonify({"error": str(e), "available_emails": []}), 500


# ──────────────────────────────────────────────────────────────
# GET /api/domains/check — check domain availability via Namecheap
# ──────────────────────────────────────────────────────────────
@wizard.route("/api/domains/check")
def domains_check():
    domains_str = request.args.get("domains", "").strip()
    if not domains_str:
        return jsonify({"error": "provide ?domains=example.co.uk"}), 400

    domain_list = [d.strip() for d in domains_str.split(",") if d.strip()]
    try:
        from namecheap_automation import check_domains
        results = check_domains(domain_list)
        return jsonify(results if isinstance(results, list) else [results])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────────────────────
# GET  /api/duns/lookup  — quick DUNS lookup
# POST /api/duns/request — submit DUNS request via stealth browser
# ──────────────────────────────────────────────────────────────
@wizard.route("/api/duns/lookup")
def duns_lookup():
    cn = request.args.get("company_number", "").strip()
    if not cn:
        return jsonify({"error": "company_number required"}), 400
    try:
        from duns_automation import lookup_duns
        result = lookup_duns(cn)
        return jsonify(result)
    except Exception as e:
        return jsonify({"found": False, "error": str(e)})


# DUNS requests run in a background thread, not in the request.
# stealth_request_duns drives a real browser and then sleeps 10s before
# checking for mail, so running it inline held a gunicorn worker thread for
# the whole duration. With threads = 8 (gunicorn.conf.py), eight of these
# left nothing to serve the dashboard. The state lives in process memory,
# which is sound only because workers = 1 - the same constraint api_v2._JOBS
# already imposes.
_DUNS_JOBS_LOCK = threading.Lock()
_DUNS_JOBS: dict = {}


def _duns_job_get(cn):
    with _DUNS_JOBS_LOCK:
        return dict(_DUNS_JOBS.get(cn, {}))


def _duns_job_set(cn, **kw):
    with _DUNS_JOBS_LOCK:
        _DUNS_JOBS.setdefault(cn, {}).update(kw)


@wizard.route("/api/duns/request", methods=["POST"])
def duns_request():
    data = request.get_json(force=True)
    cn = data.get("company_number", "").strip()
    email = data.get("email", "").strip()
    if not cn or not email:
        return jsonify({"error": "company_number and email required"}), 400

    if _duns_job_get(cn).get("status") == "running":
        return jsonify({"status": "running", "company_number": cn,
                        "message": "A DUNS request for this company is "
                                   "already running."}), 409

    def _work():
        from duns_automation import stealth_request_duns
        try:
            result = stealth_request_duns(cn, headless=True)
            _duns_job_set(cn, status="done", result=result,
                          finished_at=datetime.now().isoformat())
        except Exception as e:
            # The browser path raises for a missing display among other
            # things; surface the message rather than losing it in a thread.
            _duns_job_set(cn, status="failed", error=str(e),
                          finished_at=datetime.now().isoformat())

    _duns_job_set(cn, status="running", started_at=datetime.now().isoformat(),
                  result=None, error=None, finished_at=None)
    threading.Thread(target=_work, daemon=True).start()
    return jsonify({"status": "started", "company_number": cn}), 202


@wizard.route("/api/duns/status/<company_number>")
def duns_status(company_number):
    """Poll a background DUNS request. static/wizard.js polls this after the
    202 from /api/duns/request."""
    job = _duns_job_get(company_number)
    if not job:
        return jsonify({"status": "idle", "company_number": company_number})
    # Flatten the finished result so the UI can read duns_number directly,
    # which is the shape it consumed when this route was synchronous.
    out = {"status": job.get("status"), "company_number": company_number,
           "error": job.get("error")}
    if job.get("status") == "done" and isinstance(job.get("result"), dict):
        out.update(job["result"])
        out["status"] = "done"
    return jsonify(out)


# ──────────────────────────────────────────────────────────────
# Registration helper
# ──────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────
# POST /api/domains/register — actually register via Namecheap
# ──────────────────────────────────────────────────────────────
@wizard.route("/api/domains/register", methods=["POST"])
def domains_register():
    data = request.get_json(force=True)
    domain = data.get("domain", "").strip()
    if not domain:
        return jsonify({"error": "domain is required"}), 400
    try:
        from namecheap_automation import register_domain
        result = register_domain(domain)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────────────────────
# POST /api/domains/find-cheapest — auto-find best domain for company
# ──────────────────────────────────────────────────────────────
@wizard.route("/api/domains/find-cheapest", methods=["POST"])
def domains_find_cheapest():
    data = request.get_json(force=True)
    company_name = data.get("company_name", "").strip()
    if not company_name:
        return jsonify({"error": "company_name required"}), 400
    try:
        from namecheap_automation import find_cheapest_domain
        result = find_cheapest_domain(company_name)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────────────────────
# POST /api/email-pool/assign — assign next available email
# ──────────────────────────────────────────────────────────────
@wizard.route("/api/email-pool/assign", methods=["POST"])
def email_pool_assign():
    data = request.get_json(force=True)
    company_number = data.get("company_number", "").strip()
    company_name = data.get("company_name", "")
    if not company_number:
        return jsonify({"error": "company_number required"}), 400
    try:
        from email_pool import EmailPool
        pool = EmailPool()
        # Check if already assigned
        existing = pool.get_assigned_email(company_number)
        if existing:
            return jsonify({"email": existing, "status": "already_assigned"})
        # Assign next
        result = pool.assign_next(company_number=company_number, company_name=company_name)
        if result:
            return jsonify({"email": result, "status": "assigned", "remaining": pool.available_count})
        return jsonify({"error": "No emails available in pool"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────────────────────
# POST /api/certificate/download — download incorporation certificate
# ──────────────────────────────────────────────────────────────
@wizard.route("/api/certificate/download", methods=["POST"])
def certificate_download():
    data = request.get_json(force=True)
    company_number = data.get("company_number", "").strip()
    company_name = data.get("company_name", "")
    if not company_number:
        return jsonify({"error": "company_number required"}), 400
    try:
        import requests as http_requests
        from dotenv import load_dotenv
        load_dotenv()
        API_KEY = os.getenv("COMPANIES_HOUSE_API_KEY")
        BASE_URL = "https://api.company-information.service.gov.uk"
        # Get filing history
        resp = http_requests.get(
            f"{BASE_URL}/company/{company_number}/filing-history",
            params={"items_per_page": 100},
            auth=(API_KEY, ""),
        )
        resp.raise_for_status()
        filings = resp.json()
        # Find incorporation document
        doc_link = None
        for item in filings.get("items", []):
            cat = (item.get("category") or "").lower()
            desc = (item.get("description") or "").lower()
            ftype = (item.get("type") or "").upper()
            if cat == "incorporation" or ftype == "NEWINC" or "incorporat" in desc:
                links = item.get("links", {})
                doc_link = links.get("document_metadata")
                break
        if not doc_link:
            return jsonify({"status": "not_found", "message": "No incorporation filing found"})
        # Download PDF
        meta_resp = http_requests.get(f"https://api.company-information.service.gov.uk{doc_link}",
                                      auth=(API_KEY, ""))
        meta_resp.raise_for_status()
        meta = meta_resp.json()
        pdf_url = meta.get("links", {}).get("document")
        if not pdf_url:
            return jsonify({"status": "no_pdf", "message": "Document metadata found but no PDF link"})
        # Download the PDF
        pdf_resp = http_requests.get(pdf_url, auth=(API_KEY, ""),
                                     headers={"Accept": "application/pdf"})
        if pdf_resp.status_code == 200:
            safe_name = company_name.replace("/", "-").replace("\\", "-").strip() or company_number
            cert_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "certificates", safe_name)
            os.makedirs(cert_dir, exist_ok=True)
            cert_path = os.path.join(cert_dir, f"Certificate_{company_number}.pdf")
            with open(cert_path, "wb") as f:
                f.write(pdf_resp.content)
            return jsonify({"status": "downloaded", "path": cert_path, "size": len(pdf_resp.content)})
        return jsonify({"status": "download_failed", "http_status": pdf_resp.status_code})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────────────────────
# POST /api/domains/setup-email-forwarding
# ──────────────────────────────────────────────────────────────
@wizard.route("/api/domains/setup-email-forwarding", methods=["POST"])
def domains_email_forwarding():
    data = request.get_json(force=True)
    domain = data.get("domain", "").strip()
    forward_to = data.get("forward_to", "").strip()
    if not domain or not forward_to:
        return jsonify({"error": "domain and forward_to required"}), 400
    try:
        # Cloudflare, not Namecheap: Namecheap's free forwarding requires
        # Namecheap DNS and stops existing the moment the domain is
        # delegated. Note Cloudflare will not deliver to a destination
        # address nobody has verified, so a brand new forward_to comes back
        # as pending_verification rather than as a working route.
        import cloudflare_dns
        result = cloudflare_dns.ensure_dev_route(
            domain, forward_to=forward_to, local_part="support")
        if result.get("status") != "configured":
            # A route that was not created must not answer 200. The old code
            # returned the provider's error dict with a success status.
            return jsonify(result), 400
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────────────────────
# POST /api/domains/apply-google-txt
# ──────────────────────────────────────────────────────────────
@wizard.route("/api/domains/apply-google-txt", methods=["POST"])
def domains_apply_google_txt():
    data = request.get_json(force=True)
    domain = (data.get("domain") or "").strip()
    txt_value = (data.get("txt_value") or "").strip()
    hostname = (data.get("hostname") or "@").strip() or "@"
    company_number = (data.get("company_number") or "").strip()

    if not domain:
        return jsonify({"error": "domain is required"}), 400
    if not txt_value:
        return jsonify({"error": "txt_value is required"}), 400

    try:
        # The set_default_dns call that used to run first is gone. It pointed
        # the domain back at Namecheap's nameservers so Namecheap's setHosts
        # would work - which on a delegated domain revokes the delegation.
        # Cloudflare writes the record individually, so nothing else on the
        # zone is read or rewritten either.
        import cloudflare_dns

        txt_result = cloudflare_dns.ensure_google_txt_record(
            domain, txt_value=txt_value, hostname=hostname)
        if txt_result.get("status") != "configured":
            return jsonify({
                "error": (txt_result.get("message") or txt_result.get("error")
                          or "the TXT record was not written"),
                "status": txt_result.get("status"),
                "stage": "cloudflare_txt",
            }), 400

        # Best-effort write-back to Supabase, the single source of truth.
        # The response key is still "excel_updated" because static/wizard.js
        # reads that name; only the store behind it has changed.
        updated_excel = False
        excel_warning = ""
        if company_number:
            try:
                if db.find_row(company_number):
                    db.update_row(company_number, {
                        "Domain": domain,
                        "Google TXT Status": "configured",
                    })
                    updated_excel = True
            except Exception as e:
                excel_warning = f"TXT applied, but could not update the database: {e}"

        return jsonify({
            "success": True,
            "domain": domain,
            "hostname": hostname,
            "txt_value": txt_value,
            "dns": dns_result,
            "txt": txt_result,
            "excel_updated": updated_excel,
            "warning": excel_warning,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────────────────────
# GET/POST /api/pipeline/manual-inputs/<company_number>
# ──────────────────────────────────────────────────────────────
@wizard.route("/api/pipeline/manual-inputs/<company_number>", methods=["GET"])
def pipeline_manual_inputs_get(company_number):
    try:
        # Named manual_db, not db: `db` is the Supabase module imported above.
        manual_db = _load_manual_inputs_db()
        return jsonify(manual_db.get(company_number, {}))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@wizard.route("/api/pipeline/manual-inputs/<company_number>", methods=["POST"])
def pipeline_manual_inputs_save(company_number):
    try:
        payload = request.get_json(force=True) or {}

        manual = {
            "representative_id": {
                "full_name_as_on_id": (payload.get("representative_id", {}).get("full_name_as_on_id") or "").strip(),
                "personal_address": (payload.get("representative_id", {}).get("personal_address") or "").strip(),
                "id_type": (payload.get("representative_id", {}).get("id_type") or "").strip(),
                "id_country": (payload.get("representative_id", {}).get("id_country") or "").strip(),
                "id_expiry_date": (payload.get("representative_id", {}).get("id_expiry_date") or "").strip(),
                "id_image_front": (payload.get("representative_id", {}).get("id_image_front") or "").strip(),
                "id_image_back": (payload.get("representative_id", {}).get("id_image_back") or "").strip(),
                "id_quality_check": (payload.get("representative_id", {}).get("id_quality_check") or "pending").strip(),
            }
        }

        # Named manual_db, not db: `db` is the Supabase module imported above.
        manual_db = _load_manual_inputs_db()
        manual_db[company_number] = manual
        _save_manual_inputs_db(manual_db)

        # This used to mirror a "Rep ID Status" column into the spreadsheet.
        # That column was written here and read nowhere - its value was just
        # "both ID images present?", which is derivable from the manual-inputs
        # record above - so it is not carried over to Supabase. The response
        # flag now reports whether the company is known, keeping the shape
        # static/wizard.js expects.
        excel_updated = False
        try:
            excel_updated = db.find_row(company_number) is not None
        except Exception:
            excel_updated = False

        return jsonify({"success": True, "company_number": company_number, "excel_updated": excel_updated})
    except Exception as e:
        return jsonify({"error": str(e)}), 500





@wizard.route("/download")
def download_file():
    """Download a file from the pipeline output directory."""
    from flask import send_file, abort
    
    file_path = request.args.get("path", "").strip()
    if not file_path:
        return abort(400)

    # Security: only allow files under pipeline_output. A string prefix on
    # abspath() accepted siblings such as ".../pipeline_output_X" and
    # followed symlinks out of the directory; see safe_path for both
    # reproductions. is_file() replaces the old exists() check so a
    # directory is refused as 404 rather than handed to send_file.
    base_dir = os.path.join(os.path.dirname(__file__), "pipeline_output")
    abs_path = resolve_within(base_dir, file_path)

    if abs_path is None:
        return abort(403)

    if not abs_path.is_file():
        return abort(404)

    return send_file(str(abs_path), as_attachment=True)


@wizard.route("/api/pipeline/retry/<company_number>", methods=["POST"])
def pipeline_retry(company_number):
    """
    Retry a failed company (re-trigger pipeline for a single company).
    Returns a job ID or status message.
    """
    if not company_number:
        return jsonify({"error": "company_number is required"}), 400

    try:
        row = db.find_row(company_number)
        if not row:
            return jsonify({"error": f"Company {company_number} not found"}), 404

        # Clear only the status columns that currently hold an error, which is
        # what dropping the "error" key from each stage dict used to do. This
        # previously read and rewrote the spreadsheet via the forked
        # ggplay.run_pipeline copy, so a retry changed nothing the dashboard
        # reads.
        #
        # MONEY SAFETY: db.DOMAIN_CLAIM ("purchasing") is NOT an error and
        # must never be cleared here. It is the record that a billed
        # namecheap.domains.create order may already have gone through
        # (run_pipeline.register_company_domain). Clearing it would let the
        # domain stage buy a second domain. Only values that start with
        # "error" are reset, so the claim survives a retry by construction.
        cleared = {}
        for col in ("DUNS Status", "Domain Status", "Google TXT Status",
                    "Dev Email Forwarding"):
            value = str(row.get(col) or "").strip().lower()
            if value.startswith("error"):
                cleared[col] = ""
        if cleared:
            db.update_row(company_number, cleared)

        return jsonify({
            "success": True,
            "company_number": company_number,
            "cleared": sorted(cleared),
            "message": "Company queued for retry. Pipeline will process on next run.",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@wizard.route("/api/pipeline/run", methods=["POST"])
def pipeline_run_start():
    """Start a background pipeline run from dashboard action controls."""
    data = request.get_json(silent=True) or {}
    try:
        count = int(data.get("count", 1))
    except (TypeError, ValueError):
        return jsonify({"error": "count must be a valid integer"}), 400

    if count < 1 or count > 500:
        return jsonify({"error": "count must be between 1 and 500"}), 400

    nationality = (data.get("nationality") or "kenyan").strip().lower()
    if not nationality:
        nationality = "kenyan"

    with _PIPELINE_LOCK:
        state = _snapshot_pipeline_state_locked()
        if state["running"]:
            return jsonify({
                "error": "Pipeline is already running",
                "pid": state.get("pid"),
                "started_at": state.get("started_at"),
            }), 409

        script_path = _resolve_pipeline_script()
        if not os.path.exists(script_path):
            return jsonify({"error": f"Pipeline script not found: {script_path}"}), 500

        cmd = [
            sys.executable,
            "-u",
            script_path,
            "--count", str(count),
            "--nationality", nationality,
        ]

        logs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pipeline_output", "logs")
        os.makedirs(logs_dir, exist_ok=True)
        log_file_name = f"pipeline_run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        log_file_path = os.path.join(logs_dir, log_file_name)

        log_handle = None

        try:
            log_handle = open(log_file_path, "a", encoding="utf-8", buffering=1)
            log_handle.write(f"[launcher] starting at {datetime.now().isoformat()}\n")
            log_handle.write(f"[launcher] command: {' '.join(cmd)}\n")
            log_handle.write(f"[launcher] count={count} nationality={nationality}\n")
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            proc = subprocess.Popen(
                cmd,
                cwd=os.path.dirname(os.path.abspath(__file__)),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                env=env,
            )
        except Exception as e:
            if log_handle:
                try:
                    log_handle.close()
                except Exception:
                    pass
            return jsonify({"error": f"Failed to start pipeline: {e}"}), 500

        _PIPELINE_RUN_STATE["process"] = proc
        _PIPELINE_RUN_STATE["pid"] = proc.pid
        _PIPELINE_RUN_STATE["started_at"] = datetime.now().isoformat()
        _PIPELINE_RUN_STATE["finished_at"] = None
        _PIPELINE_RUN_STATE["exit_code"] = None
        _PIPELINE_RUN_STATE["command"] = " ".join(cmd)
        _PIPELINE_RUN_STATE["error"] = None
        _PIPELINE_RUN_STATE["log_path"] = log_file_path
        _PIPELINE_RUN_STATE["log_handle"] = log_handle

        return jsonify({
            "success": True,
            "message": "Pipeline started",
            "pid": proc.pid,
            "started_at": _PIPELINE_RUN_STATE["started_at"],
            "count": count,
            "nationality": nationality,
            "log_path": log_file_path,
        })


@wizard.route("/api/pipeline/run/status")
def pipeline_run_status():
    """Get latest background pipeline run status."""
    with _PIPELINE_LOCK:
        state = _snapshot_pipeline_state_locked()
    return jsonify(state)


@wizard.route("/api/pipeline/run/logs")
def pipeline_run_logs():
    """Return tail of pipeline run logs for live dashboard viewing."""
    try:
        tail_lines = int(request.args.get("tail", 300))
    except (TypeError, ValueError):
        tail_lines = 300

    if tail_lines < 50:
        tail_lines = 50
    if tail_lines > 2000:
        tail_lines = 2000

    with _PIPELINE_LOCK:
        state = _snapshot_pipeline_state_locked()
        log_path = _PIPELINE_RUN_STATE.get("log_path")
        log_handle = _PIPELINE_RUN_STATE.get("log_handle")
        if log_handle:
            try:
                log_handle.flush()
            except Exception:
                pass
        if not log_path:
            log_path = _latest_pipeline_log_path()
            if not log_path:
                return jsonify({"running": state.get("running", False), "log": "", "line_count": 0})

        # log_path is server-derived here (_PIPELINE_RUN_STATE above, or
        # _latest_pipeline_log_path), never taken from the request, so this
        # was not an exploitable bypass like the two download endpoints.
        # Hardened with the same helper so there is one containment rule in
        # the codebase rather than three hand-rolled ones.
        logs_base = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "pipeline_output", "logs")
        safe_log = resolve_within(logs_base, log_path)
        if safe_log is None:
            return jsonify({"error": "invalid log path"}), 403
        abs_log_path = str(safe_log)

    if not os.path.exists(abs_log_path):
        return jsonify({"running": state.get("running", False), "log": "", "line_count": 0})

    try:
        with open(abs_log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return jsonify({"error": f"failed reading logs: {e}"}), 500

    tail = lines[-tail_lines:]
    return jsonify({
        "running": state.get("running", False),
        "log": "".join(tail),
        "line_count": len(lines),
        "tail_lines": len(tail),
        "log_path": abs_log_path,
    })





@wizard.route("/api/company/<cn>/files")
def company_files_api(cn):
    """File tree for a single company (only key files)."""
    import re as _re
    ROOT = os.path.dirname(os.path.abspath(__file__))
    COMP_DIR = os.path.join(ROOT, "pipeline_output", "companies")

    # The company name only decides which folder to look in. db.find_row
    # already normalises the company number the same way the old spreadsheet
    # scan did with .zfill(8).
    name = ""
    try:
        row = db.find_row(cn)
        if row:
            name = str(row.get("Company Name") or "").strip()
    except Exception:
        pass

    safe = _re.sub(r'[\\/:*?"<>|]', "", name).strip()
    folder = f"{cn.zfill(8)} - {safe}" if safe else cn.zfill(8)
    cdir = os.path.join(COMP_DIR, folder)
    sections = []

    # Certificate — only _cert.pdf (skip the large original)
    cert_d = os.path.join(cdir, "certificate")
    cert_files = []
    if os.path.isdir(cert_d):
        for f in sorted(os.listdir(cert_d)):
            fp = os.path.join(cert_d, f)
            if os.path.isfile(fp):
                cert_files.append({"name": f, "size": os.path.getsize(fp), "path": fp})
    sections.append({"name": "certificate", "files": cert_files})

    # Director ID — front, back, combined
    id_d = os.path.join(cdir, "director_id")
    id_files = []
    for f in ["ID Front.jpeg", "ID Back.jpeg", "ID_Combined.jpeg"]:
        fp = os.path.join(id_d, f)
        if os.path.exists(fp):
            id_files.append({"name": f, "size": os.path.getsize(fp), "path": fp})
    sections.append({"name": "director_id", "files": id_files})

    # App — APK and AAB only
    art_d = os.path.join(cdir, "app", "artifacts")
    app_files = []
    if os.path.isdir(art_d):
        for f in sorted(os.listdir(art_d)):
            if f.endswith((".apk", ".aab")):
                fp = os.path.join(art_d, f)
                app_files.append({"name": f, "size": os.path.getsize(fp), "path": fp})
    sections.append({"name": "app", "files": app_files})

    # Payment profile (check subdirectory first, then root)
    pp_files = []
    for pp_path in [
        os.path.join(cdir, "payment_profile", "payment_profile.txt"),
        os.path.join(cdir, "payment_profile.txt"),
    ]:
        if os.path.exists(pp_path):
            pp_files.append({"name": "payment_profile.txt", "size": os.path.getsize(pp_path), "path": pp_path})
            break
    sections.append({"name": "payment_profile", "files": pp_files})

    return jsonify({"cn": cn, "name": name, "sections": sections})


def register_wizard_routes(app):
    """Call this from app.py to mount all wizard endpoints."""
    app.register_blueprint(wizard)
