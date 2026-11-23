#!/usr/bin/env python3
"""What the company detail pane has to show.

The pane is the screen an operator reads before running a stage that
spends money. Two values were stored, sent over the wire, and never
rendered:

DIRECTOR NATIONALITY. The pipeline's whole admission rule is "at least
one Kenyan director" (api_v2._kenyan_verdict). The pane showed the
director's NAME and not their nationality, so the one fact that justified
admitting the company - and the one an operator would want to check
before a billed domain order goes out in that company's name - was
visible only by opening Edit fields.

DATE OF REGISTRATION. Stored by the add route, autofilled from Companies
House, shown nowhere. It is how you tell two similarly-named companies
apart.

Both were already in /api/v2/company/<cn> (api_v2.py:962, :972). Nothing
needed fetching; they were simply dropped on the floor by renderDetail.

This file checks the RENDERED page source rather than the route, because
the route was never the problem.
"""
from __future__ import annotations

import os
import re
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)


class Results:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, name: str, cond: bool, detail: str = "") -> bool:
        if cond:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed += 1
            print(f"  FAIL  {name}")
            if detail:
                for line in str(detail).splitlines():
                    print(f"          {line}")
        return bool(cond)

    def report(self) -> int:
        print(f"\n{self.passed} passed / {self.failed} failed")
        return 1 if self.failed else 0


def page() -> str:
    return open(os.path.join(REPO, "static", "app.html"),
                encoding="utf-8").read()


def live(src: str) -> str:
    """The page with // comment lines removed.

    Three checks in this project have matched commentary as if it were
    code. A substring test is happy to find the word in a sentence
    explaining why the code should exist.
    """
    return "\n".join(l for l in src.splitlines()
                     if not l.strip().startswith("//"))


def check_route_already_sends_them(r: Results) -> None:
    """Establish there is nothing to fetch before asking the page to show it."""
    print("\n-- the values are already on the wire --")
    api = open(os.path.join(REPO, "api_v2.py"), encoding="utf-8").read()
    body = api[api.index("def v2_company("):api.index("def v2_update_fields")]
    r.check("v2_company sends nationalities",
            '"nationalities"' in body, "nothing to render if it is not sent")
    r.check("v2_company sends date_of_creation",
            '"date_of_creation"' in body, body[:200])


def check_nationality_is_rendered(r: Results) -> None:
    print("\n-- the director's nationality is on the pane --")
    src = page()
    code = live(src)

    r.check("icDirectors is given the nationalities",
            re.search(r"icDirectors\(\s*d\.directors\s*,\s*d\.nationalities",
                      code) is not None,
            "the card renders names only, so the admission fact is invisible "
            "unless Edit fields is opened")

    fn = code[code.index("function icDirectors"):]
    fn = fn[:fn.index("\nfunction ")]
    r.check("...and the function actually uses them",
            "nats" in fn or "nationalit" in fn.lower(),
            "accepting the argument and ignoring it renders the same pane")
    r.check("...pairing each director with their own nationality",
            "split(\";\")" in fn.replace("'", '"'),
            "Directors and Director Nationalities are parallel ';'-joined "
            "lists; showing the whole string against every name would "
            "attribute a nationality to the wrong person")


def check_registration_date_is_rendered(r: Results) -> None:
    print("\n-- the date of registration is on the pane --")
    code = live(page())
    detail = code[code.index("function renderDetail"):]
    detail = detail[:detail.index("function buildStageRow")]
    r.check("renderDetail reads date_of_creation",
            "date_of_creation" in detail,
            "stored, sent, and never shown")
    r.check("...and puts it in the header",
            "Reg." in detail or "Registered" in detail,
            "a bare date with no label is not readable next to a company "
            "number and a status")


def check_it_degrades_quietly(r: Results) -> None:
    """A company added before autofill existed has neither value."""
    print("\n-- a row without them must not render an empty label --")
    code = live(page())
    detail = code[code.index("function renderDetail"):]
    detail = detail[:detail.index("function buildStageRow")]
    r.check("the registration badge is conditional",
            re.search(r"d\.date_of_creation\s*\?", detail) is not None,
            "an unconditional badge renders 'Reg.' with nothing after it "
            "for every company added before the autofill existed")

    fn = code[code.index("function icDirectors"):]
    fn = fn[:fn.index("\nfunction ")]
    r.check("a missing nationality renders nothing, not 'undefined'",
            "undefined" not in fn,
            "indexing past the end of the nationalities list is the normal "
            "case for a hand-entered row with two directors and one "
            "nationality")


