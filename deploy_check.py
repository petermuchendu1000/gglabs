#!/usr/bin/env python3
"""Deployment self-check. Read-only: it creates and changes nothing.

Run on the server after `git pull`:

    cd /opt/company_registration
    xvfb-run -a .venv/bin/python deploy_check.py

Exists because the alternative is pasting a long script into a terminal,
and a noVNC console silently drops and reorders keystrokes on a large
paste - which produced a SyntaxError that looked like a code fault and was
really a transport fault. A checked-in script is pasted as one short line
and is repeatable on every future deploy.

Each check states what would FALSIFY it rather than just printing a tick.
The database section is skipped, not failed, when DATABASE_URL is unset, so
this is useful before as well as after the database is wired up.

Exit code is the number of failed checks, so `echo $?` is meaningful.
"""
from __future__ import annotations

import asyncio
import importlib.metadata as md
import os
import re
import shutil
import stat
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

RESULTS: list[tuple[str, str, str]] = []
BROWSER_TIMEOUT = 90


def load_env() -> str:
    """Honour .env the way every entry point does (app.py:14, db.py:37).

    Without this the database section reports SKIP on a correctly configured
    box: it reads os.environ before importing db, and db is what would
    otherwise have loaded .env. A check that says "not configured" about a
    configured system is worse than no check.
    """
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return "no .env file"
    try:
        from dotenv import load_dotenv
    except Exception as e:                      # pragma: no cover - defensive
        return f".env present but python-dotenv unusable: {e}"
    load_dotenv(path)
    mode = oct(os.stat(path).st_mode & 0o777)
    return f"loaded {path} (mode {mode})"


def record(name: str, state: str, detail: str = "") -> bool:
    RESULTS.append((name, state, detail))
    mark = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "SKIP"}[state]
    print(f"  [{mark}] {name}" + (f": {detail}" if detail else ""))
    return state != "FAIL"


def section(title: str) -> None:
    print()
    print(f"=== {title} ===")


# ── 1. Interpreter ──────────────────────────────────────────────────────────
def check_python() -> None:
    section("1. Interpreter")
    v = sys.version_info
    # Pillow 13.0.0 requires >= 3.11 and Debian 12 ships exactly 3.11, so this
    # has no margin: an older base image breaks the pinned dependency set.
    record("python >= 3.11 (Pillow 13 requires it)",
           "PASS" if v >= (3, 11) else "FAIL",
           f"{v.major}.{v.minor}.{v.micro}")
    # sys.prefix differs from base_prefix inside any virtualenv. Testing for
    # the literal string ".venv" in the path instead would fail for every
    # legitimately named environment, which is a false alarm, not a check.
    # What the pins are actually installed into is verified directly below.
    record("running inside a virtualenv, not system python",
           "PASS" if sys.prefix != sys.base_prefix else "FAIL",
           sys.executable)


