#!/usr/bin/env python3
"""The contract the deploy artifacts must keep.

    python3 test_deploy_scripts.py

Hermetic: each script is RUN in a temporary directory with a stub
interpreter, so nothing touches a real .env and nothing reaches the
network. APP_DIR and PY are overridable in the scripts for exactly this.

Checking these by grepping their source is how a broken one shipped. The
source contained "rc=$?" and a check asserting that string passed, while
under `set -euo pipefail` the shell terminated at the failing command and
that line never ran - the operator got a bare traceback and no guidance.
A check on a string is not a check on a property.

The second bug the same day: the verification was piped in on stdin, and
load_dotenv() with no argument calls find_dotenv(), which walks back
through the CALLER's stack frames. A script read from stdin has no caller
frame, so it died on `assert frame.f_back is not None`. The stub
interpreter here records how it was invoked, so a return to stdin is
caught.

One file rather than one per script, because the contract is the same and
two copies of it would be free to drift - which is the defect this project
has already had twice, in the scorer and in the output path.
"""
from __future__ import annotations

import os
import shutil
import stat
import sys
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).parent
DEPLOY = REPO / "deploy"

# script -> (args it takes, the .env keys it must end up writing)
SCRIPTS = {
    "set_ibenty_env.sh": (["a@b.c", "pw"],
                          ["IBENTY_EMAIL=a@b.c", "IBENTY_PASSWORD=pw"]),
    "set_ch_key.sh": (["k3y-value"],
                      ["COMPANIES_HOUSE_API_KEY=k3y-value"]),
}

ENV_BEFORE = ("DATABASE_URL=postgresql://app:s3cret@db/company\n"
              "FLASK_SECRET_KEY=keep-me\n")


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


def run_script(script: str, tmp: Path, py_exit: int, args: list[str],
               env_body: str = ENV_BEFORE):
    """Run one deploy script with a stub interpreter that records its argv."""
    (tmp / ".env").write_text(env_body, encoding="utf-8")
    argv_log = tmp / "argv.txt"
    stub = tmp / "stubpy"
    stub.write_text(
        f'#!/bin/sh\nprintf "%s" "$*" > {argv_log}\nexit {py_exit}\n',
        encoding="utf-8")
    stub.chmod(0o755)
    proc = subprocess.run(
        ["bash", str(DEPLOY / script), *args],
        env={**os.environ, "APP_DIR": str(tmp), "PY": str(stub)},
        capture_output=True, text=True, timeout=120)
    argv = argv_log.read_text(encoding="utf-8").strip() if argv_log.exists() else ""
    return proc.returncode, proc.stdout + proc.stderr, argv


