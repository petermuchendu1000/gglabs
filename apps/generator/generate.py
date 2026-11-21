"""End-to-end Android artifact factory.

    python -m apps.generator.generate --count 3

Steps:
  1. Load N companies from pipeline_output/companies_pipeline.xlsx
  2. For each: synth palette, mint keystore, write per-flavor res overlay
  3. Write flavors.gradle.kts
  4. Ensure local.properties points at the Android SDK
  5. Run `gradlew assemble` or `bundle` tasks
  6. Copy signed APK/AAB artifacts to pipeline_output/apps/{company_number}/
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Iterable
from pathlib import Path

from .brand_synth import palette_for
from .catalog import Company, load_companies, summarize
from .flavor_writer import BuiltFlavor, clean_stale_flavors, write_flavor_resources, write_flavors_gradle, _sic_role_vocab
from .keystore_mint import mint as mint_keystore
from . import assets


def _get_vocab(company: Company) -> dict[str, str]:
    return _sic_role_vocab(company)

# Project layout
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATE_ROOT = os.path.join(ROOT, "apps", "template-shift-journal")
APP_DIR = os.path.join(TEMPLATE_ROOT, "app")
KEYSTORES_DIR = os.path.join(APP_DIR, "keystores")
COMPANIES_DIR = os.path.join(ROOT, "pipeline_output", "companies")

SDK_DIR_DEFAULT = os.environ.get(
    "ANDROID_HOME",
    os.path.expanduser("~/Android/Sdk"),
)
DEFAULT_ARTIFACT = "aab"


def _ensure_local_properties(sdk_dir: str = SDK_DIR_DEFAULT) -> None:
    path = os.path.join(TEMPLATE_ROOT, "local.properties")
    escaped = sdk_dir.replace("\\", "\\\\").replace(":", "\\:")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"sdk.dir={escaped}\n")


def build_flavors(companies: Iterable[Company]) -> list[BuiltFlavor]:
    companies = list(companies)
    built: list[BuiltFlavor] = []
    for c in companies:
        palette = palette_for(c.company_name)
        write_flavor_resources(c, palette, APP_DIR)
        ks = mint_keystore(c.company_number, c.company_name, KEYSTORES_DIR)
        built.append(BuiltFlavor(company=c, palette=palette, keystore=ks, flavor_dir=""))
    write_flavors_gradle(built, APP_DIR)
    clean_stale_flavors(APP_DIR, keep_flavors={b.company.flavor for b in built})
    return built


def run_gradle(built: list[BuiltFlavor], variant: str = "Release", artifact: str = DEFAULT_ARTIFACT) -> int:
    """Invoke the Gradle wrapper to assemble or bundle all flavors in one go."""
    # gradlew.bat is a Windows batch file. On Linux it still EXISTS, so the
    # existence guard below passed and subprocess.run then raised - errno 13
    # without the exec bit, errno 8 (Exec format error) with it - so the
    # pipeline died on an uncaught exception instead of returning 1.
    # posix is the only new branch: every other os.name keeps the previous
    # path exactly, so Windows cannot regress.
    wrapper = "gradlew" if os.name == "posix" else "gradlew.bat"
    gradlew = os.path.join(TEMPLATE_ROOT, wrapper)
    if not os.path.exists(gradlew):
        print(f"  ERROR: {gradlew} not found")
        return 1

    tasks: list[str] = []
    for b in built:
        suffix = b.company.flavor.capitalize() + variant
        if artifact in {"apk", "both"}:
            tasks.append(f":app:assemble{suffix}")
        if artifact in {"aab", "both"}:
            tasks.append(f":app:bundle{suffix}")

    if not tasks:
        print("  ERROR: no build artifact selected")
        return 1

    cmd = [gradlew, "--no-daemon", "--console=plain", "--stacktrace", *tasks]
    print(f"\n  Gradle: {' '.join(tasks)}")

    # A JAVA_HOME with no JDK behind it is dropped for the child.
    #
    # Gradle PREFERS JAVA_HOME over PATH, so one stale line in .env - on this
    # server, a developer's C:\Program Files\Android\Android Studio\jbr -
    # kills every build before it compiles anything, while `java -version`
    # keeps answering and looks fine. It cost two diagnosis cycles.
    #
    # This is narrow on purpose. There is no reading under which a JAVA_HOME
    # containing no bin/java is intentional: it cannot work, for anyone,
    # ever. A VALID JAVA_HOME is passed through untouched, because choosing
    # which JDK to build with is the operator's business whenever the choice
    # can actually run. And the drop is announced, with where to fix it,
    # rather than quietly papering over a broken deployment.
    env = os.environ.copy()
    java_home = env.get("JAVA_HOME", "").strip()
    if java_home and not any(
            os.path.exists(os.path.join(java_home, "bin", exe))
            for exe in ("java", "java.exe")):
        print(f"    JAVA_HOME={java_home!r} contains no bin/java, so Gradle "
              f"would refuse to start.")
        print("    Dropping it for this build; gradlew will use the java on "
              "PATH instead.")
        print("    Fix it at source - it is almost certainly a stale line in "
              ".env or the unit.")
        env.pop("JAVA_HOME", None)

    t0 = time.time()

    # Gradle's output is RELAYED through print(), not inherited.
    #
    # subprocess.run(cmd) with no pipes hands the child the real file
    # descriptors. api_v2 captures a stage's output by replacing sys.stdout,
    # which is a Python-level object - fd 1 is untouched - so every line
    # Gradle wrote went to the service's journal and the job drawer showed
    # only "Gradle exit=1". That is how a build that died in 0.0s, before it
    # compiled anything, arrived as a failure with no reason attached: the
    # reason had been printed, to somewhere nobody was reading.
    #
    # Line by line rather than capture-then-print, so a ten-minute build
    # still shows progress instead of going silent until it ends.
    lines: list[str] = []
    try:
        proc = subprocess.Popen(cmd, cwd=TEMPLATE_ROOT, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                bufsize=1, env=env)
    except OSError as e:
        # errno 13 without the exec bit, errno 8 for a bad format. Both used
        # to escape as an uncaught exception from inside a worker thread.
        print(f"  ERROR: could not start {gradlew}: {e}")
        return 1
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        lines.append(line)
        print(f"    | {line}")
    code = proc.wait()
    dt = time.time() - t0
    print(f"  Gradle exit={code} in {dt:.1f}s")

    if code != 0 and not lines:
        # A non-zero exit with NOTHING on either stream. The JVM does this
        # when it cannot reserve its heap, and it is over in milliseconds -
        # which looks identical to "the wrapper never ran". Say which.
        print("  Gradle produced no output at all before exiting "
              f"{code} in {dt:.1f}s.")
        print("  That is not a compile error: nothing was compiled. A JVM "
              "that cannot reserve its heap exits like this.")
        print(f"  Reproduce it directly with:  cd {TEMPLATE_ROOT} && "
              f"./gradlew --no-daemon --version")
    return code


def _co_dir(c) -> str:
    """Return pipeline_output/companies/{cn} - {safe_name}/app for a Company."""
    safe = re.sub(r'[\\/:*?"<>|]', "", c.company_name).strip()
    folder = f"{c.company_number} - {safe}" if safe else c.company_number
    return os.path.join(COMPANIES_DIR, folder, "app")


def collect_artifacts(built: list[BuiltFlavor], variant: str = "release", artifacts: str = DEFAULT_ARTIFACT) -> dict[str, dict[str, str]]:
    """Copy built APK/AAB files to pipeline_output/companies/{cn} - {name}/app/artifacts/."""
    out: dict[str, dict[str, str]] = {}
    for b in built:
        c = b.company
        record: dict[str, str] = {}
        dst_dir = os.path.join(_co_dir(c), "artifacts")
        os.makedirs(dst_dir, exist_ok=True)

        apk_fname = f"app-{c.flavor}-{variant}.apk"
        aab_fname = f"app-{c.flavor}-{variant}.aab"

        if artifacts in {"apk", "both"}:
            apk_src = os.path.join(APP_DIR, "build", "outputs", "apk", c.flavor, variant, apk_fname)
            if os.path.exists(apk_src):
                apk_dst = os.path.join(dst_dir, apk_fname)
                shutil.copy2(apk_src, apk_dst)
                record["apk"] = apk_dst
                size_kb = os.path.getsize(apk_dst) // 1024
                print(f"  [{c.company_number}] artifacts/{apk_fname}  ({size_kb} KB)")
            else:
                print(f"  [{c.company_number}] MISSING {apk_src}")

        if artifacts in {"aab", "both"}:
            aab_src = os.path.join(
                APP_DIR, "build", "outputs", "bundle", f"{c.flavor}{variant.capitalize()}", aab_fname,
            )
            if os.path.exists(aab_src):
                aab_dst = os.path.join(dst_dir, aab_fname)
                shutil.copy2(aab_src, aab_dst)
                record["aab"] = aab_dst
                size_kb = os.path.getsize(aab_dst) // 1024
                print(f"  [{c.company_number}] artifacts/{aab_fname}  ({size_kb} KB)")
            else:
                print(f"  [{c.company_number}] MISSING {aab_src}")

        if record:
            out[c.company_number] = record
    return out


def backup_and_make_assets(built: list[BuiltFlavor], collected: dict[str, dict[str, str]]) -> None:
    """For each built flavor, copy keystore, generate assets, and write manifest."""
    for b in built:
        c = b.company
        out_dir = _co_dir(c)
        artifact_record = collected.get(c.company_number, {})
        apk_path = artifact_record.get("apk")
        aab_path = artifact_record.get("aab")
        # backup keystore into output
        ks_src = os.path.join(APP_DIR, "keystores", f"{c.company_number}.jks")
        if os.path.exists(ks_src):
            assets.backup_keystore(Path(out_dir), Path(ks_src), mint_keystore(c.company_number, c.company_name, KEYSTORES_DIR).store_password, "upload")
        # generate assets (use palette to pick primary color)
        palette = palette_for(c.company_name)
        vocab = _get_vocab(c)
        assets.generate_all({
            "display_name": c.display_name,
            "company_name": c.company_name,
            "company_number": c.company_number,
            "support_email": c.support_email,
            "application_id": c.application_id,
            "domain": c.domain,
            # Needed by the privacy publisher, which prints the registered
            # address on the policy page. Without it a pipeline run would
            # overwrite a published policy with one missing that line.
            "address": c.address,
            "role_noun": vocab["ROLE_NOUN"],
            "role_verb_start": vocab["ROLE_VERB_START"],
            "role_verb_end": vocab["ROLE_VERB_END"],
            "export_title": vocab["EXPORT_TITLE"],
        }, Path(apk_path) if apk_path else None, Path(aab_path) if aab_path else None, Path(out_dir), palette.primary)


def generate_from_pipeline_results(
    results: list[dict],
    variant: str = "Release",
    sdk_dir: str = SDK_DIR_DEFAULT,
    artifact: str = DEFAULT_ARTIFACT,
    skip_build: bool = False,
) -> dict[str, dict]:
    """
    Generate APKs from pipeline result dicts (used for integrated pipeline).
    
    Args:
        results: List of pipeline result dicts with company_number, details, domain, email, etc.
        variant: "Release" or "Debug"
        sdk_dir: Path to Android SDK
        skip_build: If True, only generate flavors without running Gradle
    
    Returns:
        Dict mapping company_number -> {status, apk_path, aab_path, error, message}
        Possible statuses: "success", "failed"
    """
    from .catalog import Company, SIC_ARCHETYPE, DEFAULT_ARCHETYPE
    
    output = {}
    
    # Convert pipeline results to Company objects
    companies = []
    for r in results:
        details = r.get("details", {})
        domain = r.get("domain", {}).get("domain", "")
        email = r.get("email", {}).get("email", "")
        
        if not details.get("company_number"):
            continue
            
        cn = details.get("company_number", "")
        sic_raw = details.get("sic_codes", "")
        primary_sic = sic_raw.split(",")[0].strip() if sic_raw else ""
        archetype = SIC_ARCHETYPE.get(primary_sic, DEFAULT_ARCHETYPE)
        
        support = f"dev@{domain}" if domain else (email or "support@example.uk")
        
        c = Company(
            company_number=cn,
            company_name=details.get("company_name", ""),
            sic_codes=sic_raw,
            domain=domain,
            support_email=support,
            archetype=archetype,
            short_name="",
            address=details.get("address", ""),
        )
        companies.append(c)
        output[cn] = {"status": "pending", "apk_path": "", "aab_path": "", "error": "", "message": ""}
    
    if not companies:
        return output
    
    try:
        print(f"\n[APK Step 1] Writing brand overlays + minting keystores ({len(companies)} companies)…")
        built = build_flavors(companies)
        print(f"  Wrote {len(built)} flavor overlays + flavors.gradle.kts + keystores")
        
        _ensure_local_properties(sdk_dir)
        print(f"  local.properties -> sdk.dir={sdk_dir}")
        
        if skip_build:
            print("\n[APK Step 2] SKIPPED Gradle build.")
            backup_and_make_assets(built, {})
            for c in companies:
                output[c.company_number]["status"] = "skipped"
                output[c.company_number]["message"] = "Gradle build skipped"
            return output
        
        print("\n[APK Step 2] Running Gradle…")
        rc = run_gradle(built, variant=variant, artifact=artifact)
        if rc != 0:
            for c in companies:
                output[c.company_number]["status"] = "failed"
                output[c.company_number]["error"] = f"Gradle build failed (exit code {rc})"
            return output
        
        artifact_label = "AABs" if artifact == "aab" else "APKs" if artifact == "apk" else "APKs and AABs"
        print(f"\n[APK Step 3] Collecting {artifact_label}…")
        got = collect_artifacts(built, variant=variant.lower(), artifacts=artifact)
        
        print("\n[APK Step 4] Backing up keystores and generating install assets…")
        backup_and_make_assets(built, got)
        
        # Update output with results
        for c in companies:
            if c.company_number in got:
                output[c.company_number]["status"] = "success"
                output[c.company_number]["apk_path"] = got[c.company_number].get("apk", "")
                output[c.company_number]["aab_path"] = got[c.company_number].get("aab", "")
                # Single quotes inside these replacement fields, not double:
                # nesting the enclosing quote is PEP 701 (Python 3.12+), and
                # Debian 12 ships 3.11, where it is a SyntaxError at import.
                if artifact == "aab":
                    output[c.company_number]["message"] = f"AAB built: {Path(output[c.company_number]['aab_path']).name if output[c.company_number]['aab_path'] else 'missing'}"
                elif artifact == "both":
                    output[c.company_number]["message"] = f"APK/AAB built: {Path(output[c.company_number]['apk_path']).name if output[c.company_number]['apk_path'] else 'missing'}, {Path(output[c.company_number]['aab_path']).name if output[c.company_number]['aab_path'] else 'missing'}"
                else:
                    output[c.company_number]["message"] = f"APK built: {Path(output[c.company_number]['apk_path']).name if output[c.company_number]['apk_path'] else 'missing'}"
            else:
                output[c.company_number]["status"] = "failed"
                output[c.company_number]["error"] = f"{artifact_label} not found after build"
        
        return output
        
    except Exception as e:
        for c in companies:
            output[c.company_number]["status"] = "failed"
            output[c.company_number]["error"] = f"Exception: {str(e)}"
        return output


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Android APK factory")
    p.add_argument("--count", type=int, default=3, help="Number of companies to build")
    p.add_argument("--company", nargs="+", metavar="CN", help="Build only these company number(s), e.g. --company 02591663")
    p.add_argument("--variant", default="Release", choices=["Release", "Debug"])
    p.add_argument("--artifact", default=DEFAULT_ARTIFACT, choices=["apk", "aab", "both"], help="Artifact type to build")
    p.add_argument("--sdk", default=SDK_DIR_DEFAULT)
    p.add_argument("--skip-build", action="store_true", help="Generate flavors but don't run Gradle")
    args = p.parse_args(argv)

    companies = load_companies(
        limit=None if args.company else args.count,
        company_numbers=args.company,
    )
    if not companies:
        print("No companies loaded. Bail.")
        return 2
    print("=" * 72)
    print(f"APK FACTORY — {len(companies)} companies, variant={args.variant}")
    print("=" * 72)
    print(summarize(companies))

    print("\n[1] Writing brand overlays + minting keystores…")
    built = build_flavors(companies)
    print(f"  wrote {len(built)} flavor overlays + flavors.gradle.kts + keystores")

    _ensure_local_properties(args.sdk)
    print(f"  local.properties -> sdk.dir={args.sdk}")

    if args.skip_build:
        print("\n[SKIPPED] Gradle build. Collecting any existing artifacts and generating assets…")
        got = collect_artifacts(built, variant=args.variant.lower(), artifacts=args.artifact)
        backup_and_make_assets(built, got)
        return 0

    print("\n[2] Running Gradle…")
    rc = run_gradle(built, variant=args.variant, artifact=args.artifact)
    if rc != 0:
        print("\nBuild failed. See Gradle output above.")
        return rc

    artifact_label = "AABs" if args.artifact == "aab" else "APKs" if args.artifact == "apk" else "APKs and AABs"
    print(f"\n[3] Collecting {artifact_label}…")
    got = collect_artifacts(built, variant=args.variant.lower(), artifacts=args.artifact)
    if len(got) != len(built):
        print(f"\n  WARN: only {len(got)}/{len(built)} {artifact_label} collected.")
    else:
        # COMPANIES_DIR, not OUTPUT_DIR: the latter is defined nowhere in this
        # module, so only this total-success branch raised NameError, skipping
        # backup_and_make_assets below and leaving no assets or manifest.json.
        print(f"\n  OK: all {len(got)} {artifact_label} under {COMPANIES_DIR}")

    print("\n[4] Backing up keystores and generating install assets…")
    backup_and_make_assets(built, got)

    return 0 if len(got) == len(built) else 1


if __name__ == "__main__":
    sys.exit(main())
