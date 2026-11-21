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


def looks_like_windows_path(value: str) -> bool:
    """A drive letter or a backslash separator: somebody's .env travelled.

    Worth its own predicate because the resulting failure names the variable
    but not the reason, and "invalid directory" invites somebody to create
    the directory rather than to delete the line.
    """
    v = (value or "").strip()
    if not v:
        return False
    return bool(re.match(r"^[A-Za-z]:[\\/]", v)) or "\\" in v


def required_build_tools() -> str | None:
    """buildToolsVersion from the app template, or None if it is not pinned.

    Checking merely that SOME build-tools directory exists is what let this
    through: 35.0.0 was installed, the check passed, and AGP asked for
    34.0.0 - its own built-in default, unrelated to compileSdk - then tried
    to download it into a read-only SDK and failed. The version the build
    will actually use is the one written in the template, so that is the one
    to look for. Unpinned is itself a finding.
    """
    text = _template_gradle()
    if text is None:
        return None
    m = re.search(r'buildToolsVersion\s*=\s*"([^"]+)"', text)
    return m.group(1) if m else None


def _template_gradle() -> str | None:
    """The app template's build file, preferring the generated one.

    build.gradle.kts is regenerated from build.gradle.kts.template on every
    build, so on a fresh checkout only the template exists.
    """
    base = os.path.join(ROOT, "apps", "template-shift-journal", "app")
    for name in ("build.gradle.kts", "build.gradle.kts.template"):
        try:
            return open(os.path.join(base, name), encoding="utf-8").read()
        except OSError:
            continue
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

    # JAVA_HOME FIRST, because Gradle prefers it over PATH. A java on PATH
    # therefore proves nothing on its own: observed on the server, a .env
    # carrying a developer's Windows JAVA_HOME
    # (C:\Program Files\Android\Android Studio\jbr) made every build die with
    # "JAVA_HOME is set to an invalid directory" while `java -version`
    # answered perfectly and this section passed.
    java_home = os.environ.get("JAVA_HOME", "").strip()
    if not java_home:
        # Unset is CORRECT here: gradlew then uses the java on PATH.
        record("JAVA_HOME is unset, so Gradle uses the java on PATH", "PASS")
    else:
        if looks_like_windows_path(java_home):
            record("JAVA_HOME is a usable directory", "FAIL",
                   f"{java_home!r} is a Windows path on a Linux host - a "
                   f"developer's .env was copied here. Gradle prefers "
                   f"JAVA_HOME over PATH, so this breaks every build while "
                   f"java -version keeps working")
        elif not os.path.isdir(java_home):
            record("JAVA_HOME is a usable directory", "FAIL",
                   f"{java_home!r} does not exist; Gradle prefers JAVA_HOME "
                   f"over PATH and refuses to start")
        elif not os.path.exists(os.path.join(java_home, "bin", "java")):
            record("JAVA_HOME is a usable directory", "FAIL",
                   f"{java_home}/bin/java is missing, so it is not a JDK")
        else:
            record("JAVA_HOME is a usable directory", "PASS", java_home)

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
    want_bt = required_build_tools()
    if not want_bt:
        # Unpinned is the defect, not a detail. AGP then picks its own
        # default and downloads it at build time, which fails on a read-only
        # SDK with a message about the directory not being writable.
        record("buildToolsVersion is pinned in the template", "FAIL",
               "unpinned, so AGP will choose its own default (34.0.0 for "
               "8.7.3) and try to download it into the SDK at build time; "
               f"installed here: {', '.join(versions) or 'none'}")
    else:
        record(f"build-tools {want_bt} (the pinned version) is installed",
               "PASS" if want_bt in versions else "FAIL",
               "" if want_bt in versions else
               f"installed: {', '.join(versions) or 'none'} - the build "
               f"would try to download {want_bt} into the SDK")

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