# ── 2. Pinned dependencies ──────────────────────────────────────────────────
def check_pins() -> None:
    section("2. Pinned dependencies")
    lock = os.path.join(ROOT, "requirements.lock.txt")
    if not os.path.exists(lock):
        record("requirements.lock.txt present", "FAIL", lock)
        return
    want = {}
    with open(lock, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#")[0].strip()
            if "==" in line:
                n, v = line.split("==")
                want[n.strip().lower().replace("_", "-")] = v.strip()
    if not want:
        record("lock file contains pins", "FAIL", "no '==' lines found")
        return
    bad = []
    for n, v in sorted(want.items()):
        try:
            got = md.version(n)
        except Exception:
            got = None
        if got != v:
            bad.append(f"{n} want={v} got={got}")
    record(f"all {len(want)} pins match the lock file",
           "PASS" if not bad else "FAIL", "; ".join(bad))


# ── 3. The stealth import ───────────────────────────────────────────────────
def check_stealth_import() -> None:
    section("3. playwright-stealth API")
    # browser_stealth.py uses the v2 API: `from playwright_stealth import
    # Stealth`, Stealth(), apply_stealth_async. v1 exported a bare
    # stealth_async function and no Stealth class, so an unpinned install
    # that resolved to v1 raises ImportError right here and takes the whole
    # DUNS stage with it. This is the check that pin exists for.
    try:
        import browser_stealth as bs
    except Exception as e:
        record("browser_stealth imports", "FAIL", f"{type(e).__name__}: {e}")
        return
    record("browser_stealth imports", "PASS")
    ok = type(getattr(bs, "_stealth", None)).__name__ == "Stealth"
    record("stealth object is the v2 Stealth class",
           "PASS" if ok else "FAIL",
           type(getattr(bs, "_stealth", None)).__name__)
    record("apply_stealth_async present",
           "PASS" if hasattr(bs._stealth, "apply_stealth_async") else "FAIL")


# ── 4. Virtual display ──────────────────────────────────────────────────────
def check_display() -> bool:
    section("4. Virtual display")
    disp = os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    if not disp:
        record("DISPLAY is set", "FAIL",
               "run this under: xvfb-run -a .venv/bin/python deploy_check.py")
        return False
    record("DISPLAY is set", "PASS", disp)
    try:
        import browser_stealth as bs
        bs._require_display()
        record("browser_stealth._require_display() is satisfied", "PASS")
    except Exception as e:
        record("browser_stealth._require_display() is satisfied", "FAIL", str(e))
        return False
    return True


# ── 5. Headed Chromium ──────────────────────────────────────────────────────
async def _launch_headed() -> str:
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        try:
            page = await browser.new_page()
            await page.set_content("<h1 id=x>ok</h1>")
            text = await page.inner_text("#x")
            if text != "ok":
                raise RuntimeError(f"rendered {text!r}, expected 'ok'")
            return browser.version
        finally:
            await browser.close()


def check_browser(have_display: bool) -> None:
    section("5. Headed Chromium")
    if not have_display:
        record("headed chromium launches", "SKIP", "no display")
        return
    # HEADED, not headless. D&B blocks headless, so a headless-only box
    # passes every other check and still cannot do a DUNS lookup.
    try:
        version = asyncio.run(asyncio.wait_for(_launch_headed(),
                                               timeout=BROWSER_TIMEOUT))
        record("headed chromium launches and renders", "PASS", version)
    except asyncio.TimeoutError:
        record("headed chromium launches and renders", "FAIL",
               f"timed out after {BROWSER_TIMEOUT}s")
    except Exception as e:
        record("headed chromium launches and renders", "FAIL",
               f"{type(e).__name__}: {e}")


# ── 6. Database ─────────────────────────────────────────────────────────────
def check_database() -> None:
    section("6. Database")
    if not (os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")):
        record("DATABASE_URL is set", "SKIP",
               "not configured yet - everything above is still valid")
        return
    record("DATABASE_URL is set", "PASS")
    try:
        import db
    except Exception as e:
        record("db module imports", "FAIL", f"{type(e).__name__}: {e}")
        return

    try:
        with db._cursor() as cur:
            cur.execute("SELECT current_database() AS d, version() AS v")
            row = cur.fetchone()
        record("connects", "PASS", f"{row['d']}, {row['v'].split(',')[0]}")
    except Exception as e:
        record("connects", "FAIL", f"{type(e).__name__}: {e}")
        return

    try:
        with db._cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = %s",
                (db.TABLE,))
            present = {r["column_name"] for r in cur.fetchall()}
    except Exception as e:
        record(f"table '{db.TABLE}' readable", "FAIL", f"{type(e).__name__}: {e}")
        return

    if not present:
        record(f"table '{db.TABLE}' exists", "FAIL",
               "not created yet - run: .venv/bin/python db.py")
        return
    record(f"table '{db.TABLE}' exists", "PASS", f"{len(present)} columns")

    expected = {c for _, c in db.COLUMNS}
    missing = sorted(expected - present)
    record(f"all {len(expected)} mapped columns present",
           "PASS" if not missing else "FAIL",
           f"missing: {', '.join(missing)}" if missing else "")
    record("updated_at present",
           "PASS" if "updated_at" in present else "FAIL")

    try:
        rows = db.all_rows(use_cache=False)
        record("all_rows() round-trips", "PASS", f"{len(rows)} companies")
    except Exception as e:
        record("all_rows() round-trips", "FAIL", f"{type(e).__name__}: {e}")


# ── 7. The app-factory modules actually import ──────────────────────────────
def check_app_imports() -> None:
    """Import every apps/generator module and report what fails.

    check_pins asks whether the 11 locked distributions are installed, which
    is not the same question as "does the code import". Two defects got past
    it: privacy_policy.py imports Crypto (pycryptodome), which is in neither
    requirements file, and generate.py once used f-string syntax that is a
    SyntaxError before Python 3.12. assets.py:838 swallows the first and a
    build still reports success, so neither is visible without this.

    The list is read from the directory, so a new module needs no edit here.
    Modules that touch the database or .env at import - pipeline,
    run_pipeline, develop_app - are deliberately excluded: ensure_schema runs
    DDL, and this script promises to change nothing.
    """
    section("7. App-factory imports")
    pkg_dir = os.path.join(ROOT, "apps", "generator")
    if not os.path.isdir(pkg_dir):
        record("apps/generator present", "FAIL", pkg_dir)
        return

    names = sorted(
        f[:-3] for f in os.listdir(pkg_dir)
        if f.endswith(".py") and f != "__init__.py"
    )
    if not names:
        # An empty list would make every check below vacuously pass.
        record("apps/generator contains modules", "FAIL", "no .py files found")
        return

    import importlib
    bad = []
    for name in names:
        try:
            importlib.import_module(f"apps.generator.{name}")
        except Exception as e:
            bad.append(f"{name} ({type(e).__name__}: {e})")
    record(f"all {len(names)} apps/generator modules import",
           "PASS" if not bad else "FAIL", "; ".join(bad))


def owner_only(mode: int) -> bool:
    """Can only the owner read this? Then no non-root service user can.

    Deliberately decided from the MODE BITS, never from os.access(). As root
    os.access() returns True for a 0600 file owned by somebody else, so an
    access-based check passes for the very fault it is meant to catch - and
    deploy_check is usually run as root. A check that cannot fail where it is
    run is not a check.
    """
    return not (mode & (stat.S_IRGRP | stat.S_IROTH))


def owner_only_exec(mode: int) -> bool:
    """Can only the owner EXECUTE this? Then the service user cannot.

    The same trap as owner_only and it caught this file out: the first version
    of the Gradle wrapper check used os.access(X_OK), which as root returns
    True when merely u+x is set - so a wrapper the service user cannot exec
    would have passed, and run_gradle would raise errno 13 exactly as it did
    for delegate.py.
    """
    return not (mode & (stat.S_IXGRP | stat.S_IXOTH))


def world_readable(mode: int) -> bool:
    """Can anybody on the box read this? Correct for source, not for .env."""
    return bool(mode & stat.S_IROTH)


def _source_files() -> list[str]:
    skip = {".git", ".venv", "venv", "__pycache__", "node_modules",
            "pipeline_output", "logs"}
    out = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for f in filenames:
            if f.endswith((".py", ".js", ".html")):
                out.append(os.path.join(dirpath, f))
    return sorted(out)


def check_source_readable() -> None:
    """Every source file must be readable by the user the service runs as.

    THIS IS NOT HYPOTHETICAL. Observed 2026-11-20: the delegate stage failed
    from the dashboard with PermissionError on delegate.py while the same
    import worked perfectly from the console - because the console was root,
    and root ignores file modes. `git pull` under a umask of 077 creates new
    files 0600, so every module added by a deploy is unreadable by the service
    while every older one keeps working. The failure surfaces as a traceback
    inside one stage, which reads like a code fault and is not one.

    The .env check is the other half, and it is here on purpose: the fix for
    the fault above is a chmod, and `chmod -R a+r` over the repo would make
    the database password and every API key readable by any account on the
    box. One of these two checks catches the bug and the other catches the
    careless fix.
    """
    section("8. Source file permissions")
    if os.geteuid() == 0:
        # Not a failure - just the reason this check reads modes rather than
        # asking whether IT can open the files.
        record("running as root, so modes are read directly", "PASS",
               "os.access() would pass for every file here")

    files = _source_files()
    if not files:
        record("source files found to check", "FAIL",
               "nothing was walked, so every check below is vacuous")
        return
    record(f"{len(files)} source files found", "PASS")

    unreadable = [os.path.relpath(p, ROOT) for p in files
                  if owner_only(os.stat(p).st_mode)]
    record("every source file is readable by non-owner",
           "FAIL" if unreadable else "PASS",
           ("owner-only, so the service cannot import: "
            + ", ".join(unreadable[:12])) if unreadable else "")

    env = os.path.join(ROOT, ".env")
    if not os.path.exists(env):
        record(".env is not world-readable", "SKIP", "no .env file")
    else:
        mode = os.stat(env).st_mode
        record(".env is not world-readable",
               "FAIL" if world_readable(mode) else "PASS",
               f"mode {oct(mode & 0o777)} - every account on this box can "
               f"read the database password and every API key"
               if world_readable(mode) else f"mode {oct(mode & 0o777)}")


def java_major(version_output: str) -> int | None:
    """The major version from `java -version` output, or None.

    Two formats, because Java changed its scheme at 9: 'openjdk version
    "17.0.13"' is 17, and 'java version "1.8.0_392"' is 8, not 1. Reading the
    first component of the latter gives 1, which compares as older than
    everything and would report a usable JDK as unusable - or, with the
    comparison the other way, an unusable one as fine.
    """
    m = re.search(r'version "([0-9][0-9._]*)', version_output or "")
    if not m:
        return None
    parts = m.group(1).split(".")
    try:
        if parts[0] == "1" and len(parts) > 1:
            return int(parts[1])
        return int(parts[0])
    except ValueError:
        return None


def required_compile_sdk() -> int | None:
    """compileSdk from the app template, so this check cannot go stale.

    Hardcoding 35 here would keep passing after somebody bumps the template,
    and the build would then fail on the server for a platform nobody checked
    for.
    """
    path = os.path.join(ROOT, "apps", "template-shift-journal", "app",
                        "build.gradle.kts")
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return None
    m = re.search(r"compileSdk\s*=\s*(\d+)", text)
    return int(m.group(1)) if m else None


def check_build_toolchain() -> None:
    """What the app stage needs, checked before the app stage needs it.

    Observed 2026-11-20: a run reached the app stage - every earlier stage
    green - and died on

      FileNotFoundError: keytool not found - install a JDK or set JAVA_HOME

    raised from keystore_mint, which signs every flavour. The whole chain had
    to run, including a billed domain purchase, to discover a missing package.
    Nothing before this section asked whether the build toolchain existed.

    The Android SDK is checked too, because it is the very next thing that
    would have failed: generate.py writes local.properties from ANDROID_HOME
    and defaults it to ~/Android/Sdk, a path a systemd service user does not
    normally have.
    """
    section("9. App build toolchain (the app stage)")

    keytool = shutil.which("keytool")
    record("keytool is on PATH", "PASS" if keytool else "FAIL",
           keytool or "keystore_mint cannot sign anything; install a JDK")

    java = shutil.which("java")
    if not java:
        record("java is on PATH", "FAIL", "Gradle cannot run")
    else:
        record("java is on PATH", "PASS", java)
        try:
            p = subprocess.run([java, "-version"], capture_output=True,
                               text=True, timeout=30)
            major = java_major(p.stderr + p.stdout)
        except Exception as e:                  # pragma: no cover - defensive
            major = None
            record("java -version could be read", "FAIL", str(e))
        if major is not None:
            # AGP 8.7.3 and jvmTarget 17 in the template. 17 is the floor.
            record("java is 17 or newer (AGP 8.7.3 needs 17)",
                   "PASS" if major >= 17 else "FAIL", f"found {major}")

    sdk = (os.environ.get("ANDROID_HOME")
           or os.environ.get("ANDROID_SDK_ROOT") or "")
    if not sdk:
        record("ANDROID_HOME or ANDROID_SDK_ROOT is set", "FAIL",
               "generate.py falls back to ~/Android/Sdk, which a service "
               "user does not have")
        return
    record("ANDROID_HOME or ANDROID_SDK_ROOT is set", "PASS", sdk)
    if not os.path.isdir(sdk):
        record("the SDK directory exists", "FAIL", sdk)
        return
    record("the SDK directory exists", "PASS")

    want = required_compile_sdk()
    if want is None:
        record("compileSdk could be read from the template", "FAIL",
               "cannot tell which platform the build needs, so the platform "
               "check below would be vacuous")
    else:
        plat = os.path.join(sdk, "platforms", f"android-{want}")
        record(f"platforms/android-{want} is installed",
               "PASS" if os.path.isdir(plat) else "FAIL", plat)

    bt = os.path.join(sdk, "build-tools")
    versions = sorted(os.listdir(bt)) if os.path.isdir(bt) else []
    record("build-tools is installed", "PASS" if versions else "FAIL",
           ", ".join(versions) or bt)

    gradlew = os.path.join(ROOT, "apps", "template-shift-journal", "gradlew")
    if not os.path.exists(gradlew):
        record("the Gradle wrapper is present", "FAIL", gradlew)
    else:
        record("the Gradle wrapper is present", "PASS")
        gmode = os.stat(gradlew).st_mode
        record("...and is executable by the service user",
               "FAIL" if owner_only_exec(gmode) else "PASS",
               f"mode {oct(gmode & 0o777)} - run_gradle execs it directly, "
               f"so without the bit it raises errno 13")


def main() -> int:
    print("Deployment self-check - read-only, changes nothing")
    print(f"repo: {ROOT}")
    print(f"env:  {load_env()}")
    check_python()
    check_pins()
    check_stealth_import()
    have_display = check_display()
    check_browser(have_display)
    check_database()
    check_app_imports()
    check_source_readable()
    check_build_toolchain()

    failed = [n for n, s, _ in RESULTS if s == "FAIL"]
    skipped = [n for n, s, _ in RESULTS if s == "SKIP"]
    print()
    print(f"RESULT: {len(RESULTS) - len(failed) - len(skipped)} passed, "
          f"{len(failed)} failed, {len(skipped)} skipped")
    if skipped:
        print(f"  skipped: {', '.join(skipped)}")
    if failed:
        print(f"  FAILED:  {', '.join(failed)}")
    return len(failed)


if __name__ == "__main__":
    sys.exit(main())
