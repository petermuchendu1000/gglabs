from flask import Flask, request, jsonify, send_from_directory, Response, g
import logging
import uuid
import requests as http_requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import os
import json
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder="static")

# --- Observability ---
# Configured here, before the auth gate below, so that rejected requests are
# logged too: a 401 is exactly the event worth seeing on a system that can
# spend money. Nothing configured logging before this, so the ~30
# logger.info calls already present in browser_stealth.py were being
# discarded - see logging_setup for why that matters.
from logging_setup import configure_logging
configure_logging()
log = logging.getLogger("app")


@app.before_request
def _assign_request_id():
    """Give every request a short id so its log lines can be correlated."""
    g.request_id = uuid.uuid4().hex[:8]
    g.request_started = time.perf_counter()


@app.after_request
def _log_request(response):
    try:
        started = getattr(g, "request_started", None)
        if started is not None:
            log.info("%s %s -> %s in %.0fms", request.method, request.path,
                     response.status_code,
                     (time.perf_counter() - started) * 1000)
        else:
            log.info("%s %s -> %s", request.method, request.path,
                     response.status_code)
    except Exception:
        # Logging must never be the reason a response fails to return.
        pass
    response.headers["X-Request-Id"] = getattr(g, "request_id", "")
    return response


# --- Single-operator authentication gate ---
# Every route in this app was once reachable with no credentials at all,
# including the stage runner that issues billed namecheap.domains.create
# orders (api_v2.py:554 -> run_pipeline.py:582). One app-wide before_request
# closes that at the root: it covers the wizard routes, the api_v2 and
# imports blueprints, and any route added later, so no endpoint can be left
# open by omission.
#
# The gate, the login page and the brute-force limiter live in auth.py, so
# "what protects this app?" has one answer rather than a before_request
# sitting between the Companies House proxy and the DUNS endpoints.
# install_auth accepts three credentials, all checking APP_AUTH_TOKEN:
#
#   signed session cookie                       - browsers, via /login
#   Authorization: Bearer <APP_AUTH_TOKEN>      - scripts, curl, deploy checks
#   HTTP Basic, any username, password = token  - pre-existing callers
#
# Fails CLOSED: with APP_AUTH_TOKEN unset the app serves nothing but
# /health. A system that spends money must not default to open.
from auth import install_auth, _AUTH_EXEMPT  # noqa: E402  (after app exists)

_AUTH_TOKEN = os.getenv("APP_AUTH_TOKEN", "")
install_auth(app)

# --- Accounts, roles and the approval workflow ---
# This module deliberately does NOT create the accounts schema or the
# bootstrap superadmin at import.
#
# It used to. That meant merely starting the process - which gunicorn does
# on every deploy, every restart and every crash-loop iteration - issued
# CREATE TABLE and an INSERT against the production database, silently, as
# a side effect of an import. Schema changes on a live system should be a
# thing somebody decides to do, with the statements in front of them, not
# a thing that happens because a service came up.
#
# `companies` has always worked this way: db.py creates it only when run as
# a command. Accounts now match that. To initialise:
#
#     .venv/bin/python users.py --init
#
# Until then the service token still signs in (auth.py never touches the
# users table for that path), /health still answers, and the login page
# reports that the account database is unavailable rather than crashing.
import users  # noqa: E402

from admin_routes import admin as _admin_bp  # noqa: E402
app.register_blueprint(_admin_bp)


# --- Wizard onboarding UI ---
from wizard_routes import register_wizard_routes
register_wizard_routes(app)

# --- Company Pipeline API v2 ---
from api_v2 import api_v2
app.register_blueprint(api_v2)

# --- Operator Excel import (upload -> diff -> apply) ---
from import_routes import imports
app.register_blueprint(imports)


API_KEY = os.getenv("COMPANIES_HOUSE_API_KEY")
BASE_URL = "https://api.company-information.service.gov.uk"

# --- Connection pooling with retry ---
_session = http_requests.Session()
_adapter = HTTPAdapter(
    pool_connections=10,
    pool_maxsize=20,
    max_retries=Retry(total=2, backoff_factor=0.3, status_forcelist=[500, 502, 503]),
)
_session.mount("https://", _adapter)

