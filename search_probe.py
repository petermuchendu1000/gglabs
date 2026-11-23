#!/usr/bin/env python3
"""Why did the Add Company search return what it returned?

    cd /opt/company_registration
    .venv/bin/python search_probe.py kimani
    .venv/bin/python search_probe.py kimani --deep

Runs the SAME code the dashboard runs - _ch_search_candidates to page
through Companies House, _fetch_kenyan for the admissibility verdict - and
prints one line per company saying whether it was kept or hidden and why.
Nothing else shows the per-company reason: the dashboard aggregates it into
"8 hidden (no Kenyan director)", which tells an operator that something was
excluded but not which rule did it.

READ-ONLY. It writes nothing, adds nothing to the pipeline, and spends only
Companies House requests - one search call per 20 companies scanned, one
officers call per candidate examined. Verdicts land in the same 24h cache
the dashboard uses, so running this warms it rather than competing with it.

It exists because the alternative was moving COMPANIES_HOUSE_API_KEY off
the server to somewhere it could be run from, and a diagnostic is not worth
copying a credential for.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("query", help="what you would type into Add Company")
    p.add_argument("--deep", action="store_true",
                   help="the budget behind the 'Check all matches' button "
                        "rather than the per-keystroke one")
    p.add_argument("--years", default="",
                   help="comma-separated years of registration to keep")
    a = p.parse_args()

    import pipeline
    pipeline._load_env()

    if not os.environ.get("COMPANIES_HOUSE_API_KEY", "").strip():
        print("COMPANIES_HOUSE_API_KEY is not set, so nothing can be looked "
              "up. It lives in .env on the server.")
        return 2

    import api_v2
    from run_pipeline import ch_get

    budget = api_v2._NAT_MAX_DEEP if a.deep else api_v2._NAT_MAX_PER_SEARCH
    years = {y.strip() for y in a.years.split(",") if y.strip()}

    print(f"query      : {a.query!r}")
    print(f"mode       : {'deep (Check all matches)' if a.deep else 'type-ahead'}"
          f"  budget={budget}")
    if years:
        print(f"years      : {sorted(years)}")

    cands, total, scanned = api_v2._ch_search_candidates(
        a.query, ch_get, years or None, seen=set(), want=budget)

    print(f"matches    : {total} at Companies House")
    print(f"scanned    : {scanned} (active + year filter -> {len(cands)} candidates)")
    if not cands:
        print("\nNo candidates to check. Every match was dissolved, outside "
              "the year filter, or already in the pipeline.")
        return 1

    print(f"examining  : {min(len(cands), budget)} for a Kenyan director\n")

    kept, excluded, note = api_v2._apply_kenyan_filter(cands, budget=budget)
    keep_cns = {k["cn"] for k in kept}

    for c in cands:
        mark = "KEEP" if c["cn"] in keep_cns else "hide"
        reason = c.get("kenyan_reason", "(not examined)")
        print(f"  {mark}  {c['cn']}  {c['name'][:44]:<44}  {reason}")

    print(f"\nadmissible : {len(kept)}")
    print(f"hidden     : {excluded}")
    if note:
        print(f"note       : {note}")
    if total > min(len(cands), budget) and not a.deep:
        print(f"\n{total} companies match and {min(len(cands), budget)} were "
              f"checked. Re-run with --deep to go through the rest.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
