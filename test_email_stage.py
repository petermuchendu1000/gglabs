#!/usr/bin/env python3
"""Tests for stage 3, dev@ email routing (pipeline.stage_email).

    python3 test_email_stage.py

Hermetic: no network, no database, no Cloudflare account, and no writes
inside the repository - BASE_DIR is redirected to a temporary directory for
the manifest checks. The double replaces cloudflare_dns.ensure_dev_route,
and pipeline.db is a module-scope import so it is replaceable. No production
code was changed to make this testable.

This stage mutates live mail routing for a real domain. It does not spend
money, but a wrong write silently redirects or destroys a customer's mail,
so the check that matters most is still that dry_run issues no write.

WHAT CHANGED, AND WHY TWO CHECKS WERE REMOVED
---------------------------------------------
This stage used to drive Namecheap directly with ~80 lines of inline XML.
Namecheap's setEmailForwarding REPLACES the whole forwarding set, so the
code had to read every existing forward back and re-submit it, and anything
missing from that read was DELETED. Two checks existed solely to guard that
hazard:

    check_preserves_existing       an existing forward is never dropped
    check_malformed_forward_refused  a set with a blank mailbox or target is
                                   refused rather than rewritten

Cloudflare creates routing rules INDIVIDUALLY. There is no set to read, no
merge, and ensure_dev_route only ever matches a rule whose matcher is
exactly dev@<this domain>, so it cannot remove or rewrite a rule it did not
create. The operation those two checks guarded no longer exists, so the
checks were removed rather than rewritten into something vacuous.

They are replaced by check_the_merge_hazard_is_gone, which asserts the
absence directly: the stage makes exactly ONE provider call per company and
its source contains no read-modify-write of a forwarding set.

Namecheap is NOT gone from the pipeline. It remains the registrar - it
checks availability, buys the domain and sets its nameservers. It no longer
touches records or mail.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

# Read with bracket access at pipeline.py:190-193, so a missing one raises.
os.environ.setdefault("NAMECHEAP_API_USER", "test-user")
os.environ.setdefault("NAMECHEAP_API_KEY", "test-key")
os.environ.setdefault("NAMECHEAP_USERNAME", "test-user")
os.environ.setdefault("NAMECHEAP_CLIENT_IP", "127.0.0.1")
os.environ.setdefault("DATABASE_URL", "postgresql://unused@127.0.0.1:1/none")
os.environ.setdefault("LOG_LEVEL", "CRITICAL")

import cloudflare_dns
import pipeline


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


def route(status="configured", created=True, target="owner@real.example",
          **extra):
    """Build one ensure_dev_route return value."""
    out = {"status": status, "target": target}
    if status == "configured":
        out["created"] = created
    out.update(extra)
    return out


class _Routes:
    """Records every ensure_dev_route call and answers from a script."""

    def __init__(self, result=None, per_domain=None):
        self.result = result
        self.per_domain = per_domain or {}
        self.calls: list[dict] = []

    def __call__(self, domain, worker=None, forward_to=None,
                 local_part="dev", dry_run=False):
        self.calls.append({"domain": domain, "worker": worker,
                           "forward_to": forward_to, "local_part": local_part,
                           "dry_run": dry_run})
        out = self.per_domain.get(domain, self.result)
        if out is None:
            target = forward_to or ("worker:%s" % worker)
            out = ({"status": "dry_run", "would": "create", "target": target}
                   if dry_run
                   else {"status": "configured", "created": True,
                         "target": target})
        return dict(out)


class _DB:
    def __init__(self, rows): self._rows = rows
    def all_rows(self, use_cache=True): return [dict(r) for r in self._rows]
    def update_row(self, cn, updates): raise AssertionError(
        "stage_email must not write to the database")


def run_stage(rows, result=None, per_domain=None, dry_run=False,
              base_dir=None, worker=""):
    calls = _Routes(result, per_domain)
    saved = (cloudflare_dns.ensure_dev_route, pipeline.db, pipeline.BASE_DIR,
             pipeline.COMPANIES_DIR)
    saved_worker = os.environ.get("CLOUDFLARE_EMAIL_WORKER")
    buf = io.StringIO()
    err = None
    try:
        cloudflare_dns.ensure_dev_route = calls
        pipeline.db = _DB(rows)
        if worker:
            os.environ["CLOUDFLARE_EMAIL_WORKER"] = worker
        else:
            os.environ.pop("CLOUDFLARE_EMAIL_WORKER", None)
        if base_dir is not None:
            # COMPANIES_DIR is bound at import from BASE_DIR, so patching
            # BASE_DIR alone does NOT redirect it - a check of mine passed
            # for that exact wrong reason. Both are patched together.
            pipeline.BASE_DIR = Path(base_dir)
            pipeline.COMPANIES_DIR = (Path(base_dir) / "pipeline_output"
                                      / "companies")
        with redirect_stdout(buf):
            pipeline.stage_email(dry_run=dry_run)
    except Exception as e:                      # noqa: BLE001 - reported
        err = e
    finally:
        (cloudflare_dns.ensure_dev_route, pipeline.db, pipeline.BASE_DIR,
         pipeline.COMPANIES_DIR) = saved
        if saved_worker is None:
            os.environ.pop("CLOUDFLARE_EMAIL_WORKER", None)
        else:
            os.environ["CLOUDFLARE_EMAIL_WORKER"] = saved_worker
    return calls, buf.getvalue(), err


def _co(cn="00000001", name="ACME LTD", domain="acme.uk",
        email="owner@real.example"):
    # Company Name matters: the manifest lives under _co_dir(cn, name), so a
    # row without it resolves to the bare company number and finds nothing.
    return {"Company Number": cn, "Company Name": name,
            "Domain": domain, "Assigned Email": email}


ONE = [_co()]


# ── the production-safety invariant ──────────────────────
def check_dry_run(r: Results) -> None:
    print("\n-- dry_run writes nothing --")
    calls, out, err = run_stage(ONE, dry_run=True)
    r.check("raised nothing", err is None, repr(err))
    r.check("the provider was still consulted", len(calls.calls) == 1,
            f"calls={calls.calls}")
    r.check("and it was told this is a dry run",
            calls.calls and calls.calls[0]["dry_run"] is True,
            f"calls={calls.calls}")
    r.check("the output says what it WOULD do", "DRY" in out, f"out {out!r}")
    r.check("...naming the address", "dev@acme.uk" in out, f"out {out!r}")

    calls, out, err = run_stage(ONE, dry_run=False)
    r.check("the same input does NOT dry-run when the flag is off",
            calls.calls and calls.calls[0]["dry_run"] is False,
            f"calls={calls.calls}")
    r.check("...and reports a real write", "SET" in out, f"out {out!r}")


def check_the_merge_hazard_is_gone(r: Results) -> None:
    """The replacement for check_preserves_existing and
    check_malformed_forward_refused. See the module docstring: the
    read-modify-write of a whole forwarding set no longer exists, so the
    thing to assert is its ABSENCE, not a guard around it."""
    print("\n-- the read-merge-replace hazard no longer exists --")
    src = open(os.path.join(REPO, "pipeline.py"), encoding="utf-8").read()
    body = src[src.index("def stage_email("):]
    body = body[:body.index("\ndef ", 10)]
    code = "\n".join(l for l in body.splitlines()
                     if not l.strip().startswith("#"))
    for banned in ("getEmailForwarding", "setEmailForwarding", "setHosts",
                   "api.namecheap.com", "ET.fromstring"):
        r.check(f"the stage no longer calls {banned}", banned not in code,
                f"found {banned!r} in stage_email")
    calls, out, err = run_stage(ONE)
    r.check("exactly one provider call per company", len(calls.calls) == 1,
            f"calls={calls.calls} - a read-then-write would be two")


def check_already_configured(r: Results) -> None:
    print("\n-- a route that already exists is reported, not rewritten --")
    calls, out, err = run_stage(ONE, route(created=False))
    r.check("raised nothing", err is None, repr(err))
    r.check("reported as already routed", "already routed" in out,
            f"out {out!r}")
    r.check("not counted as a new route", "0 route(s) created" in out,
            f"out {out!r}")

    calls, out, err = run_stage(ONE, route(created=True))
    r.check("a newly created route IS counted", "1 route(s) created" in out,
            f"out {out!r}")


def check_status_taxonomy(r: Results) -> None:
    """Every way ensure_dev_route can not-work must reach the operator."""
    print("\n-- each failure status is counted and explained --")
    cases = [
        ("not_delegated", {"status": "not_delegated",
                           "message": "acme.uk has no zone here"},
         "SKIP", "no zone here", "1 skipped"),
        ("error", {"status": "error", "error": "Cloudflare said no"},
         "ERROR", "Cloudflare said no", "1 failed"),
        ("pending_verification", {"status": "pending_verification",
                                  "message": "nobody clicked the link"},
         "ERROR", "nobody clicked the link", "1 failed"),
        ("routing_not_ready", {"status": "routing_not_ready",
                               "message": "Email Routing is not ready"},
         "ERROR", "not ready", "1 failed"),
    ]
    for status, result, marker, needle, tally in cases:
        calls, out, err = run_stage(ONE, result)
        r.check(f"{status}: raised nothing", err is None, repr(err))
        r.check(f"{status}: printed as {marker}", marker in out, f"out {out!r}")
        r.check(f"{status}: the reason reaches the operator", needle in out,
                f"out {out!r}")
        r.check(f"{status}: counted as {tally}", tally in out, f"out {out!r}")
        r.check(f"{status}: the status word itself is shown",
                status in out or marker == "SKIP", f"out {out!r}")


def check_partial_completion(r: Results) -> None:
    print("\n-- one company's bad day does not stop the rest --")
    rows = [_co(cn="00000001", domain="good.uk"),
            _co(cn="00000002", domain="bad.uk"),
            _co(cn="00000003", domain="also-good.uk")]
    calls, out, err = run_stage(rows, per_domain={
        "bad.uk": {"status": "error", "error": "provider exploded"}})
    r.check("raised nothing", err is None, repr(err))
    r.check("all three were attempted", len(calls.calls) == 3,
            f"calls={[c['domain'] for c in calls.calls]}")
    r.check("the good ones were configured",
            out.count("SET") == 2, f"out {out!r}")
    r.check("the bad one is reported, not silent", "provider exploded" in out,
            f"out {out!r}")
    r.check("the summary accounts for all of them",
            "2 route(s) created" in out and "1 failed" in out
            and "of 3 considered" in out, f"out {out!r}")


def check_worker_is_preferred_over_a_forward(r: Results) -> None:
    print("\n-- a Worker route is requested when one is configured --")
    calls, out, err = run_stage(ONE, worker="gglabs-inbound")
    r.check("the worker name is passed through",
            calls.calls and calls.calls[0]["worker"] == "gglabs-inbound",
            f"calls={calls.calls}")
    r.check("and NO forward target is passed",
            calls.calls and calls.calls[0]["forward_to"] is None,
            "a forward needs a human to verify each company's mailbox, which "
            "is the manual step a Worker exists to avoid")

    calls, out, err = run_stage(ONE)
    r.check("with no Worker configured it falls back to a forward",
            calls.calls and calls.calls[0]["worker"] is None
            and calls.calls[0]["forward_to"] == "owner@real.example",
            f"calls={calls.calls}")


def check_row_filter(r: Results) -> None:
    print("\n-- a company needs a number, a domain AND an email --")
    cases = [
        ("no number", _co(cn="")),
        ("no domain", _co(domain="")),
        ("no email", _co(email="")),
    ]
    for label, row in cases:
        calls, out, err = run_stage([row], route())
        r.check(f"{label}: nothing is contacted",
                err is None and calls.calls == [],
                f"calls={calls.calls}")
    calls, out, err = run_stage([_co()], route())
    r.check("a complete row IS processed", len(calls.calls) == 1,
            f"calls={calls.calls}; if 0 the checks above are vacuous")


def check_dev_address(r: Results) -> None:
    print("\n-- the dev mailbox is derived from the domain, not the email --")
    calls, out, err = run_stage(
        [_co(domain="weird-domain.co.uk", email="someone@elsewhere.test")],
        route(target="someone@elsewhere.test"))
    if not r.check("one provider call", len(calls.calls) == 1,
                   f"{calls.calls}"):
        return
    c = calls.calls[0]
    r.check("the domain is passed, not the email",
            c["domain"] == "weird-domain.co.uk", f"call={c!r}")
    r.check("the mailbox is 'dev'", c["local_part"] == "dev", f"call={c!r}")
    r.check("it forwards to the ASSIGNED email, not dev@domain",
            c["forward_to"] == "someone@elsewhere.test", f"call={c!r}")
    r.check("the printed address is dev@<domain>",
            "dev@weird-domain.co.uk" in out, f"out {out!r}")


# ── the manifest update ─────────────────────────────────────────────────
def _fresh_manifest(p: Path, email: str = "stale@real.example") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"support_email": email}), encoding="utf-8")


def check_manifest(r: Results) -> None:
    print("\n-- the manifest the BUILD wrote is the one updated --")
    tmp = tempfile.mkdtemp()
    try:
        po = Path(tmp) / "pipeline_output"
        # Where the build writes: assets.py:858 via generate._co_dir, which is
        # COMPANIES_DIR/<cn> - <name>/app/. _co() supplies the name.
        built = po / "companies" / "00000001 - ACME LTD" / "app" / "manifest.json"
        # The path this stage used to read, which nothing has ever written.
        stale = po / "apps" / "00000001" / "manifest.json"
        _fresh_manifest(built)
        _fresh_manifest(stale)

        calls, out, err = run_stage(ONE, base_dir=tmp)
        r.check("raised nothing", err is None, repr(err))
        r.check("the build's manifest IS updated",
                json.loads(built.read_text())["support_email"] == "dev@acme.uk",
                f"still {json.loads(built.read_text())!r}; out {out!r}")
        r.check("and the update is reported", "UPD" in out, f"out {out!r}")
        r.check("the stale pipeline_output/apps path is NOT touched",
                json.loads(stale.read_text())["support_email"]
                == "stale@real.example",
                "the old path is still being written")

        # A row with no company name resolves to the bare number, so it must
        # not silently update some other company's manifest.
        bare = po / "companies" / "00000001" / "app" / "manifest.json"
        _fresh_manifest(built)
        _fresh_manifest(bare)
        calls, out, err = run_stage([_co(name="")], base_dir=tmp)
        r.check("a nameless row updates <cn>/, not <cn> - <name>/",
                json.loads(bare.read_text())["support_email"] == "dev@acme.uk"
                and json.loads(built.read_text())["support_email"]
                == "stale@real.example",
                f"bare={json.loads(bare.read_text())!r} "
                f"named={json.loads(built.read_text())!r}")

        # Already correct -> left alone, and said so.
        _fresh_manifest(built, "dev@acme.uk")
        calls, out, err = run_stage(ONE, route(created=False), base_dir=tmp)
        r.check("an already-correct manifest is not rewritten and says so",
                "manifest already has" in out, f"out {out!r}")

        # dry_run must not write the manifest either.
        _fresh_manifest(built)
        calls, out, err = run_stage(ONE, dry_run=True, base_dir=tmp)
        r.check("dry_run leaves the manifest untouched",
                json.loads(built.read_text())["support_email"]
                == "stale@real.example", f"out {out!r}")
        r.check("dry_run still reports what it would change",
                "DRY" in out and "support_email" in out, f"out {out!r}")

        # A missing manifest must not raise - the build may not have run.
        shutil.rmtree(po / "companies")
        calls, out, err = run_stage(ONE, base_dir=tmp)
        r.check("a missing manifest is not an error", err is None, repr(err))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_unrouted_company_does_not_get_a_support_email(r: Results) -> None:
    """A company whose dev@ is not routed must NOT have that address shipped
    into its app manifest.

    support_email is what Play Console review writes to. Advertising
    dev@<domain> for a domain whose mail does not work is worse than leaving
    the stale value: it looks configured and silently goes nowhere. Found by
    a surviving mutant that deleted the `continue` after the SKIP.
    """
    print("\n-- an unrouted company's manifest is left alone --")
    tmp = tempfile.mkdtemp()
    try:
        po = Path(tmp) / "pipeline_output"
        built = po / "companies" / "00000001 - ACME LTD" / "app" / "manifest.json"
        for status, result in [
            ("not_delegated", {"status": "not_delegated",
                               "message": "acme.uk has no zone here"}),
            ("error", {"status": "error", "error": "provider exploded"}),
            ("pending_verification", {"status": "pending_verification",
                                      "message": "nobody clicked"}),
        ]:
            _fresh_manifest(built)
            calls, out, err = run_stage(ONE, result, base_dir=tmp)
            r.check(f"{status}: the manifest is NOT rewritten",
                    json.loads(built.read_text())["support_email"]
                    == "stale@real.example",
                    f"manifest became "
                    f"{json.loads(built.read_text())!r} - the app would "
                    f"advertise a support address that receives nothing")
            r.check(f"{status}: and no manifest update is claimed",
                    "UPD" not in out, f"out {out!r}")

        # The check must be capable of failing: a CONFIGURED route does write.
        _fresh_manifest(built)
        calls, out, err = run_stage(ONE, route(), base_dir=tmp)
        r.check("a configured route DOES update the manifest",
                json.loads(built.read_text())["support_email"] == "dev@acme.uk",
                "if this fails the three checks above are vacuous")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_a_broken_dev_route_does_not_mark_the_stage_complete(r: Results) -> None:
    """The dashboard's stage tick, and the run-all skip, must not read
    "an address was assigned" as "mail reaches this company".

    _compute_stages decides two things at once: which tick the operator sees,
    and which stages a run-all SKIPS. Keyed on Assigned Email alone, a company
    whose dev@ route failed is skipped for ever: the email stage returns an
    error the first time and halts the chain, but on the next run-all the
    stage reads done, so the chain walks straight past the one thing that is
    broken and on to director_id and app - which ship dev@<domain> as the
    support address Play review writes to.

    Same family as the surviving mutant above, one layer up: there the stage
    body could advertise an unrouted address, here the stage is never re-run
    to fix it.
    """
    print("\n-- an assigned address is not a working route --")
    import api_v2

    def stages(**row):
        base = {"Company Name": "ACME LTD", "Domain": "acme.uk"}
        base.update(row)
        return api_v2._compute_stages("00000001", base)

    s = stages(**{"Assigned Email": "owner@real.example",
                  "Dev Email Forwarding": "configured"})
    # The control, first: without this the checks below are vacuous, because
    # a _compute_stages that returned False for every email row would pass
    # all of them.
    r.check("a configured route DOES mark the stage complete",
            s.get("email") is True,
            "if this fails every check below is vacuous: " + repr(s))

    for status in ("pending_verification", "not_delegated", "routing_not_ready",
                   "error", ""):
        s = stages(**{"Assigned Email": "owner@real.example",
                      "Dev Email Forwarding": status})
        r.check(f"{status!r} does NOT mark the stage complete",
                s.get("email") is False,
                "run-all would skip the email stage and ship an unrouted "
                "dev@ address as the Play review contact: " + repr(s))

    # And the older half of the condition still holds: no address, not done,
    # whatever the routing column happens to say.
    s = stages(**{"Assigned Email": "", "Dev Email Forwarding": "configured"})
    r.check("no assigned address is not complete either",
            s.get("email") is False, repr(s))


def check_print_summary(r: Results) -> None:
    print("\n-- print_summary reads what the build wrote --")
    tmp = tempfile.mkdtemp()
    try:
        po = Path(tmp) / "pipeline_output"
        app = po / "companies" / "13510663 - PORTABILITY FIXTURE LTD" / "app"
        (app / "artifacts").mkdir(parents=True)
        # Artifact paths are stored RELATIVE TO app/, as the real build writes
        # them; resolving them against the company directory finds nothing.
        (app / "artifacts" / "a.apk").write_bytes(b"x")
        (app / "manifest.json").write_text(json.dumps({
            "apk": "artifacts/a.apk", "aab": "artifacts/missing.aab",
            "domain": "fixture-example.uk", "privacy_policy_url": None,
            "support_email": "dev@fixture-example.uk"}), encoding="utf-8")
        # stage_director_id writes here, not to pipeline_output/representative_ids.
        idd = po / "companies" / "13510663 - PORTABILITY FIXTURE LTD" / "director_id"
        idd.mkdir(parents=True)
        (idd / "ID Front.jpeg").write_bytes(b"x")

        saved = (pipeline.BASE_DIR, pipeline.COMPANIES_DIR)
        buf = io.StringIO()
        try:
            pipeline.BASE_DIR = Path(tmp)
            pipeline.COMPANIES_DIR = po / "companies"
            with redirect_stdout(buf):
                pipeline.print_summary()
        finally:
            pipeline.BASE_DIR, pipeline.COMPANIES_DIR = saved
        out = buf.getvalue()

        r.check("the company is listed at all",
                "13510663 - PORTABILITY FIXTURE LTD" in out, f"out {out!r}")
        r.check("an artifact that exists reads APK:OK", "APK:OK" in out,
                f"out {out!r}")
        r.check("an artifact that does not exist reads AAB:--",
                "AAB:--" in out, f"out {out!r}")
        r.check("the ID is found under <company>/director_id/",
                "ID:OK" in out, f"out {out!r}")
        r.check("a null privacy_policy_url reads Policy:--",
                "Policy:--" in out, f"out {out!r}")
        r.check("the domain is reported", "Domain:OK" in out, f"out {out!r}")
        r.check("the support email is shown",
                "dev@fixture-example.uk" in out, f"out {out!r}")
        r.check("it no longer claims pipeline_output/apps is missing",
                "No pipeline_output/apps" not in out, f"out {out!r}")

        # An empty companies dir must say so rather than printing nothing.
        shutil.rmtree(po / "companies")
        (po / "companies").mkdir(parents=True)
        buf = io.StringIO()
        saved = (pipeline.BASE_DIR, pipeline.COMPANIES_DIR)
        try:
            pipeline.BASE_DIR = Path(tmp)
            pipeline.COMPANIES_DIR = po / "companies"
            with redirect_stdout(buf):
                pipeline.print_summary()
        finally:
            pipeline.BASE_DIR, pipeline.COMPANIES_DIR = saved
        r.check("an empty companies directory is reported, not silent",
                "holds no manifest.json" in buf.getvalue(),
                f"out {buf.getvalue()!r}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_co_dir_matches_writer(r: Results) -> None:
    print("\n-- _co_dir agrees with the code that creates the directory --")
    # THE invariant behind both fixes. pipeline._co_dir only finds the
    # manifest if it builds the same folder name as the writer,
    # apps/generator/generate.py:107-108. A mutation replacing pipeline's
    # re.sub with a bare strip SURVIVED this suite until this check existed:
    # no fixture had a name containing the stripped characters, so the two
    # constructions could diverge silently and every manifest lookup would
    # quietly miss.
    import apps.generator.generate as gen

    class _C:
        def __init__(self, n, nm):
            self.company_number, self.company_name = n, nm

    cases = [
        ("13510663", "PORTABILITY FIXTURE LTD"),
        ("00000001", ""),                        # empty -> bare number
        ("00000002", "   "),                     # whitespace only
        ("00000003", 'A/B:C*D?E"F<G>H|I\\J'),    # every stripped character
        ("00000004", "  Leading & trailing  "),
        ("00000005", "Ünïcodé Ltd"),
        ("00000006", "Dash - In - Name Ltd"),
    ]
    saved = pipeline.COMPANIES_DIR
    try:
        pipeline.COMPANIES_DIR = Path(gen.COMPANIES_DIR)
        mismatches = []
        for cn, nm in cases:
            writer = os.path.normpath(gen._co_dir(_C(cn, nm)))
            reader = os.path.normpath(str(pipeline._co_dir(cn, nm) / "app"))
            if writer != reader:
                mismatches.append((nm, writer, reader))
        r.check(f"all {len(cases)} names resolve identically on both sides",
                not mismatches,
                "; ".join(f"{nm!r}: writer={w} reader={rd}"
                          for nm, w, rd in mismatches))
        # And the check must be capable of failing: a deliberately wrong
        # construction has to be caught, or the loop above proves nothing.
        bogus = os.path.normpath(str(saved / "00000003 - A/B" / "app"))
        r.check("the comparison would catch a divergence",
                bogus != os.path.normpath(gen._co_dir(_C("00000003", 'A/B'))),
                "the comparison cannot distinguish a wrong folder name")
    finally:
        pipeline.COMPANIES_DIR = saved


def check_no_db_write(r: Results) -> None:
    print("\n-- the stage never writes to the database --")
    # _DB.update_row raises; reaching it would surface as an error here.
    calls, out, err = run_stage(ONE, route())
    r.check("no db write attempted", err is None, f"err={err!r}")


def main() -> int:
    r = Results()
    print(f"target: {os.path.join(REPO, 'pipeline.py')} stage_email")
    check_dry_run(r)
    check_the_merge_hazard_is_gone(r)
    check_already_configured(r)
    check_status_taxonomy(r)
    check_partial_completion(r)
    check_worker_is_preferred_over_a_forward(r)
    check_row_filter(r)
    check_dev_address(r)
    check_manifest(r)
    check_unrouted_company_does_not_get_a_support_email(r)
    check_a_broken_dev_route_does_not_mark_the_stage_complete(r)
    check_print_summary(r)
    check_co_dir_matches_writer(r)
    check_no_db_write(r)
    return r.report()


if __name__ == "__main__":
    raise SystemExit(main())