# --- LRU Cache ---
_cache = OrderedDict()
_CACHE_MAX = 500
_CACHE_TTL = 300  # 5 minutes


def _cache_get(key):
    if key in _cache:
        val, ts = _cache[key]
        if time.time() - ts < _CACHE_TTL:
            _cache.move_to_end(key)
            return val
        del _cache[key]
    return None


def _cache_set(key, val):
    _cache[key] = (val, time.time())
    if len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


def ch_get(path, params=None):
    cache_key = f"{path}|{json.dumps(params, sort_keys=True) if params else ''}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    for attempt in range(5):
        resp = _session.get(
            f"{BASE_URL}{path}",
            params=params,
            auth=(API_KEY, ""),
            timeout=15,
        )
        if resp.status_code == 429:
            wait = min(2 ** attempt * 2, 30)
            time.sleep(wait)
            continue
        resp.raise_for_status()
        data = resp.json()
        _cache_set(cache_key, data)
        return data
    # Final attempt failed
    raise Exception(f"Rate limited after 5 retries: {path}")


def ch_get_raw(url, headers=None, stream=False):
    """Raw GET for document downloads (no caching), with 429 backoff."""
    for attempt in range(5):
        resp = _session.get(url, auth=(API_KEY, ""), headers=headers, stream=stream, timeout=30)
        if resp.status_code == 429:
            wait = min(2 ** attempt * 2, 30)
            time.sleep(wait)
            continue
        return resp
    return resp  # return last response even if 429


@app.route("/")
def index():
    return send_from_directory("static", "app.html")


# Officer name search terms per nationality.
# Searched via /search/officers, then trace appointments to companies.
# One company returned per name.
OFFICER_NAME_TERMS = {
    "kenyan": [
        "mwangi", "odhiambo", "moraa", "wekesa", "mutua",
        "lenku", "kinoti", "kiptoo", "wanjiku", "onyango",
        "barasa", "nduku", "cheruiyot", "naserian", "muriithi",
        "mogaka", "achieng", "kamau", "wambua", "kaari",
        "kipchoge", "onsongo", "saitoti", "musyoka", "kerubo",
        "wanjala", "nyakundi", "ochieng", "wafula", "mutegi",
        "naisula", "wangari", "otieno", "lekatoo", "kiprotich",
        "kawira", "wanyonyi", "njoroge", "chebet", "mwikali",
    ],
    "nigerian": [
        "okafor", "adeyemi", "olawale", "adebayo", "akinwumi",
        "nwachukwu", "igwe", "emeka", "chukwu", "obinna",
        "eze", "ogunmola", "babatunde", "adewale", "chinwe",
    ],
    "south african": [
        "nkosi", "dlamini", "ndlovu", "zulu", "mthembu",
        "naidoo", "pillay", "govender",
    ],
    "indian": [
        "patel", "sharma", "gupta", "singh", "kumar", "mehta",
        "joshi", "agarwal", "verma", "reddy", "nair",
    ],
    "ugandan": [
        "mugisha", "tumusiime", "nakamya", "ssemakula",
        "namukwaya", "kayondo", "ssenyonga",
    ],
    "tanzanian": [
        "mchomvu", "massawe", "nyerere", "mkapa",
    ],
    "ghanaian": [
        "asante", "mensah", "owusu", "boateng", "agyemang",
        "ampofo", "nkrumah",
    ],
    "pakistani": [
        "khan", "malik", "hussain", "chaudhry", "butt", "iqbal",
    ],
    "bangladeshi": [
        "rahman", "hossain", "chowdhury", "uddin", "alam",
    ],
}

BATCH_SIZE = 10  # parallel officer checks
MAX_OFFICERS = 3   # only include companies with at most this many active officers


def search_companies_page(query, start_index=0, size=50):
    data = ch_get("/search/companies", {"q": query, "items_per_page": size, "start_index": start_index})
    items = []
    for item in data.get("items", []):
        if item.get("company_status", "") != "active":
            continue
        inc_date = item.get("date_of_creation", "")
        items.append({
            "company_number": item.get("company_number", ""),
            "title": item.get("title", ""),
            "company_status": "active",
            "company_type": item.get("company_type", ""),
            "date_of_creation": inc_date,
            "year": inc_date[:4] if inc_date else "",
            "officer_nationalities": [],
            "address": item.get("address_snippet", ""),
        })
    return items