def unescaped_in(js: str) -> list[str]:
    """Interpolations that put raw field data into HTML.

    Three passes, because the two obvious shortcuts both misfire:
      - string literals go first, or the class name "dir-nat-kenyan"
        reads as a reference to `nat`, since a hyphen is a word boundary;
      - x(...) calls go next, so `${nat ? "·" + x(nat) : ""}` is seen for
        what it is - an escaped value;
      - a ternary CONDITION is not an output, so `nat ?` is dropped.
    What survives all three is a field interpolated raw.
    """
    DATA = {"n", "nat", "val", "nats"}
    bad = []
    for v in re.findall(r"\$\{([^}]*)\}", js):
        s = re.sub(r'"[^"]*"|\'[^\']*\'', "", v)      # string literals
        s = re.sub(r"\bx\([^)]*\)", "", s)            # escaped values
        s = re.sub(r"\b[A-Za-z_$][\w$]*\s*\?", "", s)  # ternary guards
        if DATA & set(re.findall(r"\b[A-Za-z_$][\w$]*\b", s)):
            bad.append(v)
    return bad


def check_the_chip_is_wide_enough_to_read(r: Results) -> None:
    """Rendering it is not the same as showing it.

    The nationality went into .dir-chip, which was capped at 140px with
    an ellipsis because it used to hold a name on its own. The chip then
    rendered "WANJIKU, Grace | K…". Every text-based check passed -
    innerText carries the whole string - and the operator could read one
    letter of the fact the admission rule turns on. Found by looking at
    the pixels, so the number that caused it is pinned here.
    """
    print("\n-- the chip is wide enough for a name AND a nationality --")
    src = page()
    block = src[src.index(".dir-chip {"):]
    block = block[:block.index("}")]
    m = re.search(r"max-width:\s*(\d+)px", block)
    r.check("the chip has a max-width", m is not None, block)
    if m:
        r.check("...wide enough for both values",
                int(m.group(1)) >= 220,
                f"max-width is {m.group(1)}px; at 140px 'Kenyan' rendered "
                f"as 'K…' while every text assertion still passed")


def check_escaping(r: Results) -> None:
    """Both values come from Companies House and are rendered as HTML."""
    print("\n-- both values are escaped --")
    code = live(page())
    fn = code[code.index("function icDirectors"):]
    fn = fn[:fn.index("\nfunction ")]
    # An interpolation is safe when, after every x(...) call is removed,
    # no bare reference to the DATA remains. That is stricter than
    # "starts with x(" - which rejects `${nat ? " · " + x(nat) : ""}`,
    # where the value IS escaped - and weaker than nothing, because
    # `${nat}` still fails. PRE_ESCAPED names hold HTML this function
    # built itself from already-escaped parts.
    r.check("every interpolation in icDirectors is escaped",
            not unescaped_in(fn),
            f"unescaped data reference in: {unescaped_in(fn)}")

    # A check that keeps crying wolf gets deleted, and a check never seen
    # failing proves nothing (L5). Both directions, here, on the spot.
    r.check("...and that check can still fail",
            unescaped_in('`<span>${nat}</span>`') != [],
            "a bare ${nat} must be caught, or this check is decoration")
    r.check("...without flagging an escaped value behind a guard",
            unescaped_in('`${nat ? " · " + x(nat) : ""}`') == [],
            "the value IS escaped; the bare `nat` is only the condition")
    r.check("...or a hyphenated class name that contains a field name",
            unescaped_in('`${k ? " dir-nat-kenyan" : ""}`') == [],
            "'nat' sits between hyphens inside a string literal")

    detail = code[code.index("function renderDetail"):]
    detail = detail[:detail.index("function buildStageRow")]
    m = re.search(r"Reg\.[^`]*?\$\{([^}]*)\}", detail)
    r.check("the registration date is escaped",
            m is not None and m.group(1).strip().startswith("x("),
            f"got {m.group(1) if m else 'no match'}")


def main() -> int:
    r = Results()
    print("test_detail_fields.py - what the detail pane must show")
    check_route_already_sends_them(r)
    check_nationality_is_rendered(r)
    check_registration_date_is_rendered(r)
    check_it_degrades_quietly(r)
    check_the_chip_is_wide_enough_to_read(r)
    check_escaping(r)
    return r.report()


if __name__ == "__main__":
    raise SystemExit(main())
