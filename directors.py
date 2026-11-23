#!/usr/bin/env python3
"""Reading Companies House officer records.

One copy, imported by both the console (api_v2) and the pipeline
(run_pipeline), because the console filters a search on these rules and the
pipeline writes a row from them. This project has twice paid for the same
logic living in two places - two domain scorers that disagreed about which
company a domain spelled, and two output paths that disagreed about where
an app was written - so the selection rule gets one home.

Pure: standard library only, no network, no database, no configuration.
Every observation quoted below came from live Companies House responses.
"""
from __future__ import annotations

KENYAN = "kenyan"

# Spellings of the same claim. Companies House stores nationality as free
# text typed by the filer, so it is not a controlled vocabulary.
#
# MEASURED, not guessed. Part 1 of 33 of the PSC snapshot of 2026-11-22
# holds 461,710 individual records, all with a nationality. The Kenyan ones
# are spelled: Kenyan 181, British,Kenyan 3, Kenyan,British 1,
# Finnish,Kenyan 1, English,Kenyan 1, and Kenya 1 - one in 188 without the
# trailing n. An exact membership test against "kenyan" silently missed it,
# and the officers data the pipeline reads comes from the same field typed
# by the same filers.
#
# Still a membership test over comma-split values, never a substring scan:
# 'Kenyatta' is a surname and must not read as a nationality.
KENYAN_FORMS = frozenset({"kenyan", "kenya"})


def is_kenyan(officer: dict) -> bool:
    """Does this officer claim Kenyan nationality, however it is spelled?

    One home for the rule. It was written out at two call sites - here and
    in api_v2 - as `KENYAN in nationalities(o)`, which is exactly the kind
    of duplication the module docstring above exists to prevent, and it
    meant a fix had to be made twice or be half made.
    """
    return bool(KENYAN_FORMS & set(nationalities(officer)))


def nationalities(officer: dict) -> list[str]:
    """An officer's nationalities, lowercased.

    Companies House records this as free text. Observed live: 'British',
    'Kenyan', 'South African' (a space INSIDE one value), 'British,Kenyan'
    and 'British,Irish' (comma-joined for dual nationals), and None. So the
    split is on commas only - splitting on whitespace would turn
    'South African' into two bogus nationalities, neither of which is a
    nationality.
    """
    raw = officer.get("nationality")
    if raw is None:
        return []
    return [p.strip().lower() for p in str(raw).split(",") if p.strip()]


def active_directors(items: list[dict] | None) -> list[dict]:
    """The officers who are directors and have not resigned.

    'director' is matched as a substring because the role arrives as
    'director', 'corporate-director' and 'nominee-director'. Resignation is
    the PRESENCE of resigned_on - there is no status field to read.
    """
    out: list[dict] = []
    for o in items or []:
        if o.get("resigned_on"):
            continue
        if "director" not in str(o.get("officer_role") or "").lower():
            continue
        out.append(o)
    return out


def pick_kenyan(officers: list[dict] | None) -> list[dict]:
    """The Kenyan ones out of an ALREADY-FILTERED list, earliest first.

    Separate from kenyan_directors because the two callers hold different
    things. The pipeline has the raw officers payload; the Play Console
    dossier has details["directors"], which get_company_details has already
    reduced to active directors and which carries only name, nationality
    and appointed_on - no officer_role at all. Running the role filter over
    that list would discard every entry and name nobody.

    The ORDER is why this is a function rather than a comprehension at two
    call sites. The row's director columns are single-valued, so which
    Kenyan director they describe has to be the same answer everywhere, and
    must not depend on the order the API happened to return - KENYA AIRWAYS
    PLC has three active Kenyan directors among five.

    A missing appointed_on sorts last, not first, so a dated appointment
    always wins over an undated one.

    The name breaks a tie, because the date alone does not. Measured on
    FC010541: ELEVEN active Kenyan directors, every one appointed
    1980-07-11. Python's sort is stable, so on the date alone the winner
    would be whichever one Companies House happened to list first - and
    the API promises no order, so the same company could name a different
    director from one request to the next, and the row would describe
    someone the ID stage never produces.
    """
    kenyan = [o for o in officers or [] if is_kenyan(o)]
    kenyan.sort(key=lambda o: (str(o.get("appointed_on") or "9999-99-99"),
                               str(o.get("name") or "")))
    return kenyan


def kenyan_directors(items: list[dict] | None) -> list[dict]:
    """Active Kenyan directors from a raw officers payload, earliest first."""
    return pick_kenyan(active_directors(items))


def dob_month(officer: dict) -> str:
    """An officer's date of birth as YYYY-MM, or "" if it is not published.

    Companies House gives month and year only - never a day - so this is
    the whole of what is knowable, and YYYY-MM is the shape the import
    template's own example uses ("1985-04"). Observed live:
    {"month": 1, "year": 1984}.
    """
    dob = officer.get("date_of_birth") or {}
    year, month = dob.get("year"), dob.get("month")
    if not year or not month:
        return ""
    try:
        return f"{int(year):04d}-{int(month):02d}"
    except (TypeError, ValueError):
        return ""