def find_one_company_for_officer(name_query, nat_lower, seen_companies, year_from=None, year_to=None):
    """Search officers by name, return the FIRST active company (<=MAX_OFFICERS)
    where the officer has the target nationality. Returns (item, matched_officer_name) or None."""
    for start in range(0, 100, 20):
        try:
            data = ch_get("/search/officers", {"q": name_query, "items_per_page": 20, "start_index": start})
        except Exception:
            return None

        officer_items = data.get("items", [])
        if not officer_items:
            return None

        for officer in officer_items:
            appointments_link = officer.get("links", {}).get("self", "")
            if not appointments_link:
                continue

            try:
                appts = ch_get(appointments_link)
            except Exception:
                continue

            for appt in appts.get("items", []):
                nationality = (appt.get("nationality") or "").strip().lower()
                if nat_lower and nat_lower not in nationality:
                    continue

                # Must be current appointment (director)
                if appt.get("resigned_on"):
                    continue

                company = appt.get("appointed_to", {})
                cn = company.get("company_number", "")
                status = (company.get("company_status") or "").lower()
                if not cn or status != "active" or cn in seen_companies:
                    continue

                # Get company profile
                try:
                    profile = ch_get(f"/company/{cn}")
                except Exception:
                    continue

                inc_date = profile.get("date_of_creation", "")
                year = inc_date[:4] if inc_date else ""
                if year_from and year < year_from:
                    continue
                if year_to and year > year_to:
                    continue

                # Count active officers and collect nationalities
                nats = set()
                active_count = 0
                matched_name = officer.get("title", name_query)
                try:
                    officers_data = ch_get(f"/company/{cn}/officers")
                    for o in officers_data.get("items", []):
                        if not o.get("resigned_on"):
                            active_count += 1
                        n = (o.get("nationality") or "").strip()
                        if n:
                            nats.add(n)
                except Exception:
                    nats.add(appt.get("nationality", ""))
                    active_count = 1

                if active_count > MAX_OFFICERS:
                    continue

                addr = profile.get("registered_office_address", {})
                if isinstance(addr, dict):
                    addr_parts = [addr.get(k, "") for k in ["address_line_1", "address_line_2", "locality", "postal_code", "country"] if addr.get(k)]
                    addr = ", ".join(addr_parts)

                return {
                    "company_number": cn,
                    "title": profile.get("company_name", company.get("company_name", "")),
                    "company_status": "active",
                    "company_type": profile.get("type", ""),
                    "date_of_creation": inc_date,
                    "year": year,
                    "officer_nationalities": sorted(nats),
                    "officer_count": active_count,
                    "address": addr,
                    "search_name": name_query.title(),
                    "matched_officer": matched_name,
                }

    return None


def check_officers(item, nat_lower):
    cn = item["company_number"]
    try:
        officers = ch_get(f"/company/{cn}/officers")
        nats = set()
        active_count = 0
        for o in officers.get("items", []):
            if not o.get("resigned_on"):
                active_count += 1
            n = (o.get("nationality") or "").strip()
            if n:
                nats.add(n)
        # Filter: skip companies with more than MAX_OFFICERS active officers
        if active_count > MAX_OFFICERS:
            return None
        item["officer_nationalities"] = sorted(nats)
        item["officer_count"] = active_count
        if any(nat_lower in n.lower() for n in nats):
            return item
    except Exception:
        pass
    return None


def generate_candidates(query, nationality, year_from, year_to):
    """Yield unique active companies - one per officer name.

    For each name in OFFICER_NAME_TERMS, find one company with an active
    director of that name and the target nationality (<=MAX_OFFICERS).
    Yields (item, pre_verified) tuples.
    """
    seen = set()

    # Phase 1: user query (company name search)
    if query:
        try:
            for item in search_companies_page(query, size=50):
                cn = item["company_number"]
                if cn not in seen:
                    seen.add(cn)
                    yield (item, False)
        except Exception:
            pass

    # Phase 2: one company per officer name (primary strategy)
    if nationality:
        nat_lower = nationality.lower()
        officer_terms = OFFICER_NAME_TERMS.get(nat_lower, [])
        for name in officer_terms:
            result = find_one_company_for_officer(name, nat_lower, seen, year_from, year_to)
            if result:
                seen.add(result["company_number"])
                yield (result, True)
            else:
                # Signal that this name was tried but no match found
                yield ({"search_name": name.title(), "_skip": True}, True)


