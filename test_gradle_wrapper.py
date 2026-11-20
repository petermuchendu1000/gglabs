#!/usr/bin/env python3
"""The Gradle wrapper must be invocable on the platform the build runs on.

    python3 test_gradle_wrapper.py

Two defects stopped stage build from ever running on Linux:

  1. generate.py selected "gradlew.bat" unconditionally. On Linux that file
     EXISTS, so the os.path.exists guard passed, and subprocess.run then
     raised - errno 13 without the exec bit, errno 8 (Exec format error)
     with it - so the pipeline died on an uncaught exception rather than
     returning 1.
  2. No POSIX gradlew had ever been committed, on any branch.

The non-obvious part is the third check below. The first POSIX gradlew I
produced came from Gradle 8.14.3, whose script launches `java -jar
gradle-wrapper.jar`. The committed jar's manifest has no Main-Class, so
that script fails with "no main manifest attribute" - it would have
replaced one broken Linux build with another. The requirement is therefore
not "ship any gradlew" but "ship one whose launch mechanism matches the jar
that is actually committed", and that is derived from the jar here rather
than hardcoded, so replacing the jar cannot silently invalidate it.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import types
import zipfile
from pathlib import Path

REPO = Path(__file__).parent
TEMPLATE = REPO / "apps" / "template-shift-journal"
POSIX_WRAPPER = TEMPLATE / "gradlew"
WIN_WRAPPER = TEMPLATE / "gradlew.bat"
WRAPPER_JAR = TEMPLATE / "gradle" / "wrapper" / "gradle-wrapper.jar"
WRAPPER_PROPS = TEMPLATE / "gradle" / "wrapper" / "gradle-wrapper.properties"

MAIN_CLASS = "org.gradle.wrapper.GradleWrapperMain"


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


def check_files(r: Results) -> None:
    print("\n-- both wrapper scripts are present and tracked --")
    r.check("POSIX gradlew exists", POSIX_WRAPPER.exists(), str(POSIX_WRAPPER))
    r.check("Windows gradlew.bat still exists", WIN_WRAPPER.exists(),
            "the Windows path must not have been removed")
    r.check("the wrapper jar exists", WRAPPER_JAR.exists(), str(WRAPPER_JAR))

    out = subprocess.run(["git", "ls-files"], cwd=REPO,
                         capture_output=True, text=True)
    tracked = set(out.stdout.split())
    for p in (POSIX_WRAPPER, WIN_WRAPPER, WRAPPER_JAR, WRAPPER_PROPS):
        rel = str(p.relative_to(REPO))
        r.check(f"tracked in git: {p.name}", rel in tracked,
                "an untracked wrapper is absent from a fresh clone, which is "
                "exactly how the POSIX script came to be missing")

    # Without the exec bit, subprocess raises PermissionError and the guard
    # in run_gradle cannot see it coming.
    r.check("the POSIX gradlew is executable",
            POSIX_WRAPPER.exists() and os.access(POSIX_WRAPPER, os.X_OK),
            f"mode is {oct(POSIX_WRAPPER.stat().st_mode & 0o777) if POSIX_WRAPPER.exists() else 'n/a'};"
            " git stores the exec bit when core.fileMode is true")


def check_launch_mechanism(r: Results) -> None:
    print("\n-- the script's launch mechanism matches the committed jar --")
    if not r.check("the wrapper jar is readable", WRAPPER_JAR.exists()):
        return
    with zipfile.ZipFile(WRAPPER_JAR) as z:
        names = set(z.namelist())
        manifest = z.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")

    has_main_class = bool(re.search(r"^Main-Class:", manifest, re.M))
    has_wrapper_main = f"{MAIN_CLASS.replace('.', '/')}.class" in names
    print(f"  (jar: Main-Class={'present' if has_main_class else 'ABSENT'}, "
          f"GradleWrapperMain.class={'present' if has_wrapper_main else 'ABSENT'})")

    r.check("the jar provides GradleWrapperMain", has_wrapper_main,
            "without it neither launch mechanism can work")

    if not r.check("the POSIX gradlew is readable", POSIX_WRAPPER.exists()):
        return
    script = POSIX_WRAPPER.read_text(encoding="utf-8", errors="replace")

    uses_jar_flag = re.search(r"-jar\s+\"?\$APP_HOME/gradle/wrapper/"
                              r"gradle-wrapper\.jar", script) is not None
    names_main = MAIN_CLASS in script

    if has_main_class:
        # A jar with Main-Class supports either form; require only that the
        # script launches the wrapper somehow.
        r.check("the script launches the wrapper", uses_jar_flag or names_main,
                "script references neither -jar nor the main class")
    else:
        # This is the committed state. `java -jar` cannot work here.
        r.check("the script does NOT rely on java -jar "
                "(the jar declares no Main-Class)", not uses_jar_flag,
                "this script launches `java -jar gradle-wrapper.jar`, which "
                "fails with 'no main manifest attribute' against this jar")
        r.check("the script launches GradleWrapperMain by class name",
                names_main,
                f"expected {MAIN_CLASS} in the script")

    # The distribution the wrapper will fetch must still be pinned.
    props = WRAPPER_PROPS.read_text(encoding="utf-8")
    m = re.search(r"distributionUrl=.*gradle-([0-9.]+)-bin\.zip", props)
    r.check("distributionUrl pins an explicit Gradle version", m is not None,
            f"got {props!r}")
    if m:
        print(f"  (pinned distribution: gradle-{m.group(1)})")


def _stub_built(flavor: str):
    company = types.SimpleNamespace(flavor=flavor)
    return types.SimpleNamespace(company=company)


def check_selection(r: Results) -> None:
    print("\n-- run_gradle selects the wrapper by platform --")
    sys.path.insert(0, str(REPO))
    import apps.generator.generate as gen

    recorded: list[list[str]] = []

    # run_gradle used subprocess.run and now uses Popen, so that Gradle's
    # output can be RELAYED through print() instead of inherited: api_v2
    # captures a stage by replacing sys.stdout, which leaves fd 1 alone, so an
    # inherited stream goes to the journal and the job drawer shows only an
    # exit code. This double must follow that change, and when it did not, the
    # patch stopped intercepting and this test shelled out to a real nine
    # second Gradle build - which is how the change was noticed.
    class FakeProc:
        def __init__(self, lines=("BUILD SUCCESSFUL\n",), code=0):
            self.stdout = iter(lines)
            self._code = code

        def wait(self):
            return self._code

    def fake_popen(cmd, **kw):
        recorded.append(list(cmd))
        return FakeProc()

    # Collect inside the patched block, assert outside it. pathlib picks
    # WindowsPath vs PosixPath from os.name at call time, so touching Path()
    # while os.name is "nt" raises NotImplementedError on Linux - on 3.11,
    # though not on 3.13, which is how this first passed and then failed.
    # Only string operations are used below for the same reason.
    results: list[tuple[str, int, list[str]]] = []
    saved_run, saved_name = gen.subprocess.Popen, os.name
    try:
        gen.subprocess.Popen = fake_popen
        for name in ("posix", "nt"):
            recorded.clear()
            os.name = name
            rc = gen.run_gradle([_stub_built("c13510663")],
                                variant="Release", artifact="aab")
            results.append((name, rc, list(recorded[0]) if recorded else []))
    finally:
        gen.subprocess.Popen = saved_run
        os.name = saved_name

    for (name, rc, argv), want in zip(results, ("gradlew", "gradlew.bat")):
        if not r.check(f"os.name={name!r}: run_gradle reached subprocess",
                       bool(argv) and rc == 0,
                       f"argv={argv} rc={rc}; a return of 1 means the "
                       "existence guard rejected the wrapper"):
            continue
        r.check(f"os.name={name!r}: chose {want}",
                argv[0].endswith(os.sep + want),
                f"chose {argv[0]!r}")
        r.check(f"os.name={name!r}: task name is well-formed",
                ":app:bundleC13510663Release" in argv, f"argv={argv}")

    r.check("os.name was restored after the test", os.name == saved_name,
            f"os.name is now {os.name!r}")


def main() -> int:
    r = Results()
    print(f"repo: {REPO}")
    print(f"running on os.name={os.name!r}")
    check_files(r)
    check_launch_mechanism(r)
    check_selection(r)
    return r.report()


if __name__ == "__main__":
    raise SystemExit(main())