def check_script(r: Results, script: str) -> None:
    args, expected_keys = SCRIPTS[script]
    p = DEPLOY / script
    print(f"\n-- {script} --")
    if not r.check("exists", p.is_file()):
        return
    r.check("is executable", bool(p.stat().st_mode & stat.S_IXUSR),
            f"mode {oct(stat.S_IMODE(p.stat().st_mode))}")
    src = p.read_text(encoding="utf-8")
    head = src.split("say()")[0]
    r.check("no credential baked into it",
            "@" not in head.replace("#!/usr/bin/env bash", ""),
            "a credential in the repository is published when it is pushed")
    r.check("it refuses to run outside the app directory",
            'does not exist - is this the server?' in src)

    # ── the success path ────────────────────────────────────────────────
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        rc, out, argv = run_script(script, tmp, 0, args)
        r.check("the interpreter gets a real file, not stdin",
                argv.endswith(".py") and argv != "-",
                f"invoked as {argv!r}; '-' is stdin, which makes "
                f"load_dotenv() raise AssertionError")
        r.check("...inside APP_DIR, beside .env", argv.startswith(str(tmp)),
                f"invoked with {argv!r}")
        r.check("a successful verification exits 0", rc == 0, f"rc={rc}\n{out}")
        r.check("...leaving no temp verification file",
                not list(tmp.glob(".verify_*")),
                f"left: {[x.name for x in tmp.glob('.verify_*')]}")

        body = (tmp / ".env").read_text(encoding="utf-8")
        for kv in expected_keys:
            r.check(f"writes {kv.split('=')[0]}", kv in body, f"got {body!r}")
            r.check(f"...exactly once",
                    body.count("\n" + kv.split("=")[0] + "=")
                    + body.startswith(kv.split("=")[0] + "=") == 1,
                    f"python-dotenv keeps the LAST occurrence. got {body!r}")
        r.check("every other key survives",
                "DATABASE_URL=postgresql://app:s3cret@db/company" in body
                and "FLASK_SECRET_KEY=keep-me" in body, f"got {body!r}")
        r.check(".env is left at mode 600",
                stat.S_IMODE((tmp / ".env").stat().st_mode) == 0o600,
                f"mode {oct(stat.S_IMODE((tmp / '.env').stat().st_mode))}")
        r.check("the original is backed up",
                any(x.name.startswith(".env.bak.") for x in tmp.iterdir()),
                f"files: {[x.name for x in tmp.iterdir()]}")

    # ── the failure path ────────────────────────────────────────────────
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        rc, out, argv = run_script(script, tmp, 1, args)
        r.check("a failing verification exits non-zero", rc != 0, f"rc={rc}")
        r.check("...and reaches its own message, not a bare traceback",
                "verification failed" in out,
                f"`rc=$?` alone is unreachable under set -e. out={out!r}")
        r.check("...and points at the backup", ".env backup" in out,
                f"out={out!r}")
        r.check("...leaving no temp verification file",
                not list(tmp.glob(".verify_*")),
                f"left: {[x.name for x in tmp.glob('.verify_*')]}")

    # ── re-running must not duplicate a key ─────────────────────────────
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        run_script(script, tmp, 0, args)
        first = (tmp / ".env").read_text(encoding="utf-8")
        # Second run against the .env the first one produced.
        rc, out, argv = run_script(script, tmp, 0, args, env_body=first)
        body = (tmp / ".env").read_text(encoding="utf-8")
        for kv in expected_keys:
            name = kv.split("=")[0]
            n = sum(1 for line in body.splitlines()
                    if line.startswith(name + "="))
            r.check(f"re-running leaves exactly one {name}", n == 1,
                    f"{n} lines - a duplicate silently decides which value "
                    f"wins. got {body!r}")
        r.check("re-running still exits 0", rc == 0, f"rc={rc}\n{out}")