@app.route("/api/search/stream")
def api_search_stream():
    query = request.args.get("q", "").strip()
    year_from = request.args.get("year_from", "").strip() or None
    year_to = request.args.get("year_to", "").strip() or None
    nationality = request.args.get("nationality", "").strip()

    def generate():
        if not query and not nationality:
            yield sse({"done": True, "total": 0})
            return

        matched = 0
        names_checked = 0
        nat_lower = nationality.lower() if nationality else ""
        total_names = len(OFFICER_NAME_TERMS.get(nat_lower, [])) if nat_lower else 0

        yield sse({"status": "Searching...", "total_names": total_names})

        candidate_gen = generate_candidates(query, nationality, year_from, year_to)
        batch = []

        for item, pre_verified in candidate_gen:
            if pre_verified:
                if item.get("_skip"):
                    # Name tried, no match
                    names_checked += 1
                    yield sse({"skip": item["search_name"], "matched": matched, "names_checked": names_checked, "total_names": total_names})
                    continue
                # Officer name search - already verified
                matched += 1
                names_checked += 1
                yield sse({"item": item, "matched": matched, "names_checked": names_checked, "total_names": total_names})
                continue

            if not nationality:
                matched += 1
                yield sse({"item": item, "matched": matched, "names_checked": 0, "total_names": 0})
                continue

            # Phase 1 candidates need officer checking
            batch.append(item)
            if len(batch) >= BATCH_SIZE:
                results = _check_batch(batch, nat_lower)
                for checked_item, is_match in results:
                    if is_match:
                        matched += 1
                        yield sse({"item": checked_item, "matched": matched, "names_checked": names_checked, "total_names": total_names})
                batch = []

        # Remaining batch
        if batch and nationality:
            results = _check_batch(batch, nat_lower)
            for checked_item, is_match in results:
                if is_match:
                    matched += 1
                    yield sse({"item": checked_item, "matched": matched, "names_checked": names_checked, "total_names": total_names})

        # Report any names that had no match
        if nat_lower:
            officer_terms = OFFICER_NAME_TERMS.get(nat_lower, [])
            remaining = total_names - names_checked
            if remaining > 0:
                yield sse({"progress": True, "matched": matched, "names_checked": total_names, "total_names": total_names, "note": f"{remaining} names had no matching company"})

        yield sse({"done": True, "total": matched, "names_checked": names_checked, "total_names": total_names})

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _check_batch(batch, nat_lower):
    """Check a batch of companies for officer nationality in parallel. Returns list of (item, is_match)."""
    results = []
    with ThreadPoolExecutor(max_workers=BATCH_SIZE) as pool:
        futures = [(pool.submit(check_officers, item, nat_lower), item) for item in batch]
        for future, item in futures:
            result = future.result()
            results.append((result if result else item, result is not None))
    return results


def sse(data):
    return f"data: {json.dumps(data)}\n\n"


# --- Company detail endpoints ---

@app.route("/api/company/<company_number>")
def api_company(company_number):
    try:
        return jsonify(ch_get(f"/company/{company_number}"))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/company/<company_number>/officers")
def api_officers(company_number):
    try:
        return jsonify(ch_get(f"/company/{company_number}/officers"))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/company/<company_number>/pscs")
def api_pscs(company_number):
    try:
        return jsonify(ch_get(f"/company/{company_number}/persons-with-significant-control"))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/company/<company_number>/filings")
def api_filings(company_number):
    try:
        return jsonify(ch_get(f"/company/{company_number}/filing-history", {"items_per_page": 50}))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/company/<company_number>/incorporation-certificate")
