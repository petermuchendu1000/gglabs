#!/usr/bin/env python3
"""Columns the dashboard could fill and didn't.

    python3 test_row_completeness.py

Hermetic: no network, no database, no registrar, no browser.

Three columns were only ever written by the batch CLI path (db.save_results),
so on a dashboard-only workflow they stayed empty for ever and looked like
something an operator had to type:

  * D&B Legal Name / D&B Address - get_duns_number RETURNS both
    (run_pipeline.py:586-592) and the dashboard's duns stage dropped them.
  * Dev Email Forwarding - pipeline.stage_email configures the real Namecheap
    forward and writes nothing back; the dashboard never configured it at all.

And one column could only be filled by hand for a different reason:

  * DUNS Email Used - duns_automation creates a real mail.tm address
    (create_temp_email, duns_automation.py:336) and keeps it in its OWN json
    store, which has never written the companies table. The dashboard's duns
    stage had a branch that wrote the column from r["temp_email"], a key
    get_duns_number never returns on a status it never emits - dead code
    standing in for a sync that did not exist.
"""
import os
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

from logging_setup import configure_logging

configure_logging()

import api_v2
import db


class Results:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []
        self.skipped: list[str] = []

    def check(self, name: str, cond: bool, detail: str = "") -> bool:
        if cond:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed.append(name)
            print(f"  FAIL  {name}" + (f"\n          {detail}" if detail else ""))
        return bool(cond)

    def skip(self, name: str, why: str) -> None:
        self.skipped.append(name)
        print(f"  SKIP  {name}\n          {why}")

    def report(self) -> int:
        print()
        print(f"{self.passed} passed / {len(self.failed)} failed / "
              f"{len(self.skipped)} skipped")
        for f in self.failed:
            print(f"  failed: {f}")
        return 1 if self.failed else 0


def _run_stage(cn, stage, row=None, timeout=10.0):
    import time
    row = row or {"Company Number": cn, "Company Name": "TEST CO LTD"}
    api_v2._spawn(cn, stage, lambda: api_v2._exec_stage(cn, stage, row))
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = api_v2._job_get(cn, stage)
        if j.get("status") in ("done", "failed"):
            return j
        time.sleep(0.02)
    return api_v2._job_get(cn, stage)


# ── the D&B columns the lookup already returns ─────────────────────────────

def check_dnb_columns_are_written(r: Results) -> None:
    print("\nduns stage: the D&B name and address are recorded")
    import run_pipeline
    writes: list[dict] = []
    saved = (run_pipeline.get_duns_number, api_v2._update_excel_row)
    try:
        api_v2._update_excel_row = lambda cn, u: writes.append(dict(u))
        run_pipeline.get_duns_number = lambda cn, headless=True: {
            "duns_number": "987654321", "status": "found",
            "company_name": "TAMU TRADERS LIMITED",
            "dnb_name": "TAMU TRADERS LIMITED",
            "dnb_address": "1 Example Street, London, EC1A 1BB, GB"}
        _run_stage("00000950", "duns")
        merged = {k: v for w in writes for k, v in w.items()}
        r.check("D&B Legal Name is written",
                merged.get("D&B Legal Name") == "TAMU TRADERS LIMITED",
                f"writes were {writes!r} - get_duns_number returns dnb_name and "
                f"the stage used to drop it")
        r.check("D&B Address is written",
                "Example Street" in str(merged.get("D&B Address") or ""),
                f"writes were {writes!r}")
        r.check("the DUNS number is still written",
                merged.get("DUNS Number") == "987654321", f"{writes!r}")
    finally:
        run_pipeline.get_duns_number, api_v2._update_excel_row = saved


def check_absent_dnb_fields_are_not_blanked(r: Results) -> None:
    """A lookup that returns no D&B detail must not erase what is there.

    db._norm turns "" into None and update_row has no blank-skip, so writing
    an empty string is a NULL over whatever an operator typed.
    """
    print("\nduns stage: a lookup with no D&B detail erases nothing")
    import run_pipeline
    writes: list[dict] = []
    saved = (run_pipeline.get_duns_number, api_v2._update_excel_row)
    try:
        api_v2._update_excel_row = lambda cn, u: writes.append(dict(u))
        run_pipeline.get_duns_number = lambda cn, headless=True: {
            "duns_number": "", "status": "not_found"}
        _run_stage("00000951", "duns")
        merged = {k: v for w in writes for k, v in w.items()}
        r.check("D&B Legal Name is not written at all",
                "D&B Legal Name" not in merged, f"writes were {writes!r}")
        r.check("D&B Address is not written at all",
                "D&B Address" not in merged, f"writes were {writes!r}")
    finally:
        run_pipeline.get_duns_number, api_v2._update_excel_row = saved


