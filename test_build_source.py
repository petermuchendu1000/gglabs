#!/usr/bin/env python3
"""Stage 5 must build from the same store as every other stage.

    python3 test_build_source.py

Hermetic: the database is stubbed, no workbook is read unless a test asks
for one.

apps/generator/catalog.load_companies used to default to
pipeline_output/companies_pipeline.xlsx. Nothing writes that sheet from the
database - db.py's CLI has only --diff and --import, both Excel to
database, and there is no export - so the app build and the rest of the
pipeline read two different stores. On a server where the sheet had never
been produced, stage 5 raised FileNotFoundError. The worse case is the
quiet one: where an OLD sheet exists, stage 5 would build a stale set of
companies while every other stage used the database, and nothing would say
so.

The sheet is still readable when a caller explicitly asks for one, because
the standalone tools work that way. None of the three in-repo callers does.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).parent
sys.path.insert(0, str(REPO))

os.environ.setdefault("DATABASE_URL", "postgresql://unused@127.0.0.1:1/none")
os.environ.setdefault("LOG_LEVEL", "CRITICAL")

import openpyxl  # noqa: E402

from apps.generator import catalog  # noqa: E402


class Results:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []

    def check(self, name: str, cond: bool, detail: str = "") -> bool:
        if cond:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed.append(name)
            print(f"  FAIL  {name}" + (f"\n          {detail}" if detail else ""))
        return bool(cond)

    def report(self) -> int:
        print()
        print(f"{self.passed} passed / {len(self.failed)} failed")
        for f in self.failed:
            print(f"  failed: {f}")
        return 1 if self.failed else 0


HEADERS = ["Company Number", "Company Name", "SIC Codes", "Domain",
           "Assigned Email", "Short Name", "Address"]

ROWS = [
    {"Company Number": "16940106",
     "Company Name": "ACME ADVISORY CONSULTING MANAGEMENT LTD",
     "SIC Codes": "70229,62012", "Domain": "acmeadvisory.uk",
     "Assigned Email": "pool@example.com", "Short Name": "Acme Advisory",
     "Address": "1 Example St, London"},
    {"Company Number": "123", "Company Name": "SHORT NUMBER LTD",
     "SIC Codes": "62012", "Domain": "", "Assigned Email": "fallback@example.com",
     "Short Name": "", "Address": ""},
    {"Company Number": "", "Company Name": "NO NUMBER LTD", "SIC Codes": "",
     "Domain": "", "Assigned Email": "", "Short Name": "", "Address": ""},
]


def _workbook(rows) -> Path:
    tmp = Path(tempfile.mkdtemp()) / "companies_pipeline.xlsx"
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(HEADERS)
    for r in rows:
        ws.append([r.get(h, "") for h in HEADERS])
    wb.save(tmp)
    return tmp


def _shape(cs):
    return [(c.company_number, c.company_name, c.sic_codes, c.domain,
             c.support_email, c.archetype, c.short_name, c.address,
             c.display_name, c.application_id, c.flavor) for c in cs]


def check_default_source(r: Results) -> None:
    print("\n-- with no arguments it reads the database --")
    saved = catalog._rows_from_db
    catalog._rows_from_db = lambda: [dict(x) for x in ROWS]
    try:
        cs = catalog.load_companies()
        r.check("companies are returned", len(cs) == 2, f"got {len(cs)}")
        r.check("...from the database, not a workbook",
                [c.company_number for c in cs] == ["16940106", "00000123"],
                f"got {[c.company_number for c in cs]}")

        # The regression guard. If the default path ever reads a workbook
        # again this raises, which is the whole point: on a server with no
        # sheet the old code raised FileNotFoundError, and with a STALE
        # sheet it would silently build the wrong companies.
        saved_wb = openpyxl.load_workbook

        def _refuse(*a, **k):
            raise AssertionError("load_companies() opened a workbook")

        openpyxl.load_workbook = _refuse
        try:
            cs2 = catalog.load_companies()
            opened = False
        except AssertionError:
            opened = True
        finally:
            openpyxl.load_workbook = saved_wb
        r.check("...and opens NO workbook at all", not opened,
                "the default source must not touch the filesystem")

        r.check("the default is no longer a path",
                catalog.load_companies.__defaults__[0] is None,
                f"default excel_path={catalog.load_companies.__defaults__[0]!r}")
    finally:
        catalog._rows_from_db = saved


def check_equivalence(r: Results) -> None:
    print("\n-- the two sources must agree, or the switch changed behaviour --")
    saved = catalog._rows_from_db
    catalog._rows_from_db = lambda: [dict(x) for x in ROWS]
    try:
        from_db = catalog.load_companies()
        from_xl = catalog.load_companies(excel_path=str(_workbook(ROWS)))
        r.check("the database and a workbook give identical companies",
                _shape(from_db) == _shape(from_xl),
                f"db={_shape(from_db)}\n          xl={_shape(from_xl)}")
        r.check("the workbook escape hatch still works", len(from_xl) == 2,
                f"got {len(from_xl)} - the standalone tools rely on it")

        # The equivalence check above feeds BOTH sources the same rows, so
        # it passes even if excel_path is ignored and the database is read
        # both times. This one can tell them apart: the workbook holds a
        # company the stubbed database does not.
        other = _workbook([{"Company Number": "99999999",
                            "Company Name": "ONLY IN THE WORKBOOK LTD",
                            "SIC Codes": "62012", "Domain": "wb.example",
                            "Assigned Email": "", "Short Name": "", "Address": ""}])
        got = catalog.load_companies(excel_path=str(other))
        r.check("excel_path actually reads THAT workbook",
                [c.company_number for c in got] == ["99999999"],
                f"got {[c.company_number for c in got]} - if this is the "
                f"database's rows, excel_path is being ignored")
        r.check("...and the database is untouched for that call",
                [c.company_name for c in got] == ["ONLY IN THE WORKBOOK LTD"],
                f"got {[c.company_name for c in got]}")
    finally:
        catalog._rows_from_db = saved


def check_fields(r: Results) -> None:
    print("\n-- the fields the app build depends on --")
    saved = catalog._rows_from_db
    catalog._rows_from_db = lambda: [dict(x) for x in ROWS]
    try:
        cs = {c.company_number: c for c in catalog.load_companies()}
        a, b = cs["16940106"], cs["00000123"]

        # The support address is the dev@ forward stage 3 creates on the
        # company's own domain, so it carries the domain by construction.
        r.check("support_email is dev@ the company's own domain",
                a.support_email == "dev@acmeadvisory.uk", a.support_email)
        r.check("...falling back to the assigned email with no domain",
                b.support_email == "fallback@example.com", b.support_email)
        r.check("a row with no company number is skipped", "" not in cs)
        r.check("a short number is zero-padded to 8",
                "00000123" in cs, f"got {sorted(cs)}")
        r.check("Short Name drives the launcher label",
                a.display_name == "Acme Advisory", a.display_name)
        r.check("...and without one it derives from the company name",
                b.display_name == "Short Number", b.display_name)
        r.check("the address is carried", a.address == "1 Example St, London")
        r.check("the SIC codes are carried", a.sic_codes == "70229,62012")
        r.check("the archetype comes from the primary SIC",
                a.archetype == catalog.SIC_ARCHETYPE.get("70229",
                                                         catalog.DEFAULT_ARCHETYPE),
                a.archetype)

        # No domain means no app can be built for it - stage 5 filters on
        # c.domain (pipeline.py), so this must stay falsy rather than
        # becoming a placeholder.
        r.check("a company with no domain keeps an empty domain",
                b.domain == "", f"got {b.domain!r}")
    finally:
        catalog._rows_from_db = saved


def check_filters(r: Results) -> None:
    print("\n-- limit and company_numbers --")
    saved = catalog._rows_from_db
    catalog._rows_from_db = lambda: [dict(x) for x in ROWS]
    try:
        r.check("limit caps the result", len(catalog.load_companies(limit=1)) == 1)
        got = catalog.load_companies(company_numbers=["16940106"])
        r.check("company_numbers selects one",
                [c.company_number for c in got] == ["16940106"],
                f"got {[c.company_number for c in got]}")
        got = catalog.load_companies(company_numbers=["123"])
        r.check("...and an unpadded number still matches the padded row",
                [c.company_number for c in got] == ["00000123"],
                f"got {[c.company_number for c in got]} - the filter set is "
                f"padded the same way the rows are")
        got = catalog.load_companies(company_numbers=["99999999"])
        r.check("an unknown number selects nothing", got == [], f"got {got}")
    finally:
        catalog._rows_from_db = saved


def check_callers(r: Results) -> None:
    print("\n-- no caller pins the old workbook default --")
    # If one did, that caller would keep reading the stale sheet while the
    # others read the database, which is the divergence being removed.
    import re
    for fn in ("pipeline.py", "apps/generator/generate.py",
               "apps/generator/catalog.py"):
        src = (REPO / fn).read_text(encoding="utf-8")
        # `(?<!def )` so the function DEFINITION in catalog.py is not
        # mistaken for a call - its own signature names excel_path.
        calls = re.findall(r"(?<!def )load_companies\((.*?)\)", src, re.S)
        bad = [c for c in calls if "excel_path" in c]
        r.check(f"{fn} does not pass excel_path", not bad, f"found {bad}")
    src = (REPO / "apps/generator/catalog.py").read_text(encoding="utf-8")
    r.check("EXCEL_PATH_DEFAULT is no longer the function default",
            "excel_path: str | None = None" in src,
            "the default must be the database")


def check_gradle_output_is_relayed(r: Results) -> None:
    """A failing build must say WHY, wherever it is being watched from.

    Observed on the server: the app stage reported "Gradle exit=1 in 0.0s"
    and nothing else. run_gradle used subprocess.run with no pipes, which
    hands the child the real file descriptors - while api_v2 captures a
    stage's output by replacing sys.stdout, a Python-level object that leaves
    fd 1 alone. So every line Gradle wrote went to the service journal and the
    job drawer showed a failure with no reason. The reason had been printed,
    to somewhere nobody was reading.
    """
    import io
    import types
    from contextlib import redirect_stdout

    from apps.generator import generate

    print("\n-- a failing Gradle run explains itself --")

    class _C:
        company_number = "1"
        company_name = "X"
        flavor = "c1"

    def run_with(script: str) -> tuple[int, str]:
        d = tempfile.mkdtemp()
        g = os.path.join(d, "gradlew")
        with open(g, "w", encoding="utf-8") as f:
            f.write(script)
        os.chmod(g, 0o755)
        saved = generate.TEMPLATE_ROOT
        generate.TEMPLATE_ROOT = d
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                code = generate.run_gradle(
                    [types.SimpleNamespace(company=_C())])
            return code, buf.getvalue()
        finally:
            generate.TEMPLATE_ROOT = saved

    # What the server actually did: non-zero, instantly, in total silence.
    code, out = run_with("#!/bin/sh\nexit 1\n")
    r.check("a silent non-zero exit is reported as such", code == 1,
            f"exit {code}")
    r.check("...and is explicitly NOT called a compile error",
            "no output at all" in out and "nothing was compiled" in out,
            f"the operator is left guessing: {out!r}")
    r.check("...and a command to reproduce it is given",
            "--no-daemon --version" in out, repr(out))

    # The control: a wrapper that DOES speak must have its words relayed
    # through print(), which is what the dashboard's stdout capture sees.
    code, out = run_with(
        '#!/bin/sh\necho "Could not reserve enough space" >&2\nexit 1\n')
    r.check("stderr from the wrapper is relayed",
            "Could not reserve enough space" in out,
            "the reason is still invisible from the dashboard: " + repr(out))
    r.check("...and that run is not mislabelled as silent",
            "no output at all" not in out,
            "a build that explained itself was reported as saying nothing")

    code, out = run_with('#!/bin/sh\necho "BUILD SUCCESSFUL"\nexit 0\n')
    r.check("a successful run relays its output too and returns 0",
            code == 0 and "BUILD SUCCESSFUL" in out, f"{code} {out!r}")

    # A wrapper that cannot be executed must return 1, not raise: it used to
    # escape as an uncaught exception from inside a worker thread.
    d = tempfile.mkdtemp()
    g = os.path.join(d, "gradlew")
    with open(g, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\nexit 0\n")
    os.chmod(g, 0o600)                      # present, not executable
    saved = generate.TEMPLATE_ROOT
    generate.TEMPLATE_ROOT = d
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            code = generate.run_gradle([types.SimpleNamespace(company=_C())])
    except Exception as e:                  # noqa: BLE001
        code, buf = None, io.StringIO()
        r.check("a non-executable wrapper does not raise", False,
                f"{type(e).__name__}: {e}")
    finally:
        generate.TEMPLATE_ROOT = saved
    if code is not None:
        r.check("a non-executable wrapper returns 1 instead of raising",
                code == 1, f"exit {code}")
        r.check("...and names the file and the errno",
                "could not start" in buf.getvalue(), repr(buf.getvalue()))


def check_broken_java_home_is_dropped(r: Results) -> None:
    """A JAVA_HOME with no JDK behind it must not be handed to Gradle.

    Seen twice on the server: .env carried a developer's Windows JAVA_HOME
    (C:\\Program Files\\Android\\Android Studio\\jbr), Gradle preferred it
    over PATH, and every build died before compiling anything. There is no
    reading under which a JAVA_HOME containing no bin/java is intentional -
    it cannot work, for anyone, ever - so the build drops it for the child
    process and says so loudly rather than failing and leaving the operator
    to find it.

    Narrow on purpose: a VALID JAVA_HOME is passed through untouched. Picking
    a JDK is the operator's business whenever their choice can actually run.
    """
    import io
    import types
    from contextlib import redirect_stdout

    from apps.generator import generate

    print("\n-- a JAVA_HOME with no JDK is dropped, loudly --")

    class _C:
        company_number = "1"
        company_name = "X"
        flavor = "c1"

    def run(java_home):
        d = tempfile.mkdtemp()
        g = os.path.join(d, "gradlew")
        with open(g, "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\necho "SAW_JAVA_HOME=[${JAVA_HOME:-unset}]"\n'
                    "exit 0\n")
        os.chmod(g, 0o755)
        saved_root = generate.TEMPLATE_ROOT
        saved_jh = os.environ.get("JAVA_HOME")
        generate.TEMPLATE_ROOT = d
        if java_home is None:
            os.environ.pop("JAVA_HOME", None)
        else:
            os.environ["JAVA_HOME"] = java_home
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                generate.run_gradle([types.SimpleNamespace(company=_C())])
            return buf.getvalue()
        finally:
            generate.TEMPLATE_ROOT = saved_root
            os.environ.pop("JAVA_HOME", None)
            if saved_jh is not None:
                os.environ["JAVA_HOME"] = saved_jh

    out = run(r"C:\Program Files\Android\Android Studio\jbr")
    r.check("the child does NOT receive a broken JAVA_HOME",
            "SAW_JAVA_HOME=[unset]" in out,
            "Gradle would prefer it over PATH and refuse to start: " + repr(out))
    r.check("...and the drop is announced", "JAVA_HOME" in out and
            "bin/java" in out, repr(out))
    r.check("...and the operator is pointed at the source",
            ".env" in out, "nothing says where to fix it: " + repr(out))

    # The control: a REAL JDK layout must be passed through untouched, or
    # this is just "always unset JAVA_HOME", which would override a
    # deliberate choice of JDK.
    jdk = tempfile.mkdtemp()
    os.makedirs(os.path.join(jdk, "bin"), exist_ok=True)
    with open(os.path.join(jdk, "bin", "java"), "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\nexit 0\n")
    os.chmod(os.path.join(jdk, "bin", "java"), 0o755)
    try:
        out = run(jdk)
        r.check("a valid JAVA_HOME is passed through unchanged",
                f"SAW_JAVA_HOME=[{jdk}]" in out,
                "a deliberate JDK choice was overridden: " + repr(out))
        r.check("...and nothing is announced about it",
                "bin/java" not in out,
                "a working configuration was reported as a problem")
    finally:
        shutil.rmtree(jdk, ignore_errors=True)

    out = run(None)
    r.check("an unset JAVA_HOME stays unset and is not announced",
            "SAW_JAVA_HOME=[unset]" in out and "bin/java" not in out,
            repr(out))


def check_no_developer_machine_paths(r: Results) -> None:
    """A Windows path compiled into a module that runs on a Linux server.

    stage_build held two, and they were not inert defaults - they were the
    cause of two separate failures:

      java_home = os.environ.get("JAVA_HOME", r"C:\\Program Files\\...\\jbr")
      os.environ["JAVA_HOME"] = java_home

    That SETS JAVA_HOME, unconditionally, every time the stage runs. Deleting
    the line from .env could never have helped, because .env was never where
    it came from - and I spent two cycles telling the operator to look there.

      sdk = os.environ.get("ANDROID_HOME", r"C:\\Users\\LENOVO\\...\\Sdk")

    With ANDROID_HOME unset that is written into local.properties as sdk.dir,
    and the Android Gradle Plugin then dies inside getTargetBootClasspath
    trying to load a platform from a path that cannot exist here.

    A default that can only be correct on one person's laptop is not a
    default. It converts "this is not configured" - which an operator can
    see and fix - into "this is configured wrongly", which looks like a bug
    in the build.
    """
    print("\n-- no developer-machine paths compiled into the source --")
    pattern = re.compile(r"""r?["'][A-Za-z]:\\""")
    offenders = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(REPO):
        dirnames[:] = [d for d in dirnames
                       if d not in {".git", ".venv", "__pycache__", "e2e",
                                    "pipeline_output", "node_modules"}]
        for fn in filenames:
            if not fn.endswith(".py") or fn.startswith("test_"):
                continue
            path = os.path.join(dirpath, fn)
            scanned += 1
            for i, line in enumerate(
                    open(path, encoding="utf-8").read().splitlines(), 1):
                # Comment lines are skipped: a comment recording a path that
                # was REMOVED is the useful kind of mention, and banning it
                # would push the explanation out of the file that needs it.
                # The regex requires a quote immediately before the drive
                # letter, so prose in a docstring does not match either.
                # LIMIT: a Windows path inside a trailing inline comment on a
                # line of code would be missed. Nothing has ever had one.
                if line.lstrip().startswith("#"):
                    continue
                if pattern.search(line):
                    offenders.append(
                        f"{os.path.relpath(path, REPO)}:{i}: {line.strip()}")

    # The control: the walk must actually find modules, or this is vacuous.
    r.check(f"{scanned} modules scanned", scanned > 20,
            "the walk found almost nothing, so finding no offenders means "
            "nothing")
    r.check("no module carries a Windows absolute path", not offenders,
            "a default that can only be right on one laptop silently "
            "overrides correct server configuration:\n        "
            + "\n        ".join(offenders))


