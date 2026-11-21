#!/usr/bin/env python3
"""Drive one company through every pipeline stage, in order, in the foreground.

WHY THIS EXISTS. The dashboard's "Run all" button already does this, in a
background thread, with the result spread across eight job drawers. That is
right for an operator and wrong for a proof: nobody can read eight drawers and
say "the pipeline works end to end". This prints one transcript, in order,
with one verdict at the bottom and a non-zero exit when anything is unproven.

WHAT IT IS NOT. It is not a second pipeline. Every stage is executed by
api_v2._exec_stage - the same function the dashboard calls - and the decision
to skip or halt is api_v2._compute_stages and api_v2._result_failure, the same
two the run-all chain uses. If this file grew its own idea of what a stage
does, a green run here would prove nothing about the thing operators use.

THE MONEY STAGE. `domain` buys a domain from Namecheap and the charge is real.
It runs ONLY with --spend on the command line, and without it the stage is
reported BLOCKED and the chain stops, because delegate, email and app all
need a domain and running them on a company that has none proves nothing.
A purchase that did not happen must never read like one that did; neither
must a stage that was skipped.

WHERE IT RUNS. On the server, and nowhere else. It needs DATABASE_URL, the
Namecheap key (whose IP allowlist holds the server's address), Companies House
and a Cloudflare token. A Cloudflare credential also cannot be TESTED anywhere
but where it will run - see the CREDENTIALS note in cloudflare_dns.py.

  cd /opt/company_registration
  .venv/bin/python e2e_run.py --company 12345678          # no purchase
  .venv/bin/python e2e_run.py --company 12345678 --spend  # buys a domain
  .venv/bin/python e2e_run.py --find --spend              # picks one itself
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))


# Stages that spend money. Exactly one does today: register_company_domain
# takes a purchase claim and Namecheap bills the account. Anything added here
# must be gated the same way.
MONEY_STAGES = frozenset({"domain"})

RUN      = "run"       # the stage will be executed
ALREADY  = "already"   # complete before this run started; NOT a proof
BLOCKED  = "blocked"   # needs --spend and did not get it
DONE     = "done"      # executed and succeeded, and the work stuck
FAILED   = "failed"    # executed and reported or raised a failure
NOT_RUN  = "not_run"   # the chain halted before reaching it
# Executed, returned success, and _compute_stages STILL reads it incomplete.
# Not the same thing as a failure and it must not be filed as one, but it is
# emphatically not a proof: the dashboard shows no tick and a run-all would
# run the stage again. The email stage with a dead dev@ route landed here.
NO_STICK = "no_stick"


# ── Pure decisions, so they can be tested without a provider ───────────────

def root_guard(euid: int, service_user: str, allow_root: bool) -> str | None:
    """Refuse to run as root on a box where a service user owns the pipeline.

    Returns the refusal message, or None to proceed.

    WHY THIS IS NOT FUSSINESS. Observed 2026-11-21: a console run of this
    harness, as root, created app/src/c16811331 owned by root:root 755. The
    next dashboard run died in shutil.rmtree with "Permission denied:
    'colors.xml'" - write_flavor_resources deletes and recreates that
    directory on every run, and the service user cannot empty a root-owned
    one. app/src itself was already pipeline-owned; root's run had left
    behind exactly one directory, and one was enough.

    What makes it worth refusing rather than warning is that the root run
    reports NOTHING wrong. Root ignores file modes, so it gets further than
    the service ever will, and the damage is invisible from the console that
    caused it. The operator sees a later, unrelated error - in this case a
    bad JAVA_HOME - fixes that, and the dashboard still fails for a reason
    the console cannot reproduce.
    """
    if allow_root or euid != 0 or not service_user:
        return None
    return (
        "Refusing to run as root: this pipeline's service runs as "
        f"{service_user!r}, and anything root creates here - the flavour "
        f"overlay under app/src above all - is left owned by root and the "
        f"service can no longer delete it. The next dashboard run then fails "
        f"with a PermissionError that cannot be reproduced from this "
        f"console, because root ignores the modes that stop it.\n"
        f"  Run it as the service user instead:\n"
        f"    sudo -u {service_user} .venv/bin/python e2e_run.py <args>\n"
        f"  Or pass --allow-root if you have a reason and will fix the "
        f"ownership afterwards."
    )


def plan_stage(stage: str, is_done: bool, allow_spend: bool,
               force: bool = False) -> str:
    """What should happen to one stage: RUN, ALREADY or BLOCKED.

    `force` re-runs a stage that is already complete. Without it, new code
    inside a finished stage can never run: the app stage completed before
    the privacy publisher existed, so that company has no policy URL and no
    way to acquire one short of deleting its artifacts by hand.

    --force is NOT consent to spend. Forcing `domain` means buying a second
    domain, so it still requires --spend; the two flags authorise different
    things and both are needed to re-purchase.
    """
    if is_done and not force:
        return ALREADY
    if stage in MONEY_STAGES and not allow_spend:
        return BLOCKED
    return RUN


def final_verdict(order, outcomes: dict) -> dict:
    """Did this run prove the pipeline, and what did it actually do?

    `proven` is deliberately strict: every stage in the order must have ended
    DONE or ALREADY. `executed` counts only the stages that really ran, and is
    reported separately because a company whose stages were all complete
    already yields a page of ticks and proves nothing whatsoever.
    """
    unproven = [s for s in order if outcomes.get(s) not in (DONE, ALREADY)]
    executed = [s for s in order if outcomes.get(s) == DONE]
    return {
        "proven":   not unproven,
        "unproven": unproven,
        "executed": executed,
        "already":  [s for s in order if outcomes.get(s) == ALREADY],
        "failed":   [s for s in order if outcomes.get(s) == FAILED],
        "blocked":  [s for s in order if outcomes.get(s) == BLOCKED],
        "not_run":  [s for s in order if outcomes.get(s) == NOT_RUN],
        "no_stick": [s for s in order if outcomes.get(s) == NO_STICK],
    }


# ── Picking a company ──────────────────────────────────────────────────────

def pick_company(api_v2, db, nationality: str = "kenyan") -> tuple[str, str]:
    """Find a Companies House company not already in the database, and add it.

    Returns (company_number, company_name). Raises on failure rather than
    returning a sentinel: a run with no company is not a run.
    """
    from run_pipeline import find_companies_by_nationality

    known = db.company_numbers()
    print(f"  {len(known)} company/companies already in the database")
    found = find_companies_by_nationality(nationality, count=len(known) + 5)
    if not found:
        raise RuntimeError(
            "Companies House returned no companies with %s directors; this is "
            "a search failure, not an empty world" % nationality)

    for c in found:
        cn = api_v2._cn8(c.get("company_number"))
        if cn in known:
            continue
        name = str(c.get("company_name") or c.get("title") or "").strip()
        if not name:
            continue
        print(f"  picked {cn} {name}")
        api_v2._add_company_row(cn, name, str(c.get("date_of_creation") or ""))
        row = api_v2._find_row(cn)
        if not row:
            raise RuntimeError(
                "added %s to the database but it cannot be read back; the "
                "insert did not take" % cn)
        return cn, name

    raise RuntimeError(
        "all %d companies Companies House returned are already in the "
        "database; widen the search or pass --company" % len(found))


# ── The driver ─────────────────────────────────────────────────────────────

def drive(cn: str, allow_spend: bool, only=None, stop_after=None,
          force=None) -> int:
    import api_v2
    import db

    force = set(force or ())
    order = [s for s in api_v2.STAGE_ORDER if not only or s in only]
    row = api_v2._find_row(cn)
    if not row:
        print(f"!! no company {cn} in the database. Add it from the dashboard, "
              f"or run with --find.")
        return 2
    name = str(row.get("Company Name") or "")
    before = api_v2._compute_stages(cn, row)

    print()
    print("=" * 72)
    print(f"END-TO-END RUN  {cn}  {name}")
    print(f"started {datetime.now().isoformat(timespec='seconds')}")
    print(f"purchases: {'ALLOWED (--spend)' if allow_spend else 'BLOCKED (no --spend)'}")
    print("=" * 72)
    print("\nplan:")
    planned = {}
    for s in order:
        planned[s] = plan_stage(s, bool(before.get(s)), allow_spend,
                                force=s in force)
        note = {RUN: ("will run (FORCED, it is already complete)"
                      if s in force and before.get(s) else "will run"),
                ALREADY: "already complete - will be SKIPPED, proves nothing",
                BLOCKED: "SPENDS MONEY - blocked without --spend"}[planned[s]]
        print(f"  {s:<12} {note}")

    outcomes = {s: NOT_RUN for s in order}
    halted = None

    for s in order:
        if halted:
            break
        action = planned[s]
        if action is ALREADY:
            outcomes[s] = ALREADY
            print(f"\n--- {s}: skipped, already complete ---")
            continue
        if action is BLOCKED:
            outcomes[s] = BLOCKED
            print(f"\n--- {s}: BLOCKED ---")
            print("  this stage buys a domain and bills the Namecheap account.")
            print("  re-run with --spend to authorise it.")
            halted = ("blocked", s)
            break

        print(f"\n--- {s}: running ---")
        fresh = api_v2._find_row(cn) or row
        try:
            result = api_v2._exec_stage(cn, s, fresh)
        except Exception as e:
            outcomes[s] = FAILED
            print(f"  !! {s} raised {type(e).__name__}: {e}")
            traceback.print_exc()
            halted = ("raised", s)
            break
        failure = api_v2._result_failure(result)
        if failure:
            outcomes[s] = FAILED
            print(f"  !! {s} reported failure: {failure}")
            halted = ("failed", s)
            break
        # The stage's own word is not the last word. _compute_stages is what
        # the dashboard tick and the run-all skip read, and a stage that
        # returns success while leaving that False is a stage whose work did
        # not stick - the email stage with a broken dev@ route was exactly
        # this. Recording DONE here would make this harness commit the very
        # defect it exists to catch, so it does not: the stage did not fail,
        # but it did not finish either, and the chain stops because every
        # stage after it would be running on an unfinished one.
        after = api_v2._compute_stages(cn, api_v2._find_row(cn) or fresh)
        if not after.get(s):
            outcomes[s] = NO_STICK
            print(f"  !! {s} returned success but _compute_stages still reads "
                  f"it as INCOMPLETE. The dashboard will show no tick and a "
                  f"run-all would run it again, so its work did not stick.")
            halted = ("did not stick", s)
            break
        outcomes[s] = DONE
        print(f"  {s}: done")

    if halted:
        why, s = halted
        print(f"\nchain halted at {s} ({why}); later stages were not reached")

    row = api_v2._find_row(cn) or {}
    v = final_verdict(order, outcomes)

    print()
    print("=" * 72)
    print(f"VERDICT  {cn}  {name}")
    print("=" * 72)
    for s in order:
        mark = {DONE: "ok      ", ALREADY: "skipped ", FAILED: "FAILED  ",
                BLOCKED: "BLOCKED ", NOT_RUN: "not run ",
                NO_STICK: "NO STICK"}[outcomes[s]]
        print(f"  {mark} {s}")
    print()
    print(f"  executed this run : {len(v['executed'])} "
          f"({', '.join(v['executed']) or 'none'})")
    print(f"  already complete  : {len(v['already'])} "
          f"({', '.join(v['already']) or 'none'})")
    for label, key in (("failed", "failed"), ("blocked", "blocked"),
                       ("did not stick", "no_stick"),
                       ("not reached", "not_run")):
        if v[key]:
            print(f"  {label:<18}: {', '.join(v[key])}")
    print()
    for h in ("Domain", "DNS Delegation", "Assigned Email",
              "Dev Email Forwarding", "DUNS Number"):
        print(f"  {h:<22} {str(row.get(h) or '(empty)')}")
    print()
    if v["proven"]:
        if not v["executed"]:
            print("  EVERY STAGE WAS ALREADY COMPLETE. Nothing ran, so this "
                  "run proves nothing;\n  use a fresh company.")
            return 1
        print(f"  PROVEN end to end: all {len(order)} stages are complete, "
              f"{len(v['executed'])} of them\n  executed in this run.")
        return 0
    print(f"  NOT PROVEN: {', '.join(v['unproven'])}")
    return 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--company", metavar="CN",
                   help="company number already in the database")
    g.add_argument("--find", action="store_true",
                   help="pick a fresh Companies House company and add it")
    p.add_argument("--spend", action="store_true",
                   help="authorise the domain purchase; without it the domain "
                        "stage is blocked and the chain stops")
    p.add_argument("--nationality", default="kenyan",
                   help="director nationality to search for with --find")
    p.add_argument("--only", default="",
                   help="comma-separated subset of stages to consider")
    p.add_argument("--force", default="",
                   help="comma-separated stages to re-run even though they "
                        "are already complete, or 'all'. Forcing the domain "
                        "stage still requires --spend: it would buy a second "
                        "domain.")
    p.add_argument("--allow-root", action="store_true",
                   help="run as root anyway, leaving files the service user "
                        "cannot delete")
    a = p.parse_args()

    # Before anything touches the filesystem. The service user is read from
    # the unit rather than assumed, so a box with no unit is unaffected.
    try:
        from deploy_check import service_user
        user = service_user()
    except Exception:
        user = ""
    refusal = root_guard(os.geteuid(), user, a.allow_root)
    if refusal:
        print(refusal)
        return 2

    import pipeline
    pipeline._load_env()
    import logging_setup
    logging_setup.configure_logging()

    import api_v2
    import db

    only = [s.strip() for s in a.only.split(",") if s.strip()]
    force = [s.strip() for s in a.force.split(",") if s.strip()]
    if force == ["all"]:
        force = list(api_v2.STAGE_ORDER)
    for s in only + force:
        if s not in api_v2.STAGE_ORDER:
            print(f"!! unknown stage {s!r}; known: {', '.join(api_v2.STAGE_ORDER)}")
            return 2

    if a.find:
        try:
            cn, _name = pick_company(api_v2, db, a.nationality)
        except Exception as e:
            print(f"!! could not pick a company: {e}")
            return 2
    else:
        cn = api_v2._cn8(a.company)

    return drive(cn, allow_spend=a.spend, only=only, force=force)


if __name__ == "__main__":
    raise SystemExit(main())