# Which stage stops working, and how, when a variable is missing. Written as
# a table because the failures do not look alike: some raise, some return an
# error, and CLOUDFLARE_EMAIL_WORKER quietly degrades to a route that cannot
# deliver. Each entry is (stage, names, what breaks). A tuple inside `names`
# means "any one of these".
STAGE_ENV = (
    ("details, certificate", ("COMPANIES_HOUSE_API_KEY",),
     "Companies House returns 401 and no company data or certificate arrives"),
    ("domain", ("NAMECHEAP_API_KEY", "NAMECHEAP_API_USER",
                "NAMECHEAP_USERNAME", "NAMECHEAP_CLIENT_IP"),
     "no domain can be priced or bought; NAMECHEAP_CLIENT_IP must be the "
     "address Namecheap has allowlisted for this account"),
    ("delegate", ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"),
     "no zone can be created, so no domain can be delegated"),
    ("email", ("CLOUDFLARE_EMAIL_WORKER",),
     "dev@ falls back to FORWARDING to the company's own mailbox, which "
     "Cloudflare refuses to deliver to until a human opens it and clicks a "
     "link - one manual step per company, and the stage reports "
     "pending_verification every time"),
    ("director_id", (("IBENTY_EMAIL", "XBINDER_EMAIL"),
                     ("IBENTY_PASSWORD", "XBINDER_PASSWORD")),
     "the stage returns without doing anything"),
)


def check_pipeline_env() -> None:
    """The variables each STAGE needs, checked before a stage needs them.

    Three deploys in a row were diagnosed by running the pipeline and reading
    a traceback: ANDROID_HOME, then GRADLE_USER_HOME, then
    CLOUDFLARE_EMAIL_WORKER. The last is the worst of the three because
    nothing raises - the email stage silently falls back to a forward that
    Cloudflare will not deliver, and the failure arrives as
    "pending_verification", which reads like somebody forgot to click a link
    rather than like a missing setting.
    """
    section("10. Pipeline environment (per stage)")
    for stage, names, breaks in STAGE_ENV:
        missing = []
        for name in names:
            alts = (name,) if isinstance(name, str) else name
            if not any(os.environ.get(a, "").strip() for a in alts):
                missing.append(" or ".join(alts))
        record(f"{stage}: environment is set",
               "FAIL" if missing else "PASS",
               f"missing {', '.join(missing)} - {breaks}" if missing else "")

    # CLOUDFLARE_EMAIL_WORKER naming a script that does not exist is worse
    # than leaving it unset: the routing rule is created, reads "configured",
    # and delivers to nothing. A typo is invisible without asking Cloudflare.
    worker = os.environ.get("CLOUDFLARE_EMAIL_WORKER", "").strip()
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    if not worker:
        record("the named Email Worker exists", "SKIP",
               "CLOUDFLARE_EMAIL_WORKER is not set")
        return
    if not (token and account):
        record("the named Email Worker exists", "SKIP",
               "no Cloudflare credential to ask with")
        return
    try:
        import urllib.error
        import urllib.request
        req = urllib.request.Request(
            f"https://api.cloudflare.com/client/v4/accounts/{account}"
            f"/workers/scripts/{worker}",
            headers={"Authorization": f"Bearer {token}"})
        # The body is NOT parsed. This endpoint returns the script's own
        # JAVASCRIPT SOURCE, not JSON, so an earlier version of this check
        # called json.load on it, threw, and reported SKIP - "could not ask"
        # - for a worker that plainly exists. A 200 is the whole answer.
        with urllib.request.urlopen(req, timeout=30) as f:
            status = getattr(f, "status", None) or f.getcode()
        record(f"the Email Worker {worker!r} exists",
               "PASS" if status == 200 else "SKIP",
               "" if status == 200 else f"Cloudflare answered {status}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            record(f"the Email Worker {worker!r} exists", "FAIL",
                   "Cloudflare has no such script, so every dev@ rule this "
                   "pipeline writes points at nothing and mail is lost "
                   "silently")
        else:
            # Could not ASK. Must not read as "the worker is fine".
            record(f"the Email Worker {worker!r} exists", "SKIP",
                   f"Cloudflare answered {e.code}; this check could not be "
                   f"made, which is not the same as passing")
    except Exception as e:                      # pragma: no cover - network
        record(f"the Email Worker {worker!r} exists", "SKIP",
               f"could not reach Cloudflare: {e}")