def api_incorporation_certificate(company_number):
    try:
        data = ch_get(f"/company/{company_number}/filing-history", {"items_per_page": 100})

        # Strategy 1: Look for incorporation filing
        doc_link = None
        for item in data.get("items", []):
            cat = (item.get("category") or "").lower()
            desc = (item.get("description") or "").lower()
            ftype = (item.get("type") or "").upper()
            if cat == "incorporation" or ftype == "NEWINC" or "incorporat" in desc:
                doc_link = item.get("links", {}).get("document_metadata")
                if doc_link:
                    break

        # Strategy 2: For foreign companies (FC/SF) or older companies, get the earliest filing with a document
        if not doc_link:
            filings_with_docs = [
                item for item in data.get("items", [])
                if item.get("links", {}).get("document_metadata")
            ]
            if filings_with_docs:
                # Earliest filing (last in list)
                doc_link = filings_with_docs[-1].get("links", {}).get("document_metadata")

        if not doc_link:
            return jsonify({"error": "No documents available for this company"}), 404

        # Stream the PDF
        content_url = f"{doc_link}/content"
        resp = ch_get_raw(content_url, headers={"Accept": "application/pdf"}, stream=True)
        resp.raise_for_status()
        return Response(
            resp.iter_content(chunk_size=8192),
            content_type="application/pdf",
            headers={
                "Content-Disposition": f"attachment; filename=Certificate_of_Incorporation_{company_number}.pdf"
            },
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- DUNS Automation endpoints ---

from duns_automation import (
    initiate_duns_request,
    check_duns_emails,
    get_all_duns_requests,
    get_duns_request_detail,
    get_company_data_for_duns,
    manually_set_duns,
    stealth_lookup_duns,
    stealth_request_duns,
    stealth_apply_duns,
    stealth_full_pipeline,
    sync_duns_to_company_row,
)


def _attach_row_sync(result, company_number):
    """Copy the DUNS store's findings into the company row, and say if it failed.

    duns_automation keeps its own store and never wrote the companies table,
    so the temp email it creates, the request status and the DUNS number
    itself never reached the row - "DUNS Email Used" could only be typed by
    hand. Reported rather than raised: the DUNS record is already saved, and
    swallowing the failure is how a column goes quietly empty.
    """
    try:
        result["row_updated"] = sorted(sync_duns_to_company_row(company_number))
    except Exception as e:
        result["row_sync_error"] = str(e)
    return result


@app.route("/api/duns/initiate/<company_number>", methods=["POST"])
def api_duns_initiate(company_number):
    """Initiate DUNS number retrieval for a company."""
    try:
        phone = ""
        if request.is_json:
            phone = request.json.get("phone", "")
        result = initiate_duns_request(company_number, phone_number=phone)
        _attach_row_sync(result, company_number)
        status_code = 200 if result.get("status") != "error" else 400
        return jsonify(result), status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/duns/<company_number>/check-email")
def api_duns_check_email(company_number):
    """Check temp email for DUNS communications."""
    try:
        result = check_duns_emails(company_number)
        _attach_row_sync(result, company_number)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/duns/<company_number>/detail")
def api_duns_detail(company_number):
    """Get full DUNS request details for a company."""
    try:
        result = get_duns_request_detail(company_number)
        if result is None:
            return jsonify({"error": "No DUNS request found"}), 404
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/duns/<company_number>/set-duns", methods=["POST"])
def api_duns_set(company_number):
    """Manually set DUNS number for a company."""
    try:
        data = request.get_json()
        if not data or not data.get("duns_number"):
            return jsonify({"error": "duns_number required"}), 400
        result = manually_set_duns(company_number, data["duns_number"])
        _attach_row_sync(result, company_number)
        if "error" in result:
            return jsonify(result), 400
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/duns/all")
def api_duns_all():
    """Get summary of all DUNS requests."""
    try:
        return jsonify(get_all_duns_requests())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/duns/<company_number>/company-data")
def api_duns_company_data(company_number):
    """Get pre-extracted company data formatted for DUNS application."""
    try:
        result = get_company_data_for_duns(company_number)
        if "error" in result:
            return jsonify(result), 400
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/duns/<company_number>/stealth-lookup", methods=["POST"])
def api_duns_stealth_lookup(company_number):
    """Stealth browser lookup for DUNS via D&B UK site."""
    try:
        data = request.get_json(silent=True) or {}
        headless = data.get("headless", True)

        # Get company data from CH to have name/address for fallback search
        company_data = get_company_data_for_duns(company_number)
        company_name = company_data.get("legal_name", "") if "error" not in company_data else ""
        addr = company_data.get("address", {}) if "error" not in company_data else {}

        # Email/name can come from request body or existing tracking record
        email = data.get("email", "")
        first_name = data.get("first_name", "")
        last_name = data.get("last_name", "")
        if not email:
            from duns_automation import _load_duns_db
            db = _load_duns_db()
            if company_number in db:
                email = db[company_number].get("temp_email", {}).get("address", "")

        result = stealth_lookup_duns(
            company_number=company_number,
            company_name=company_name,
            post_town=addr.get("locality", ""),
            post_code=addr.get("postal_code", ""),
            email_address=email,
            first_name=first_name,
            last_name=last_name,
            headless=headless,
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/duns/<company_number>/stealth-request", methods=["POST"])
def api_duns_stealth_request(company_number):
    """Full automated DUNS request: creates temp email, searches D&B, submits form, polls for DUNS email."""
    try:
        data = request.get_json(silent=True) or {}
        headless = data.get("headless", True)
        result = stealth_request_duns(company_number, headless=headless)
        status_code = 200 if result.get("status") != "error" else 400
        return jsonify(result), status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/duns/<company_number>/stealth-apply", methods=["POST"])
def api_duns_stealth_apply(company_number):
    """Stealth browser DUNS application via the D&B developer flow."""
    try:
        data = request.get_json(silent=True) or {}
        headless = data.get("headless", True)

        # Get company data and tracking record
        company_data = get_company_data_for_duns(company_number)
        if "error" in company_data:
            return jsonify(company_data), 400

        # Get email from tracking DB or request body
        email = data.get("email", "")
        if not email:
            from duns_automation import _load_duns_db
            db = _load_duns_db()
            if company_number in db:
                email = db[company_number].get("temp_email", {}).get("address", "")

        if not email:
            return jsonify({"error": "No email available. Initiate DUNS request first or provide 'email' in body."}), 400

        result = stealth_apply_duns(
            company_data=company_data,
            email_address=email,
            headless=headless,
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/duns/<company_number>/stealth-full", methods=["POST"])
def api_duns_stealth_full(company_number):
    """Full stealth pipeline: lookup then prepare application if not found."""
    try:
        data = request.get_json(silent=True) or {}
        headless = data.get("headless", True)

        # Get company data
        company_data = get_company_data_for_duns(company_number)
        if "error" in company_data:
            return jsonify(company_data), 400

        addr = company_data.get("address", {})

        # Get email from tracking DB
        email = data.get("email", "")
        if not email:
            from duns_automation import _load_duns_db
            db = _load_duns_db()
            if company_number in db:
                email = db[company_number].get("temp_email", {}).get("address", "")

        result = stealth_full_pipeline(
            company_number=company_number,
            company_name=company_data.get("legal_name", ""),
            post_town=addr.get("locality", ""),
            post_code=addr.get("postal_code", ""),
            company_data=company_data,
            email_address=email,
            headless=headless,
        )

        # Auto-store if found
        if result.get("duns_number"):
            # _put_duns_record, not _save_duns_db: it re-reads under the lock
            # and replaces only this company's key, so a snapshot taken
            # before another company's long browser run cannot erase it.
            from duns_automation import _load_duns_db, _put_duns_record
            db = _load_duns_db()
            if company_number in db:
                db[company_number]["duns_number"] = result["duns_number"]
                db[company_number]["status"] = "completed"
                db[company_number]["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                db[company_number]["completed_via"] = "stealth_full_pipeline"
                _put_duns_record(company_number, db[company_number])

        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    import os
    start_port = int(os.getenv("PORT", 5000))
    max_tries = 10

    for p in range(start_port, start_port + max_tries):
        try:
            print(f"Attempting to start server on port {p}...")
            app.run(debug=True, port=p, threaded=True)
            break
        except OSError as e:
            print(f"Failed to bind to port {p}: {e}")
            # try next port
    else:
        raise SystemExit(f"Could not bind to any port in range {start_port}-{start_port+max_tries-1}")