def check_dead_temp_email_branch_is_gone(r: Results) -> None:
    """The stage must not claim to record an email it is never given."""
    print("\nduns stage: no dead branch writing a key the lookup never returns")
    src = open(os.path.join(REPO, "api_v2.py"), encoding="utf-8").read()
    duns = src[src.index('elif stage == "duns"'):src.index('elif stage == "certificate"')]
    # Comments stripped first: a comment explaining why the key is NOT read is
    # not the same as reading it, and a test that cannot tell prose from code
    # fails on the explanation.
    code = "\n".join(l for l in duns.splitlines()
                     if not l.strip().startswith("#"))
    r.check("the stage does not read r['temp_email']",
            "temp_email" not in code,
            "get_duns_number returns found/not_found/error and never a "
            "temp_email key, so this branch could never run - the real source "
            "is duns_automation's store, synced separately")


# ── dev@ forwarding, which the dashboard never configured ──────────────────

class _Pool:
    def get_assigned_email(self, cn):
        return ("owner@example.invalid", "Ada", "Byron")

    def assign_next(self, company_number="", company_name=""):
        return ("owner@example.invalid", "Ada", "Byron")


def _email_stage(cn, row, forwarding):
    """Run the real email stage with the pool and Namecheap stubbed."""
    import email_pool
    import namecheap_automation as nc
    writes: list[dict] = []
    calls: list[tuple] = []
    saved = (email_pool.EmailPool, nc.ensure_dev_email_forwarding,
             api_v2._update_excel_row)
    try:
        email_pool.EmailPool = _Pool
        api_v2._update_excel_row = lambda c, u: writes.append(dict(u))

        def fake_forward(domain, forward_to):
            calls.append((domain, forward_to))
            return forwarding

        nc.ensure_dev_email_forwarding = fake_forward
        job = _run_stage(cn, "email", row)
        return job, writes, calls
    finally:
        (email_pool.EmailPool, nc.ensure_dev_email_forwarding,
         api_v2._update_excel_row) = saved


def check_forwarding_is_configured_and_recorded(r: Results) -> None:
    print("\nemail stage: dev@ forwarding is set up and recorded")
    job, writes, calls = _email_stage(
        "00000960",
        {"Company Number": "00000960", "Company Name": "TEST CO LTD",
         "Domain": "tamu-traders.site", "Assigned Email": "owner@example.invalid"},
        {"status": "configured", "mailbox": "dev"})
    merged = {k: v for w in writes for k, v in w.items()}
    r.check("Namecheap was asked to forward dev@ for the domain",
            calls == [("tamu-traders.site", "owner@example.invalid")],
            f"calls were {calls!r}")
    r.check("Dev Email Forwarding is recorded",
            merged.get("Dev Email Forwarding") == "configured",
            f"writes were {writes!r} - only the batch CLI ever filled this")
    r.check("the stage still reads done", job.get("status") == "done",
            f"{job.get('status')!r}")


def check_forwarding_skipped_without_a_domain(r: Results) -> None:
    print("\nemail stage: with no domain yet, forwarding is skipped quietly")
    job, writes, calls = _email_stage(
        "00000961",
        {"Company Number": "00000961", "Company Name": "TEST CO LTD"},
        {"status": "configured"})
    merged = {k: v for w in writes for k, v in w.items()}
    r.check("Namecheap was not called", calls == [], f"calls were {calls!r}")
    r.check("nothing was written for forwarding",
            "Dev Email Forwarding" not in merged, f"writes were {writes!r}")
    r.check("the email was still assigned",
            merged.get("Assigned Email") == "owner@example.invalid", f"{writes!r}")
    r.check("the stage reads done", job.get("status") == "done",
            f"{job.get('status')!r}")


def check_forwarding_failure_is_not_silent(r: Results) -> None:
    """A dev@ address is a blocking Play Console check, so a forward that did
    not happen must not read as a finished stage."""
    print("\nemail stage: a forwarding failure fails the stage")
    job, writes, calls = _email_stage(
        "00000962",
        {"Company Number": "00000962", "Company Name": "TEST CO LTD",
         "Domain": "tamu-traders.site", "Assigned Email": "owner@example.invalid"},
        {"status": "error", "error": "Namecheap said no"})
    merged = {k: v for w in writes for k, v in w.items()}
    r.check("the job is failed", job.get("status") == "failed",
            f"status was {job.get('status')!r}")
    r.check("the reason reaches the operator",
            "Namecheap said no" in str(job.get("error") or ""),
            f"error was {job.get('error')!r}")
    r.check("the failure is recorded on the row",
            "error" in str(merged.get("Dev Email Forwarding") or ""),
            f"writes were {writes!r}")