def service_user() -> str:
    """The user the unit runs as, read from the unit rather than assumed."""
    unit = os.path.join(ROOT, "deploy", "company-registration.service")
    try:
        text = open(unit, encoding="utf-8").read()
    except OSError:
        return ""
    m = re.search(r"^User=(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def writable_by(st, uid: int, gids) -> bool:
    """Could the user (uid, gids) write here? Decided from mode and ownership.

    NOT os.access(W_OK). deploy_check normally runs as root, and root may
    write almost anywhere regardless of mode, so an access-based answer is
    True for exactly the directories the service cannot write. Same trap as
    owner_only, and the reason that one exists.
    """
    if st.st_uid == uid:
        return bool(st.st_mode & stat.S_IWUSR)
    if st.st_gid in gids:
        return bool(st.st_mode & stat.S_IWGRP)
    return bool(st.st_mode & stat.S_IWOTH)


# Directories the app stage writes into, and what it writes. Taken from the
# code, not from memory: flavor_writer rmtree/recreates app/src/<flavor> and
# rewrites app/build.gradle.kts, generate writes local.properties and copies
# artifacts, keystore_mint creates keystores/ and .master_secret.
WRITABLE_PATHS = (
    ("pipeline_output", "stage output and app artifacts"),
    ("apps/generator", ".master_secret, minted once and needed for every "
                       "later signing"),
    ("apps/generator/keystores", "the per-company upload keystore"),
    ("apps/template-shift-journal", "local.properties and Gradle's project "
                                    "cache"),
    ("apps/template-shift-journal/app", "the generated build.gradle.kts and "
                                        "build/ outputs"),
    ("apps/template-shift-journal/app/src", "the per-company flavour overlay, "
                                            "DELETED and recreated every run"),
)


def check_service_can_write() -> None:
    """Readable is not writable, and the app stage writes a great deal.

    Section 8 checks that the service can READ the source. That was the fault
    it was written for and it does not cover this one: the app stage deletes
    and recreates app/src/<flavor> on every run, so it needs WRITE access to
    directories that a root-run `git pull` and a root-run `chmod 755` leave
    owned by root. The failure surfaces as

      PermissionError: [Errno 13] Permission denied: 'colors.xml'

    from inside shutil.rmtree - a file name with no path and no hint that the
    real subject is the containing directory's owner.

    Running the same stage from a root console does not reproduce it, which
    is what makes it expensive: root writes where the service cannot.
    """
    section("11. Directories the service must be able to write")
    user = service_user()
    if not user:
        record("the service user is named in the unit", "FAIL",
               "deploy/company-registration.service has no User= line, so "
               "this check cannot name whose permissions matter")
        return
    try:
        import grp
        import pwd
        pw = pwd.getpwnam(user)
        gids = set(os.getgrouplist(user, pw.pw_gid))
    except Exception as e:
        # Could not ASK. Not the same as "the permissions are fine".
        record(f"the service user {user!r} exists", "SKIP",
               f"{e}; this check needs the account to resolve and cannot be "
               f"made without it")
        return
    record(f"the service user {user!r} resolves", "PASS",
           f"uid={pw.pw_uid} gids={sorted(gids)}")

    for rel, what in WRITABLE_PATHS:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            # Absent is fine only if its PARENT can be written, since the
            # stage creates it. Checking the parent is the real question.
            parent = os.path.dirname(path)
            try:
                st = os.stat(parent)
            except OSError as e:
                record(f"{rel} can be created", "FAIL", f"{parent}: {e}")
                continue
            ok = writable_by(st, pw.pw_uid, gids)
            record(f"{rel} can be created by {user}", "PASS" if ok else "FAIL",
                   "" if ok else
                   f"{parent} is {_owner(st)} mode "
                   f"{oct(st.st_mode & 0o777)} - {what} cannot be created")
            continue
        st = os.stat(path)
        ok = writable_by(st, pw.pw_uid, gids)
        record(f"{rel} is writable by {user}", "PASS" if ok else "FAIL",
               "" if ok else
               f"{_owner(st)} mode {oct(st.st_mode & 0o777)} - {what}")


def _owner(st) -> str:
    try:
        import grp
        import pwd
        return (f"owned by {pwd.getpwuid(st.st_uid).pw_name}:"
                f"{grp.getgrgid(st.st_gid).gr_name}")
    except Exception:
        return f"owned by uid={st.st_uid} gid={st.st_gid}"


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
    check_pipeline_env()
    check_service_can_write()

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
