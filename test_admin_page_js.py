#!/usr/bin/env python3
"""The admin page's JavaScript must survive being a Python string.

The page is JavaScript written inside a non-raw Python string literal. That
means Python processes backslash escapes BEFORE the browser ever sees the
text, and `\\n` written for JavaScript becomes a real newline in what is
served. A real newline inside a quoted JS literal is a SyntaxError, and a
SyntaxError does not fail locally - it kills the ENTIRE <script> block, so
every function defined in it becomes undefined at once.

That is what shipped. `(job.log || []).join("\\n")` arrived in the browser
as:

    logEl.textContent = (job.log || []).join("
    ");

and the whole admin panel went inert: the status line stayed on its "..."
placeholder and "Fetch latest snapshot" did nothing, because pscRefresh was
never defined. Nothing in the server logs, nothing in the Python tests, and
`node --check` on admin_routes.py's SOURCE passes - because in the source
the escape is still two characters. The defect exists only in the rendered
output, so only the rendered output can be checked.

The author already knew about this trap: \\u2014 is correctly double-escaped
in four places. Four others were missed. Knowing the rule is not the same as
applying it every time, which is why this is a test and not a comment.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)


class Results:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.skipped = 0

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

    def skip(self, name: str, why: str) -> None:
        self.skipped += 1
        print(f"  SKIP  {name} - {why}")

    def report(self) -> int:
        print(f"\n{self.passed} passed / {self.failed} failed "
              f"/ {self.skipped} skipped")
        return 1 if self.failed else 0


# ── the detector ────────────────────────────────────────────────────────────
def unterminated_literals(js: str) -> list[tuple[int, str]]:
    """Line numbers where a ' or " string literal is still open at the newline.

    Backtick templates are left alone: those span lines legitimately. A `/`
    is read as starting a regex literal when what precedes it can only be an
    operator, which is the usual heuristic and enough for this page.
    """
    out: list[tuple[int, str]] = []
    quote = ""           # "", "'", '"', "`"
    comment = ""         # "", "//", "/*"
    regex = False
    line_no = 1
    line_start = 0
    prev_sig = ""        # last significant character before the cursor
    i = 0
    while i < len(js):
        c = js[i]
        if c == "\n":
            if quote in ("'", '"'):
                out.append((line_no, js[line_start:i]))
                quote = ""
            if comment == "//":
                comment = ""
            regex = False
            line_no += 1
            line_start = i + 1
            i += 1
            continue
        if comment == "//":
            i += 1
            continue
        if comment == "/*":
            if js.startswith("*/", i):
                comment = ""
                i += 2
                continue
            i += 1
            continue
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = ""
            i += 1
            continue
        if regex:
            if c == "\\":
                i += 2
                continue
            if c == "/":
                regex = False
            i += 1
            continue
        if js.startswith("//", i):
            comment = "//"
            i += 2
            continue
        if js.startswith("/*", i):
            comment = "/*"
            i += 2
            continue
        if c == "/" and prev_sig in "=(,:[!&|?{};+" :
            regex = True
            i += 1
            continue
        if c in "'\"`":
            quote = c
            i += 1
            prev_sig = c
            continue
        if not c.isspace():
            prev_sig = c
        i += 1
    return out


def rendered_page() -> str:
    """The page exactly as the route serves it."""
    import flask
    import admin_routes
    app = flask.Flask(__name__)
    with app.app_context():
        return flask.render_template_string(admin_routes._PAGE)


def scripts(html: str) -> list[str]:
    return re.findall(r"<script>(.*?)</script>", html, re.S)


# ── the checks ──────────────────────────────────────────────────────────────
def check_the_detector_detects(r: Results) -> None:
    """A check that cannot fail protects nothing. Prove this one can."""
    print("\n-- the detector itself --")
    good = 'const a = "x";\nconst b = `multi\nline`;\nconst e = s.replace(/"/g, "");\n'
    r.check("clean script is reported clean",
            unterminated_literals(good) == [], unterminated_literals(good))

    r.check("a template literal spanning lines is not flagged",
            unterminated_literals("const b = `one\ntwo`;\n") == [],
            "backticks legitimately span lines; flagging them would make "
            "this test unusable and it would be deleted")

    broken = 'logEl.textContent = (job.log || []).join("\n");\n'
    found = unterminated_literals(broken)
    # Both halves are genuinely cut - the opening line and the orphaned
    # `");` after it - so two reports is right. What matters is that the
    # line where the escape was written is among them.
    r.check("the exact shipped defect is flagged",
            found and found[0][0] == 1, repr(found))

    r.check("an apostrophe inside a double-quoted string is not flagged",
            unterminated_literals('confirm("today\'s snapshot?");\n') == [],
            "the real page says \"today's\"; a naive quote-counter trips on "
            "it and would have to be ignored")

    r.check("a comment containing a quote is not flagged",
            unterminated_literals("// it's fine\nconst a = 1;\n") == [],
            "apostrophes in commentary are the most common false positive")


def check_rendered_script_parses(r: Results) -> None:
    print("\n-- the page as it is actually served --")
    html = rendered_page()
    blocks = scripts(html)
    if not r.check("the page has a script block", len(blocks) == 1,
                   f"found {len(blocks)}"):
        return
    js = blocks[0]

    bad = unterminated_literals(js)
    r.check("no string literal is cut in half by a newline",
            bad == [],
            "\n".join(f"line {n}: {t.strip()[:72]}" for n, t in bad)
            + "\n(a Python escape was processed before the browser saw it; "
              "write \\\\n in the Python source so the JS receives \\n)")

    r.check("no raw tab or carriage return reached the script",
            "\t" not in js and "\r" not in js,
            "a processed \\t or \\r corrupts a literal the same way")


def check_rendering_added_no_lines(r: Results) -> None:
    """The precise signature of this defect, with no JS parsing at all.

    Python turning one escape into a newline makes the served script one
    line longer than the source it came from. Nothing else about rendering
    this page changes its line count, so any difference IS an eaten escape.
    """
    print("\n-- rendering must not change the line count --")
    src = open(os.path.join(REPO, "admin_routes.py"), encoding="utf-8").read()
    src_js = scripts(src)
    out_js = scripts(rendered_page())
    if not r.check("one block in each", len(src_js) == len(out_js) == 1,
                   f"source={len(src_js)} rendered={len(out_js)}"):
        return
    a, b = src_js[0].count("\n"), out_js[0].count("\n")
    r.check("served script has the same number of lines as its source",
            a == b,
            f"source {a} lines, served {b} lines - {b - a} escape(s) were "
            f"consumed by Python before the browser saw them")


def check_the_handlers_survive(r: Results) -> None:
    """The symptom the user saw, stated as a property."""
    print("\n-- every onclick names a function the script defines --")
    html = rendered_page()
    js = scripts(html)[0]
    # Both forms are in use on this page: `function f()` and `const f = ...`.
    # Counting only the first reports esc as undefined, which it is not.
    defined = set(re.findall(r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", js))
    defined |= set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", js))
    called = set()
    for attr in re.findall(r'on(?:click|change|input|submit)="([^"]*)"', html):
        called.update(re.findall(r"([A-Za-z_$][\w$]*)\s*\(", attr))
    missing = sorted(called - defined - {"return"})
    r.check("no handler refers to an undefined function",
            not missing,
            f"{missing} - the button calls it and nothing defines it")

    r.check("pscRefresh in particular is defined",
            "pscRefresh" in defined,
            "this is the Fetch latest snapshot button; when the script "
            "failed to parse, this was the function that went missing")


def check_node_agrees(r: Results) -> None:
    """An independent channel: a real JavaScript parser, if one is here."""
    print("\n-- an actual JS engine, as a second opinion --")
    node = shutil.which("node") or shutil.which("nodejs")
    if not node:
        r.skip("node --check on the rendered script", "node is not installed")
        return
    js = scripts(rendered_page())[0]
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(js)
        path = fh.name
    try:
        p = subprocess.run([node, "--check", path], capture_output=True,
                           text=True, timeout=60)
        r.check("node parses the served script",
                p.returncode == 0,
                (p.stderr or p.stdout).strip()[:600])
    finally:
        os.unlink(path)


def main() -> int:
    r = Results()
    print("test_admin_page_js.py - JavaScript that survives being a Python "
          "string")
    check_the_detector_detects(r)
    check_rendered_script_parses(r)
    check_rendering_added_no_lines(r)
    check_the_handlers_survive(r)
    check_node_agrees(r)
    return r.report()


if __name__ == "__main__":
    raise SystemExit(main())