def check_stage_build_requires_android_home(r: Results) -> None:
    """Unset ANDROID_HOME must stop the stage, not fall back to a guess."""
    print("\n-- stage_build refuses to guess the SDK location --")
    import io
    from contextlib import redirect_stdout

    import pipeline

    def run(env):
        saved = {k: os.environ.get(k) for k in ("ANDROID_HOME", "JAVA_HOME")}
        for k in saved:
            os.environ.pop(k, None)
        os.environ.update({k: v for k, v in env.items() if v is not None})
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                pipeline.stage_build(company_numbers=["00000001"])
            return buf.getvalue()
        finally:
            for k in saved:
                os.environ.pop(k, None)
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    out = run({})
    r.check("it stops when ANDROID_HOME is unset",
            "ANDROID_HOME" in out, repr(out[-400:]))
    r.check("...and never reaches the database",
            "Building" not in out,
            "it carried on with a guessed SDK path: " + repr(out[-400:]))

    # JAVA_HOME must be left exactly as found - not set, not overwritten.
    saved_jh = os.environ.get("JAVA_HOME")
    try:
        os.environ.pop("JAVA_HOME", None)
        run({})
        r.check("an unset JAVA_HOME is still unset afterwards",
                "JAVA_HOME" not in os.environ,
                f"the stage injected JAVA_HOME={os.environ.get('JAVA_HOME')!r}")
        os.environ["JAVA_HOME"] = "/sentinel/jdk"
        run({"JAVA_HOME": "/sentinel/jdk"})
        r.check("an existing JAVA_HOME is left untouched",
                os.environ.get("JAVA_HOME") == "/sentinel/jdk",
                f"it became {os.environ.get('JAVA_HOME')!r}")
    finally:
        os.environ.pop("JAVA_HOME", None)
        if saved_jh is not None:
            os.environ["JAVA_HOME"] = saved_jh


def main() -> int:
    r = Results()
    print(f"target: apps/generator/catalog.load_companies (stage 5's source)")
    check_default_source(r)
    check_equivalence(r)
    check_fields(r)
    check_filters(r)
    check_callers(r)
    check_gradle_output_is_relayed(r)
    check_broken_java_home_is_dropped(r)
    check_no_developer_machine_paths(r)
    check_stage_build_requires_android_home(r)
    return r.report()


if __name__ == "__main__":
    raise SystemExit(main())