def check_prompt_safety(r: Results, script: str) -> None:
    """The prompt must never consume the next line of a pasted block.

    This happened. set_ch_key.sh was handed to an operator in the middle of
    a block of commands; a plain `read` takes the following pasted line as
    its input, so "install -m 644 deploy/Caddyfile /etc/caddy/Caddyfile"
    became the credential and never ran. The script must refuse instead.
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / ".env").write_text("DATABASE_URL=keep-me\n", encoding="utf-8")
        proc = subprocess.run(
            ["bash", str(DEPLOY / script)],            # no args: the prompt path
            env={**os.environ, "APP_DIR": str(tmp), "PY": sys.executable},
            input="install -m 644 deploy/Caddyfile /etc/caddy/Caddyfile\n",
            capture_output=True, text=True, timeout=60)
        out = proc.stdout + proc.stderr
        r.check(f"{script}: a non-terminal prompt refuses", proc.returncode == 2,
                f"rc={proc.returncode} - it must not read a pasted command as "
                f"a credential. out={out[-200:]!r}")
        r.check(f"{script}: ...saying it needs a terminal",
                "needs a terminal" in out, f"out={out[-200:]!r}")
        body = (tmp / ".env").read_text(encoding="utf-8")
        r.check(f"{script}: ...and writes nothing",
                "install -m 644" not in body and body == "DATABASE_URL=keep-me\n",
                f".env became {body!r}")
        # The guard must test that /dev/tty can be OPENED. `[[ -r /dev/tty ]]`
        # is TRUE even where opening fails - measured in a container with no
        # controlling terminal - so it is not a check at all.
        src = (DEPLOY / script).read_text(encoding="utf-8")
        r.check(f"{script}: the guard tests openability, not just existence",
                "(exec </dev/tty)" in src,
                "[[ -r /dev/tty ]] passes where the device cannot be opened")
        r.check(f"{script}: every prompt reads from the terminal",
                all("< /dev/tty" in ln
                    for ln in src.splitlines()
                    if ln.strip().startswith("read -r")),
                "a prompt left on stdin can still eat a pasted line")


def check_caddyfile(r: Results) -> None:
    print("\n-- deploy/Caddyfile security headers --")
    p = DEPLOY / "Caddyfile"
    if not r.check("the Caddyfile exists", p.is_file()):
        return
    src = p.read_text(encoding="utf-8")

    # DENY refuses framing by EVERYONE, including the site itself, and the
    # console previews a certificate PDF in a same-origin iframe
    # (static/app.html:2288 builds the root-relative /api/v2/dl?...&inline=1,
    # framed at :2293). Reproduced in a real browser: with DENY, Chromium
    # refused - "Refused to display ... because it set 'X-Frame-Options' to
    # 'deny'" - and with SAMEORIGIN it rendered.
    r.check("X-Frame-Options is SAMEORIGIN, not DENY",
            'X-Frame-Options "SAMEORIGIN"' in src,
            "DENY blocks the certificate preview, which frames this same "
            "origin")
    r.check("...and DENY is not still set anywhere",
            'X-Frame-Options "DENY"' not in src, "two values would conflict")
    r.check("...with the reason recorded, so it is not reverted blindly",
            "same-origin" in src.lower() and "preview" in src.lower(),
            "a future edit will put DENY back unless the file says why")

    # The protection the header exists for must survive: cross-origin
    # framing stays refused. A CSP frame-ancestors would override
    # X-Frame-Options, so a permissive one would silently undo this.
    r.check("no frame-ancestors directive overrides it",
            "frame-ancestors" not in src.lower(),
            "CSP frame-ancestors takes precedence over X-Frame-Options")
    for header in ("X-Content-Type-Options", "Strict-Transport-Security",
                   "Referrer-Policy"):
        r.check(f"{header} is still set", header in src,
                "the other security headers must not be lost in the edit")


def check_permission_predicates(r: Results) -> None:
    """deploy_check's permission guard, and the reason it reads mode bits.

    Observed on the server 2026-11-20: the delegate stage died with
    PermissionError on delegate.py from the dashboard while the identical
    import succeeded from the console. The console was root. `git pull` under
    a umask of 077 writes new files 0600, so every module a deploy ADDS is
    unreadable by the service user while every older one keeps working.

    The guard therefore must not be written with os.access(): run as root -
    which is how deploy_check is normally run - os.access() returns True for
    a 0600 file owned by anybody, so it would pass for exactly the fault it
    exists to find. That is checked here by parsing the module, because a
    guard that cannot fail where it runs is not a guard.
    """
    print("\n-- deploy_check's source-permission guard --")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import ast

    import deploy_check

    # 0600 and 0700 are the real fault: owner-only.
    for mode, expect, why in (
        (0o600, True,  "git pull under umask 077 produces exactly this"),
        (0o700, True,  "owner-only even though it is executable"),
        (0o640, False, "group-readable, which a service user can be given"),
        (0o644, False, "the normal mode for source"),
        (0o604, False, "other-readable without group"),
        (0o664, False, "group and other readable"),
    ):
        r.check(f"owner_only({oct(mode)}) is {expect}",
                deploy_check.owner_only(mode) is expect, why)

    for mode, expect in ((0o644, True), (0o604, True), (0o640, False),
                         (0o600, False), (0o660, False)):
        r.check(f"world_readable({oct(mode)}) is {expect}",
                deploy_check.world_readable(mode) is expect,
                "the .env half of the guard turns on this")

    # The exec counterpart, for the Gradle wrapper. Same trap, and it caught
    # the first version of the toolchain check, which used os.access(X_OK).
    for mode, expect, why in (
        (0o700, True,  "u+x only: root can exec it, the service user cannot"),
        (0o744, True,  "readable by all but executable only by the owner"),
        (0o755, False, "the normal mode for a wrapper script"),
        (0o750, False, "group-executable, which a service user can be given"),
        (0o711, False, "other-executable"),
        (0o644, True,  "not executable by anybody but reported as owner-only"),
    ):
        r.check(f"owner_only_exec({oct(mode)}) is {expect}",
                deploy_check.owner_only_exec(mode) is expect, why)

    # The guard must not be access-based. Parsed, not grepped: the docstring
    # above says the words "os.access" and a grep would trip on the prose.
    tree = ast.parse(Path(deploy_check.__file__).read_text(encoding="utf-8"))
    calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                calls.add(f.attr)
            elif isinstance(f, ast.Name):
                calls.add(f.id)
    r.check("the module parsed and calls were found", len(calls) > 10,
            f"only {sorted(calls)!r}; every check below would be vacuous")
    r.check("deploy_check never calls os.access", "access" not in calls,
            "as root os.access passes for a 0600 file owned by another user, "
            "so the guard would pass for the fault it exists to catch")
    r.check("...and does read st_mode", "stat" in calls,
            "the guard has no way to see the mode bits")

    # The walk must actually find this repository's own source.
    files = deploy_check._source_files()
    names = {os.path.basename(p) for p in files}
    for expected in ("deploy_check.py", "delegate.py", "cloudflare_dns.py",
                     "api_v2.py"):
        r.check(f"the walk finds {expected}", expected in names,
                f"{len(files)} files found; a module left out is a module "
                f"whose permissions are never checked")
    r.check("the walk skips .venv and .git",
            not any(os.sep + d + os.sep in p
                    for p in files for d in (".venv", ".git")),
            "the check would report thousands of dependency files")

    check_the_guard_fires(r, deploy_check)
    check_java_version_parsing(r, deploy_check)
    check_toolchain_guard_fires(r, deploy_check)
    check_android_installer(r)


def check_android_installer(r: Results) -> None:
    """install_android_sdk.sh appends to .env, which is the risky thing it does.

    A duplicate key is not cosmetic here: pipeline._load_env walks the file
    top-down with os.environ.setdefault, so the FIRST occurrence wins, while
    python-dotenv's load_dotenv lets the LAST one win. A second ANDROID_HOME
    therefore makes the console and the service disagree about its value, and
    nothing announces it. The .sh writers contract above does not cover this
    script - it never spells ENV_FILE - so the guard is checked here.
    """
    print("\n-- deploy/install_android_sdk.sh --")
    path = DEPLOY / "install_android_sdk.sh"
    r.check("the installer is checked in", path.exists(),
            "the fix has to be pasted by hand again, into a console that "
            "mangles long pastes - which is how it failed the first time")
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")

    r.check("it is syntactically valid bash",
            subprocess.run(["bash", "-n", str(path)],
                           capture_output=True).returncode == 0,
            "it would die part-way through, as root, on the server")

    appends = "ANDROID_HOME=%s\\n' \"$SDK_ROOT\" >> \"${APP_DIR}/.env\"" in text
    r.check("it appends ANDROID_HOME to .env", appends,
            "the console path would not see the SDK")
    r.check("...only after checking the key is absent",
            "grep -q '^ANDROID_HOME=' \"${APP_DIR}/.env\"" in text,
            "an unguarded append creates a duplicate, and the two loaders "
            "disagree about which copy wins")
    r.check("it never rewrites .env in place",
            "sed -i" not in text,
            "an in-place edit of .env can truncate it; every other deploy "
            "script writes a new file and moves it")
    r.check("it reads compileSdk from the template rather than hardcoding it",
            "compileSdk" in text and "android-35" not in text,
            "a hardcoded platform goes stale the moment the template is "
            "bumped, and the build then fails for a platform nobody checked")
    r.check("it verifies the download is a zip before unpacking",
            "unzip -tq" in text or "file \"$TMP_ZIP\"" in text,
            "a proxy error page arrives with a 200 and would read as a "
            "successful download")
    r.check("it makes the SDK readable by the service user",
            "chmod -R a+rX" in text,
            "root installs it, a non-root service reads it - the same fault "
            "that made delegate.py unreadable")
    r.check("it refuses to run as non-root",
            'id -u' in text,
            "it would fail half way with a confusing permission error")


def check_java_version_parsing(r: Results, deploy_check) -> None:
    """`java -version` has two schemes and the old one is a trap.

    'java version "1.8.0_392"' is Java 8. Reading the leading component gives
    1, which compares older than every floor and reports a working JDK as
    unusable - or, with the comparison written the other way, reports Java 8
    as new enough to run AGP 8.7.3, which needs 17.
    """
    print("\n-- java -version parsing --")
    for text, expect, why in (
        ('openjdk version "17.0.13" 2024-10-15', 17, "the version to install"),
        ('openjdk version "21.0.5" 2024-10-15', 21, "newer is fine"),
        ('java version "1.8.0_392"', 8, "the old scheme: 1.8 is 8, not 1"),
        ('java version "1.7.0_80"', 7, "older still, and must read as 7"),
        ('openjdk version "11.0.25"', 11, "below the floor but parseable"),
        ('openjdk version "25"', 25, "no minor component at all"),
        ("Picked up JAVA_TOOL_OPTIONS: -Xmx1g\n"
         'openjdk version "17.0.13"', 17, "a noisy first line is common"),
    ):
        r.check(f"java_major reads {expect} from {text.splitlines()[-1]!r}",
                deploy_check.java_major(text) == expect, why)

    for junk in ("", "command not found", "no version here", None):
        r.check(f"java_major({junk!r}) is None",
                deploy_check.java_major(junk) is None,
                "unparseable output must not become a number, which would be "
                "compared against the floor and silently pass or fail")

    # The floor comes from the template, not from this test.
    sdk = deploy_check.required_compile_sdk()
    r.check("compileSdk is read from the app template", sdk == 35,
            f"got {sdk!r}; a hardcoded platform check goes stale the moment "
            f"somebody bumps the template")


def check_toolchain_guard_fires(r: Results, deploy_check) -> None:
    """The toolchain section must FAIL on the real server state and PASS on a
    complete one. Without the second half it could be a section that always
    fails, which gets ignored."""
    print("\n-- the build-toolchain guard, both directions --")
    import io
    from contextlib import redirect_stdout

    def run(env):
        saved_results = deploy_check.RESULTS
        saved_env = {k: os.environ.get(k) for k in
                     ("ANDROID_HOME", "ANDROID_SDK_ROOT")}
        deploy_check.RESULTS = []
        for k in saved_env:
            os.environ.pop(k, None)
        os.environ.update({k: v for k, v in env.items() if v})
        try:
            with redirect_stdout(io.StringIO()):
                deploy_check.check_build_toolchain()
            return [f"{n}: {d}" for n, s, d in deploy_check.RESULTS
                    if s == "FAIL"]
        finally:
            deploy_check.RESULTS = saved_results
            for k in saved_env:
                os.environ.pop(k, None)
            for k, v in saved_env.items():
                if v is not None:
                    os.environ[k] = v

    # No ANDROID_HOME at all - the state of a fresh server.
    fails = run({})
    r.check("an unset ANDROID_HOME is reported FAILED",
            any("ANDROID_HOME" in f for f in fails),
            f"generate.py would silently use ~/Android/Sdk: {fails!r}")

    # Pointed at a directory that is not an SDK.
    tmp = Path(tempfile.mkdtemp())
    try:
        fails = run({"ANDROID_HOME": str(tmp)})
        r.check("a missing platforms/android-35 is reported FAILED",
                any("android-35" in f for f in fails), repr(fails))
        r.check("missing build-tools is reported FAILED",
                any("build-tools" in f for f in fails), repr(fails))

        # And the control: a complete-looking SDK clears both of those.
        sdk = deploy_check.required_compile_sdk()
        (tmp / "platforms" / f"android-{sdk}").mkdir(parents=True)
        (tmp / "build-tools" / "35.0.0").mkdir(parents=True)
        fails = run({"ANDROID_HOME": str(tmp)})
        r.check("a complete SDK clears the platform check",
                not any("android-" in f for f in fails), repr(fails))
        r.check("...and the build-tools check",
                not any("build-tools" in f for f in fails),
                "the guard fails on a correct SDK, so it would be ignored")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # keytool: whichever way this container is, the check must agree with
    # reality rather than being hardcoded.
    import shutil as _sh
    fails = run({"ANDROID_HOME": "/nonexistent-sdk-path"})
    has_keytool = _sh.which("keytool") is not None
    r.check("the keytool check matches whether keytool exists",
            any("keytool" in f for f in fails) is not has_keytool,
            f"keytool present={has_keytool}, failures={fails!r}")


def check_the_guard_fires(r: Results, deploy_check) -> None:
    """The predicates being right proves nothing about the guard being WIRED.

    So run check_source_readable over a directory built to contain the real
    fault, and then over the same directory without it. Both directions,
    because a guard that FAILs on everything would pass the first half.
    """
    import io
    from contextlib import redirect_stdout

    def run(mode: int, env_mode: int | None = None):
        tmp = Path(tempfile.mkdtemp())
        try:
            good = tmp / "fine.py"
            good.write_text("x = 1\n", encoding="utf-8")
            good.chmod(0o644)
            bad = tmp / "delegate.py"
            bad.write_text("y = 2\n", encoding="utf-8")
            bad.chmod(mode)
            if env_mode is not None:
                env = tmp / ".env"
                env.write_text("SECRET=hunter2\n", encoding="utf-8")
                env.chmod(env_mode)
            saved_root, saved_results = deploy_check.ROOT, deploy_check.RESULTS
            deploy_check.ROOT = str(tmp)
            deploy_check.RESULTS = []
            try:
                with redirect_stdout(io.StringIO()):
                    deploy_check.check_source_readable()
                return list(deploy_check.RESULTS)
            finally:
                deploy_check.ROOT = saved_root
                deploy_check.RESULTS = saved_results
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def failures(results):
        return [f"{n}: {d}" for n, s, d in results if s == "FAIL"]

    bad = failures(run(0o600))
    r.check("a 0600 source file is reported FAILED", len(bad) == 1,
            f"the real server fault went unreported: {bad!r}")
    r.check("...and the offending file is named",
            any("delegate.py" in f for f in bad),
            f"an operator cannot act on this: {bad!r}")

    # The control. Without it the check above passes for a guard that fails
    # on every directory it is ever given.
    r.check("the same directory at 0644 reports NO failure",
            failures(run(0o644)) == [],
            "the guard fails on correctly-permissioned source, so it would "
            "be switched off within a week")

    # And the other half: the careless fix for the fault above.
    r.check("a world-readable .env is reported FAILED",
            any(".env" in f for f in failures(run(0o644, env_mode=0o644))),
            "chmod -R a+r over the repo would expose the database password "
            "and every API key, and this check is what notices")
    r.check("a 0600 .env reports no failure",
            failures(run(0o644, env_mode=0o600)) == [],
            "the correct mode for .env is reported as a problem")


def main() -> int:
    r = Results()
    print(f"target: the .env-rewriting scripts in {DEPLOY}")

    # A script added later that rewrites .env but is not listed above would
    # go unchecked, and the contract exists precisely because getting it
    # wrong is silent.
    writers = sorted(p.name for p in DEPLOY.glob("*.sh")
                     if 'ENV_FILE="$APP_DIR/.env"' in p.read_text(encoding="utf-8"))
    r.check("every .env-rewriting script in deploy/ is covered here",
            writers == sorted(SCRIPTS),
            f"found {writers}, covered {sorted(SCRIPTS)}")

    for script in sorted(SCRIPTS):
        check_script(r, script)
        check_prompt_safety(r, script)
    check_caddyfile(r)
    check_permission_predicates(r)
    return r.report()


if __name__ == "__main__":
    raise SystemExit(main())