# ── the DUNS store finally talks to the company row ────────────────────────

def check_duns_store_syncs_to_the_row(r: Results) -> None:
    print("\nduns sync: the temp email and number reach the company row")
    import duns_automation as da
    writes: list[dict] = []
    saved = (da._load_duns_db, db.update_row)
    try:
        da._load_duns_db = lambda: {"00000970": {
            "status": "completed",
            "duns_number": "123456789",
            "temp_email": {"address": "abc123@mail.tm", "password": "x",
                           "token": "y"},
        }}
        db.update_row = lambda cn, u: writes.append((cn, dict(u)))
        written = da.sync_duns_to_company_row("00000970")
        merged = {k: v for _cn, w in writes for k, v in w.items()}
        r.check("DUNS Email Used comes from the store, not a human",
                merged.get("DUNS Email Used") == "abc123@mail.tm",
                f"writes were {writes!r}")
        r.check("the DUNS number is synced too",
                merged.get("DUNS Number") == "123456789", f"{writes!r}")
        r.check("so is the status",
                str(merged.get("DUNS Status") or "") != "", f"{writes!r}")
        r.check("and it reports what it wrote",
                "DUNS Email Used" in (written or {}), f"returned {written!r}")
    finally:
        da._load_duns_db, db.update_row = saved


def check_duns_sync_with_no_record_writes_nothing(r: Results) -> None:
    print("\nduns sync: a company with no DUNS request writes nothing")
    import duns_automation as da
    writes: list[dict] = []
    saved = (da._load_duns_db, db.update_row)
    try:
        da._load_duns_db = lambda: {}
        db.update_row = lambda cn, u: writes.append((cn, dict(u)))
        written = da.sync_duns_to_company_row("00000971")
        r.check("nothing was written", writes == [], f"{writes!r}")
        r.check("and it says so", not written, f"returned {written!r}")
    finally:
        da._load_duns_db, db.update_row = saved


def check_duns_sync_never_blanks(r: Results) -> None:
    print("\nduns sync: a record with no email does not blank the column")
    import duns_automation as da
    writes: list[dict] = []
    saved = (da._load_duns_db, db.update_row)
    try:
        da._load_duns_db = lambda: {"00000972": {"status": "submitted"}}
        db.update_row = lambda cn, u: writes.append((cn, dict(u)))
        da.sync_duns_to_company_row("00000972")
        merged = {k: v for _cn, w in writes for k, v in w.items()}
        r.check("DUNS Email Used is not written at all",
                "DUNS Email Used" not in merged, f"writes were {writes!r}")
        r.check("DUNS Number is not written at all",
                "DUNS Number" not in merged, f"writes were {writes!r}")
    finally:
        da._load_duns_db, db.update_row = saved


def check_the_duns_routes_sync(r: Results) -> None:
    """Every path that learns something must sync, not just one."""
    print("\nduns sync: the routes that learn something call it")
    src = open(os.path.join(REPO, "app.py"), encoding="utf-8").read()
    for route in ("api_duns_initiate", "api_duns_check_email", "api_duns_set"):
        body = src[src.index(f"def {route}("):]
        body = body[:body.index("@app.route", 1)] if "@app.route" in body[1:] else body
        r.check(f"{route} syncs the row", "sync" in body.lower(),
                f"the DUNS store learns the email, the status or the number "
                f"here and the company row never hears about it")


def main() -> int:
    print("test_row_completeness.py — columns the dashboard could fill and didn't")
    r = Results()
    check_dnb_columns_are_written(r)
    check_absent_dnb_fields_are_not_blanked(r)
    check_dead_temp_email_branch_is_gone(r)
    check_forwarding_is_configured_and_recorded(r)
    check_forwarding_skipped_without_a_domain(r)
    check_forwarding_failure_is_not_silent(r)
    check_duns_store_syncs_to_the_row(r)
    check_duns_sync_with_no_record_writes_nothing(r)
    check_duns_sync_never_blanks(r)
    check_the_duns_routes_sync(r)
    return r.report()


if __name__ == "__main__":
    raise SystemExit(main())
