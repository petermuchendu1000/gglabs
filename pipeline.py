"""
Master pipeline orchestrator.

Stages (run in order, all idempotent):
  1. check  — verify public IP is whitelisted in Namecheap
  2. match  — match registered Namecheap domains to companies in Excel
  3. email  — create dev@domain forwarding for every company domain
  4. build  — generate brand overlays, build APK/AAB, generate all assets

Usage:
    python pipeline.py                          # Run all stages
    python pipeline.py --stage match            # Single stage
    python pipeline.py --stage build            # Build only
    python pipeline.py --company 02591663       # Build one company
    python pipeline.py --skip-build             # Regenerate assets only (no Gradle)
    python pipeline.py --stage build --artifact aab  # AAB only
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent

import db  # Supabase (PostgreSQL) data-access layer — source of truth

# ── helpers ─────────────────────────────────────────────────────────────────

COMPANIES_DIR = BASE_DIR / "pipeline_output" / "companies"


def _co_dir(cn: str, name: str) -> Path:
    """The output directory for one company. One definition, used by every stage.

    This MUST stay character-identical to apps/generator/generate.py:107-108,
    which is the code that actually creates the directory. Same character
    class, same strip, same fallback to the bare company number when the name
    is empty or whitespace. Verified equal across an empty name, a
    whitespace-only name, every stripped character and non-ASCII.

    generate._co_dir returns this path with "app" appended; this returns the
    parent, because stage_director_id writes director_id/ as a sibling of app/.

    It exists because the path was previously computed in one place and read
    from another: stage_email looked under pipeline_output/apps/<cn>/ and
    print_summary iterated pipeline_output/apps/, which nothing has ever
    written. Both were silently dead - a build that produced signed APKs and
    AABs still printed "No pipeline_output/apps directory found."
    """
    safe = re.sub(r'[\\/:*?"<>|]', "", name).strip()
    folder = f"{cn} - {safe}" if safe else cn
    return COMPANIES_DIR / folder


def _banner(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def _load_env() -> None:
    env = BASE_DIR / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


# ── Stage 1: IP check ────────────────────────────────────────────────────────

def stage_check() -> None:
    _banner("STAGE 1 — IP check")
    from check_ip import ensure_ip_whitelisted
    ensure_ip_whitelisted(verbose=True)
    print("  Namecheap API: OK")


# ── Stage 2: Domain matching ─────────────────────────────────────────────────

def stage_match(dry_run: bool = False) -> None:
    _banner("STAGE 2 — Domain matching")
    import requests
    import xml.etree.ElementTree as ET
    import re

    API_URL = "https://api.namecheap.com/xml.response"
    NS = {"nc": "http://api.namecheap.com/xml.response"}

    _STOPWORDS = {
        "ltd", "limited", "plc", "cic", "llc", "uk", "the", "and", "of",
        "for", "services", "group", "solutions", "holdings", "enterprises",
        "international", "global", "management", "consulting", "associates",
    }

    def _normalize(s: str) -> str:
        s = s.lower()
        s = re.sub(r"[^a-z0-9 ]", " ", s)
        words = [w for w in s.split() if w not in _STOPWORDS and len(w) > 1]
        return " ".join(words)

    def _domain_core(domain: str) -> str:
        sld = domain.lower().split(".")[0]
        return re.sub(r"[^a-z0-9]", "", sld)

    # A score, and WHICH RULE produced it. The rule matters as much as the
    # number, because the two are not comparable.
    #
    # STRONG means the domain spells the company: the whole domain core is
    # inside the company name, or nearly all of it in order.
    # WEAK means only that the two share a word, which is a different claim
    # and a far smaller one.
    #
    # The word-overlap rule divides by len(dom_words) alone, so a domain
    # whose only alphabetic run is one common noun scores the maximum:
    # "33traders.com" gives dom_words={"traders"}, and ANY company with
    # "traders" in its name scores 1/1 = 1.00 — beating the 0.90 an exact
    # substring earns. Measured: 33traders.com took TAMU TRADERS LTD at 1.00
    # while tamutraders.com, the domain that actually spells it, scored 0.90
    # and was then refused because the company was no longer free.
    #
    # Rescaling the weak tier alone does not fix that. Domains are processed
    # in the order the API returns them, so an early weak match still claims
    # a company before a later strong one is reached. The tier has to travel
    # with the score so the caller can rank by it and refuse to write on weak
    # evidence alone.
    STRONG, WEAK = "strong", "weak"

    def _score(company_norm: str, dom_core: str) -> tuple[float, str]:
        if not dom_core:
            return 0.0, WEAK
        company_core = company_norm.replace(" ", "")
        if dom_core in company_core:
            return 0.9, STRONG
        if len(dom_core) >= 7 and all(c in company_core for c in dom_core):
            pos = 0; seq = 0
            for ch in dom_core:
                idx = company_core.find(ch, pos)
                if idx >= 0:
                    seq += 1; pos = idx + 1
            if seq / len(dom_core) >= 0.85:
                return seq / len(dom_core), STRONG
        company_words = set(company_norm.split())
        dom_words = set(re.findall(r"[a-z]{3,}", dom_core))
        if dom_words:
            # Symmetric. The old denominator was len(dom_words) alone, which
            # asks only "is the domain explained by the company?" and never
            # "does the company have words the domain does not account for?".
            # {"traders"} against {"tamu","traders"} answered 1/1 = 1.00 while
            # leaving "tamu" — the only part that says WHICH trader — unmatched.
            # Over the union it is 1/2 = 0.50.
            shared = company_words & dom_words
            overlap = len(shared) / len(company_words | dom_words)
            if overlap >= 0.5:
                # Equal word sets mean the domain and the name are made of the
                # same words, with nothing left over on either side
                # ("acme2widgets" vs "ACME WIDGETS LTD"). Anything less is a
                # partial overlap: real evidence, but not enough to write on.
                tier = STRONG if company_words == dom_words else WEAK
                return overlap, tier
        return 0.0, WEAK

    base = {
        "ApiUser":  os.environ["NAMECHEAP_API_USER"],
        "ApiKey":   os.environ["NAMECHEAP_API_KEY"],
        "UserName": os.environ["NAMECHEAP_USERNAME"],
        "ClientIp": os.environ["NAMECHEAP_CLIENT_IP"],
    }

    r = requests.get(API_URL, params={**base, "Command": "namecheap.domains.getList", "PageSize": "100"}, timeout=30)
    root = ET.fromstring(r.text)
    # Status was not checked here, and an error response carries no Domain
    # elements — so a rejected call produced an empty list, and the stage went
    # on to print "Namecheap domains: []" and "0 new domain(s) written",
    # which reads exactly like an account with nothing left to match.
    # match_domains.py:118 raises on the same condition for the same call.
    if root.get("Status") == "ERROR":
        errs = [e.text for e in root.findall(".//nc:Error", NS)]
        raise RuntimeError(f"Namecheap API error: {errs}")
    domains = [d.get("Name") for d in root.findall(".//nc:Domain", NS) if d.get("Name")]
    print(f"  Namecheap domains: {domains}")

    # Load companies from Supabase
    companies = []   # (cn, name, existing_domain)
    for _r in db.all_rows():
        cn   = str(_r.get("Company Number") or "").strip()
        name = str(_r.get("Company Name") or "").strip()
        dom  = str(_r.get("Domain") or "").strip()
        if cn and name:
            companies.append((cn, name, dom))

    # How many rows were actually compared, and how many are still free to
    # take a domain. Without this the stage is unreadable in the one case
    # that matters: when the list is empty the inner loop never runs,
    # best_score stays 0.0, and EVERY domain prints "best score=0.00" — a run
    # with nothing to compare against is byte-identical to a run where
    # nothing matched. That is the same fault as stage 1 printing OK over a
    # refused call, and it was observed on production: 35 domains, 35 zeros,
    # and no way to tell which had happened.
    free = sum(1 for _, _, d in companies if not d)
    print(f"  Companies loaded: {len(companies)} "
          f"({free} without a domain, {len(companies) - free} already set)")
    if not companies:
        print("  NOTHING TO MATCH AGAINST — the companies table returned no "
              "usable row\n"
              "  (a row needs both a company number and a company name). "
              "Every score\n"
              "  below is 0.00 for that reason, not because the domains "
              "failed to match.")

    written = review = 0
    for domain in domains:
        dom_core   = _domain_core(domain)
        best_score = 0.0
        best_tier  = WEAK
        best_rank  = (0, 0.0)
        best_cn    = ""
        best_name  = ""
        for cn, name, existing in companies:
            score, tier = _score(_normalize(name), dom_core)
            # Rank on (tier, score), not score alone. Ranking on the number
            # by itself lets a weak 1.00 beat a strong 0.90, which is how a
            # company that merely shares a word outranks the one the domain
            # actually spells.
            rank = (1 if tier == STRONG else 0, score)
            if rank > best_rank:
                best_rank = rank
                best_score = score; best_tier = tier
                best_cn = cn; best_name = name

        if best_score >= 0.5 and best_tier == WEAK:
            # Shares a word, nothing more. Said out loud and left for a human,
            # because `domain` is a money column (db.py:546) and the evidence
            # does not carry the weight of a write.
            print(f"  REVIEW {domain!r:42s} -> {best_cn} {best_name!r}  "
                  f"(shares a word only, score={best_score:.2f}; not written)")
            review += 1
        elif best_score >= 0.5:
            print(f"  MATCH  {domain!r:42s} -> {best_cn} {best_name!r}  (score={best_score:.2f})")
            if not dry_run:
                for cn, name, existing in companies:
                    if cn == best_cn and not existing:
                        db.update_row(cn, {"Domain": domain})
                        # Update in-memory list too so next iteration sees it
                        companies = [
                            (c, n, domain if c == best_cn else d)
                            for c, n, d in companies
                        ]
                        written += 1
                        break
                else:
                    # The loop found no company that both matches best_cn and
                    # is free, so nothing was written - but MATCH was already
                    # printed above, and without this the only trace was MATCH
                    # lines outnumbering the "N new domain(s)" tally.
                    # match_domains.py:211 prints this; the live path did not.
                    #
                    # best_name, not the loop's `name`: after a for loop runs
                    # to completion its variables hold the LAST iterated
                    # company, which is not the winner. The held domain is
                    # looked up by best_cn for the same reason.
                    held = next((d for c, n, d in companies if c == best_cn), "")
                    print(f"  Skipped {domain} -> {best_name} "
                          f"(already has: {held})")
        else:
            print(f"  SKIP   {domain!r:42s} (best score={best_score:.2f})")

    # The REVIEW tally is reported in both modes. A dry run that hides it
    # would under-report what a real run leaves undone, and an operator who
    # never sees the count has no reason to go and look at the lines.
    tail = (f" {review} left for review (shared-word matches are not written)."
            if review else "")
    if dry_run:
        print(f"\n  [Dry run — not writing]{tail}")
    else:
        print(f"\n  {written} new domain(s) written to Supabase.{tail}")


# ── Stage 3: Dev email setup ─────────────────────────────────────────────────

def stage_email(dry_run: bool = False) -> None:
    _banner("STAGE 3 — dev@ email routing (Cloudflare)")
    import cloudflare_dns

    # Load companies with a domain + assigned email from Supabase. The name
    # is carried too: the manifest lives under _co_dir(cn, name), so without
    # it the manifest update below cannot find the directory the build wrote.
    companies = []
    for r in db.all_rows():
        cn     = str(r.get("Company Number") or "").strip()
        name   = str(r.get("Company Name") or "").strip()
        domain = str(r.get("Domain") or "").strip()
        email  = str(r.get("Assigned Email") or "").strip()
        if cn and domain and email:
            companies.append((cn, name, domain, email))

    print(f"  Companies with domains: {len(companies)}")

    # Counted so a partially completed run is legible. There was no summary
    # at all before, so a run that stopped halfway was indistinguishable from
    # one that did nothing.
    written = skipped = failed = 0

    # A Worker route needs no destination verification; a forward needs a
    # human to click a link in each company's own mailbox. See
    # cloudflare_dns.ensure_dev_route.
    worker = os.getenv("CLOUDFLARE_EMAIL_WORKER", "").strip()

    for cn, co_name, domain, assigned_email in companies:
        dev_email = f"dev@{domain}"
        print(f"\n  [{cn}] {domain}")

        # This used to be ~80 lines of inline Namecheap XML: read the whole
        # forwarding set, merge dev@ into it, post the whole set back. That
        # shape was dangerous on its own terms - setEmailForwarding REPLACES
        # the set, so any entry that failed to read was deleted, and the code
        # had to refuse domains with a malformed entry rather than risk
        # destroying someone's mail routing.
        #
        # Cloudflare creates routing rules INDIVIDUALLY. There is no set to
        # read back, nothing to merge, and no way for this call to remove a
        # rule it did not create - ensure_dev_route only ever matches a rule
        # whose matcher is exactly dev@<this domain>. The entire read-merge-
        # replace hazard, and the refusal that guarded it, are gone because
        # the operation that caused them is gone.
        #
        # Namecheap is not dropped from the pipeline, only from DNS and mail:
        # it remains the registrar that buys the domain and sets its
        # nameservers.
        route = cloudflare_dns.ensure_dev_route(
            domain,
            worker=worker or None,
            forward_to=None if worker else assigned_email,
            dry_run=dry_run,
        )
        status = str(route.get("status") or "unknown")
        target = route.get("target") or assigned_email

        if status == "dry_run":
            print(f"    DRY — would {route.get('would')} {dev_email} -> {target}")
        elif status == "configured":
            if route.get("created"):
                written += 1
                print(f"    SET — {dev_email} -> {target}")
            else:
                print(f"    OK  — {dev_email} already routed to {target}")
        elif status == "not_delegated":
            # The domain is still on its registrar's DNS. Not an error: the
            # delegate stage has not reached it yet.
            skipped += 1
            print(f"    SKIP — {route.get('message') or 'not delegated'}")
            continue
        else:
            # error, pending_verification, routing_not_ready. Each is a
            # different operator action, so the status is printed as well as
            # the reason.
            failed += 1
            print(f"    ERROR — {status}: "
                  f"{route.get('message') or route.get('error') or 'no reason given'}")
            continue

        # Update the manifest the build actually wrote. This read
        # pipeline_output/apps/<cn>/manifest.json, which nothing has ever
        # written - assets.py:858 writes _co_dir(cn, name)/app/manifest.json -
        # so the whole block was unreachable and support_email was never
        # corrected, silently, including the else branch below.
        manifest_path = _co_dir(cn, co_name) / "app" / "manifest.json"
        if manifest_path.exists():
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            if data.get("support_email") != dev_email:
                if dry_run:
                    print(f"    DRY — manifest support_email: {data.get('support_email')!r} -> {dev_email!r}")
                else:
                    data["support_email"] = dev_email
                    manifest_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                    print(f"    UPD — manifest support_email -> {dev_email}")
            else:
                print(f"    OK  — manifest already has {dev_email}")

    if dry_run:
        print(f"\n  [Dry run — nothing written] {len(companies)} considered, "
              f"{skipped} skipped, {failed} failed")
    else:
        print(f"\n  {written} route(s) created, {skipped} skipped, "
              f"{failed} failed, of {len(companies)} considered.")


# ── Stage 4: Director ID (ibenty) ───────────────────────────────────────────

def _ibenty_credentials() -> tuple[str, str]:
    """The ibenty login, preferring the current variable names.

    The service was called xbinder and the variables kept that name for a
    while, so a .env written before the rename holds XBINDER_EMAIL and
    XBINDER_PASSWORD. Reading only the new names would turn a configured
    box into one that prints "SKIP" and does nothing - a silent regression
    on the one stage that touches a person's identity documents.

    The old names are read second, never first, and using them says so, so
    the deprecation is visible rather than permanent.
    """
    email = os.environ.get("IBENTY_EMAIL", "").strip()
    password = os.environ.get("IBENTY_PASSWORD", "").strip()
    if email and password:
        return email, password

    old_email = os.environ.get("XBINDER_EMAIL", "").strip()
    old_password = os.environ.get("XBINDER_PASSWORD", "").strip()
    if old_email and old_password:
        print("  NOTE — using XBINDER_EMAIL / XBINDER_PASSWORD. The service is "
              "now ibenty;\n         rename them to IBENTY_EMAIL / "
              "IBENTY_PASSWORD (deploy/set_ibenty_env.sh does it).")
        return old_email, old_password

    # Partial configuration is worth naming: "set both" is unhelpful when
    # one of the two is already there.
    have = [n for n, v in (("IBENTY_EMAIL", email), ("IBENTY_PASSWORD", password),
                           ("XBINDER_EMAIL", old_email),
                           ("XBINDER_PASSWORD", old_password)) if v]
    if have:
        print(f"  SKIP — only {', '.join(have)} is set; both an email and a "
              f"password are required.")
    else:
        print("  SKIP — set IBENTY_EMAIL and IBENTY_PASSWORD in .env")
    return "", ""


def stage_director_id(
    company_numbers: list[str] | None = None,
    dry_run: bool = False,
    headless: bool = True,
) -> None:
    _banner("STAGE 4 — Director ID (ibenty)")

    ibenty_email, ibenty_password = _ibenty_credentials()
    if not ibenty_email or not ibenty_password:
        return

    import requests as req_lib
    sys.path.insert(0, str(BASE_DIR))
    from ibenty_auto import (
        login, create_bind, search_bind, download_id_via_ui,
        split_combined_id_image, load_director_profile,
    )

    CH_BASE       = "https://api.company-information.service.gov.uk"
    ch_key        = os.environ.get("COMPANIES_HOUSE_API_KEY", "")

    # COMPANIES_DIR and _co_dir are module level now; this stage had its own
    # copy of both, which is how the other stages came to disagree with it.

    # ── Load director candidates from Supabase ──────────────────────────────
    candidates: list[tuple[str, str, str, str, str]] = []  # (cn, director_name, dob_str, director_gender, co_name)
    for _row in db.all_rows():
        cn = str(_row.get("Company Number") or "").strip()
        if not cn:
            continue
        if cn.isdigit() and len(cn) < 8:
            cn = cn.zfill(8)
        if company_numbers and cn not in company_numbers:
            continue
        co_name  = str(_row.get("Company Name") or "").strip()
        id_dir   = _co_dir(cn, co_name) / "director_id"
        if (id_dir / "ID Front.jpeg").exists():
            print(f"  [{cn}] OK — ID already downloaded")
            continue

        dirs = str(_row.get("Directors") or "").strip()
        nats = str(_row.get("Director Nationalities") or "").strip()
        dobs = str(_row.get("Director DOB") or "").strip()
        gens = str(_row.get("Director Gender") or "").strip()

        if not dirs:
            continue

        # Pick the first Kenyan director
        dir_list  = [d.strip() for d in dirs.split(";") if d.strip()]
        nat_list  = [n.strip() for n in nats.split(";")]
        dob_list  = [d.strip() for d in dobs.split(";")]
        gen_list  = [g.strip() for g in gens.split(";")]

        director_name   = ""
        director_dob    = ""
        director_gender = ""
        for idx, nat in enumerate(nat_list):
            if "kenyan" in nat.lower():
                director_name   = dir_list[idx] if idx < len(dir_list) else ""
                director_dob    = dob_list[idx] if idx < len(dob_list) else ""
                director_gender = gen_list[idx] if idx < len(gen_list) else ""
                break

        if not director_name:
            continue  # no Kenyan director

        candidates.append((cn, director_name, director_dob, director_gender, co_name))

    # ── Backfill DOB from CH API for any candidate missing it ────────────────
    if not ch_key and any(not dob for _, _, dob, _, _ in candidates):
        print("  WARN — COMPANIES_HOUSE_API_KEY not set; DOB will be randomised for missing entries")

    session_ch = req_lib.Session()
    filled: list[tuple[str, str, str, str, str]] = []
    for cn, director_name, director_dob, director_gender, co_name in candidates:
        if director_dob:
            filled.append((cn, director_name, director_dob, director_gender, co_name))
            continue
        # DOB missing — query CH API, match by surname
        dob_str = ""
        if ch_key:
            try:
                resp = session_ch.get(
                    f"{CH_BASE}/company/{cn}/officers",
                    auth=(ch_key, ""),
                    timeout=20,
                )
                if resp.status_code == 200:
                    target_full = director_name.strip().upper()
                    target_surn = target_full.split(",")[0].strip()
                    surn_fallback = ""
                    for o in resp.json().get("items", []):
                        if o.get("resigned_on"):
                            continue
                        if "director" not in (o.get("officer_role", "") or "").lower():
                            continue
                        dob = o.get("date_of_birth") or {}
                        y, m, day = dob.get("year"), dob.get("month"), dob.get("day")
                        if y and m and day:
                            d_val = f"{int(y):04d}-{int(m):02d}-{int(day):02d}"
                        elif y and m:
                            d_val = f"{int(y):04d}-{int(m):02d}"
                        elif y:
                            d_val = f"{int(y):04d}"
                        else:
                            d_val = ""
                        ch_full = (o.get("name") or "").strip().upper()
                        ch_surn = ch_full.split(",")[0].strip()
                        if ch_full == target_full:
                            dob_str = d_val
                            break
                        if ch_surn == target_surn and not surn_fallback:
                            surn_fallback = d_val
                    if not dob_str:
                        dob_str = surn_fallback
            except Exception as e:
                print(f"  [{cn}] CH API DOB lookup error: {e}")
        filled.append((cn, director_name, dob_str, director_gender, co_name))
        if dob_str:
            print(f"  [{cn}] DOB from CH API: {dob_str}")
        else:
            print(f"  [{cn}] DOB not found — will be randomised by load_director_profile")

    candidates = filled

    if not candidates:
        print("  No companies with Kenyan directors found (or all IDs already downloaded).")
        return

    print(f"\n  {len(candidates)} companies need director IDs")

    if dry_run:
        for cn, name, _, gender, _ in candidates:
            print(f"  [{cn}] DRY — would generate ID for: {name}  gender={gender or '?'}")
        return

    # ── Login to ibenty once ──────────────────────────────────────────────────
    login_session = req_lib.Session()
    try:
        auth    = login(login_session, ibenty_email, ibenty_password)
        token   = auth["token"]
        user_id = auth.get("user_id")
        print("  ibenty login: OK")
    except Exception as e:
        print(f"  ibenty login FAILED: {e}")
        return

    # ── Process each company ──────────────────────────────────────────────────
    for cn, director_name, dob_str, director_gender, co_name in candidates:
        id_dir = _co_dir(cn, co_name) / "director_id"
        if (id_dir / "ID Front.jpeg").exists():
            print(f"  [{cn}] OK — ID already downloaded")
            continue

        # Normalise gender: M/F only; fall back to infer from name if still unknown
        gender_val = director_gender.strip().upper()
        if gender_val not in {"M", "F"}:
            from run_pipeline import _infer_gender as _ig
            gender_val = _ig(director_name)
        if gender_val not in {"M", "F"}:
            gender_val = "M"   # last-resort default

        print(f"\n  [{cn}] Generating ID for: {director_name}  gender={gender_val}")

        # Build profile via temp JSON + load_director_profile (proven pattern)
        profile_data = {
            cn: {
                "company_number": cn,
                "ibenty_profile": {
                    "fname": "",
                    "lname": "",
                    "sname": "",
                    "idnumber": "",
                    "actualdob": dob_str,
                    "gender": gender_val,
                },
                "company_data": {
                    "primary_director": {
                        "name": director_name,
                        "date_of_birth": dob_str,
                    }
                },
            }
        }
        temp_profile = BASE_DIR / "pipeline_output" / f"_tmp_profile_{cn}.json"
        try:
            temp_profile.write_text(json.dumps(profile_data), encoding="utf-8")
            profile = load_director_profile(temp_profile, cn)
        except Exception as e:
            print(f"  [{cn}] profile build FAILED: {e}")
            temp_profile.unlink(missing_ok=True)
            continue
        finally:
            temp_profile.unlink(missing_ok=True)

        # Create bind (new session per bind, Bearer-token authenticated)
        try:
            bind_session = req_lib.Session()
            bind_result = create_bind(bind_session, token, profile)
            bind    = bind_result.get("bind", {})
            bind_id = bind.get("id", "")
            id_no   = bind.get("idNo", profile.get("idnumber", ""))
            print(f"  [{cn}] bind: id={bind_id}  id_no={id_no}")
            print(f"  [{cn}] name: {profile['fname']} {profile['sname']} {profile['lname']}")
        except Exception as e:
            print(f"  [{cn}] create_bind FAILED: {e}")
            continue

        if not bind_id:
            print(f"  [{cn}] SKIP — no bind_id in response")
            continue

        # Search confirms bind is indexed (mirrors ibenty_auto main() flow)
        try:
            search_bind(bind_session, token, id_no[:2] if id_no else "")
        except Exception:
            pass

        # Download via browser UI
        id_dir.mkdir(parents=True, exist_ok=True)
        combined = id_dir / "ID_Combined.jpeg"
        try:
            download_id_via_ui(token, user_id, bind_id, id_no, combined, headless=headless)
        except Exception as e:
            print(f"  [{cn}] download FAILED: {e}")
            continue

        # Split into front/back
        try:
            front_path, back_path = split_combined_id_image(combined)
            print(f"  [{cn}] ID done: {front_path.name} + {back_path.name}")
        except Exception as e:
            print(f"  [{cn}] split FAILED (keeping combined): {e}")
            front_path = combined
            back_path  = combined

        # Persist metadata
        result = {
            "company_number":  cn,
            "director_name":   director_name,
            "full_name_on_id": f"{profile.get('fname','')} {profile.get('sname','')} {profile.get('lname','')}".strip(),
            "id_type":         "National ID",
            "id_country":      "KE",
            "bind_id":         bind_id,
            "id_image_front":  str(front_path),
            "id_image_back":   str(back_path),
            "status":          "downloaded",
        }
        (id_dir / "id_result.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8"
        )
        print(f"  [{cn}] Saved id_result.json")


# ── Stage 5: App build ──────────────────────────────────────────────────────

def stage_build(
    company_numbers: list[str] | None = None,
    skip_build: bool = False,
    artifact: str = "both",
) -> None:
    _banner("STAGE 5 — App build")

    # NO DEFAULT. This used to fall back to one developer's laptop paths:
    #
    #   sdk       = ... or r"C:\Users\LENOVO\AppData\Local\Android\Sdk"
    #   java_home = ... or r"C:\Program Files\Android\Android Studio\jbr"
    #   os.environ["JAVA_HOME"] = java_home
    #
    # Both caused real failures on the server. The sdk default is written
    # into local.properties as sdk.dir, and the Android Gradle Plugin then
    # dies inside getTargetBootClasspath loading a platform from a path that
    # cannot exist on Linux. The java_home line was worse: it SET JAVA_HOME,
    # unconditionally, on every run - so the Windows value appeared in the
    # environment no matter what .env said, and deleting it from .env could
    # never have helped. Two diagnosis cycles went into looking in .env for
    # a value this line was injecting.
    #
    # A default that can only be correct on one machine is not a default. It
    # turns "not configured", which an operator can see and fix, into
    # "configured wrongly", which reads as a bug in the build. Unset now
    # stops the stage and says so.
    #
    # JAVA_HOME is deliberately NOT touched at all. gradlew uses the java on
    # PATH when it is unset, which is correct here, and generate.run_gradle
    # drops a JAVA_HOME that has no bin/java behind it.
    sdk = os.environ.get("ANDROID_HOME", "").strip()
    if not sdk:
        print("  ANDROID_HOME is not set, so the Android SDK cannot be "
              "located and local.properties would be written with a "
              "placeholder.")
        print("  Set it in the service unit and in .env, e.g. "
              "ANDROID_HOME=/opt/android-sdk, then re-run.")
        return

    sys.path.insert(0, str(BASE_DIR))
    from apps.generator.catalog import load_companies
    from apps.generator.generate import (
        build_flavors, run_gradle, collect_artifacts,
        backup_and_make_assets, _ensure_local_properties,
    )

    companies = load_companies(
        company_numbers=company_numbers,
        limit=None,
    )
    # Only build companies that have a domain
    companies = [c for c in companies if c.domain]
    if not companies:
        print("  No companies with domains — nothing to build.")
        return

    print(f"  Building {len(companies)} company app(s):")
    for c in companies:
        print(f"    {c.company_number}  {c.display_name}  ({c.domain})")

    _ensure_local_properties(sdk)

    built = build_flavors(companies)
    print(f"  Wrote {len(built)} flavor overlays + build.gradle.kts")

    if skip_build:
        print("  Gradle: SKIPPED (--skip-build)")
        from apps.generator.generate import collect_artifacts
        got = collect_artifacts(built, variant="release", artifacts=artifact)
        backup_and_make_assets(built, got)
        return

    rc = run_gradle(built, variant="Release", artifact=artifact)
    if rc != 0:
        print(f"  Gradle FAILED (exit {rc})")
        sys.exit(rc)

    got = collect_artifacts(built, variant="release", artifacts=artifact)
    print(f"  Collected {len(got)}/{len(built)} artifact set(s)")

    backup_and_make_assets(built, got)
    print("  Assets generated.")


# ── Summary ──────────────────────────────────────────────────────────────────

def print_summary() -> None:
    _banner("PIPELINE SUMMARY")
    # This iterated pipeline_output/apps/, which nothing has ever written, so
    # a build that produced signed APKs and AABs still printed "No
    # pipeline_output/apps directory found." Three separate paths were wrong:
    # the directory, the artifact paths (the manifest stores them relative to
    # the app/ subdirectory, not to the company directory), and the ID, which
    # stage_director_id writes to <company>/director_id/ and not to
    # pipeline_output/representative_ids/.
    if not COMPANIES_DIR.exists():
        print(f"  No {COMPANIES_DIR.relative_to(BASE_DIR)} directory found.")
        return

    rows = 0
    for co_dir in sorted(COMPANIES_DIR.iterdir()):
        if not co_dir.is_dir():
            continue
        app_dir = co_dir / "app"
        manifest_path = app_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        rows += 1
        m = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        apk_ok  = "OK" if m.get("apk") and (app_dir / m["apk"]).exists() else "--"
        aab_ok  = "OK" if m.get("aab") and (app_dir / m["aab"]).exists() else "--"
        pp_ok   = "OK" if m.get("privacy_policy_url") else "--"
        dom_ok  = "OK" if m.get("domain") else "--"
        id_ok   = "OK" if (co_dir / "director_id" / "ID Front.jpeg").exists() else "--"
        email   = m.get("support_email", "")
        print(f"  {co_dir.name}  APK:{apk_ok}  AAB:{aab_ok}  Policy:{pp_ok}  "
              f"Domain:{dom_ok}  ID:{id_ok}  email={email}")

    if rows == 0:
        print(f"  {COMPANIES_DIR.relative_to(BASE_DIR)} exists but holds no "
              f"manifest.json; has the build stage run?")


# ── Entry point ──────────────────────────────────────────────────────────────

STAGES = ["check", "match", "email", "director_id", "build"]


def main() -> None:
    p = argparse.ArgumentParser(description="Company app pipeline")
    p.add_argument("--stage",       choices=STAGES, help="Run a single stage only")
    p.add_argument("--company",     nargs="+", metavar="CN", help="Limit build to these company numbers")
    p.add_argument("--skip-build",  action="store_true", help="Skip Gradle (regenerate assets only)")
    p.add_argument("--artifact",    default="both", choices=["apk", "aab", "both"])
    p.add_argument("--dry-run",     action="store_true", help="Preview changes without writing")
    p.add_argument("--no-headless", action="store_true", help="Show browser window during the ibenty download (debug)")
    args = p.parse_args()

    _load_env()

    run = args.stage or "all"
    t0  = time.time()

    if run in ("all", "check"):
        stage_check()

    if run in ("all", "match"):
        stage_match(dry_run=args.dry_run)

    if run in ("all", "email"):
        stage_email(dry_run=args.dry_run)

    if run in ("all", "director_id"):
        stage_director_id(
            company_numbers=args.company,
            dry_run=args.dry_run,
            headless=not args.no_headless,
        )

    if run in ("all", "build"):
        stage_build(
            company_numbers=args.company,
            skip_build=args.skip_build,
            artifact=args.artifact,
        )

    print_summary()
    print(f"\n  Total time: {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
