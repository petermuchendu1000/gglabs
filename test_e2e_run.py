#!/usr/bin/env python3
"""e2e_run.py is a harness, and a harness that lies is worse than none.

Two things here are load-bearing and neither is obvious:

  1. THE MONEY GATE. e2e_run drives the stage that buys a domain. If the
     --spend gate is wrong in the permissive direction it spends money nobody
     authorised; if it is wrong in the other direction the chain stops for no
     reason. Both are tested, and the "already bought" case is tested too,
     because a company that HAS a domain must not need --spend to be re-run.

  2. THE VERDICT. A run whose stages were all complete beforehand prints a
     column of ticks and proves nothing. If that exits 0 the harness has
     reported a proof it does not have - the same silent-success family this
     codebase keeps removing, this time about itself.

Every check below has a control that makes it capable of failing.
"""
from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stdout

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

import e2e_run
import api_v2


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
                print(f"          {detail}")
        return bool(cond)

    def report(self) -> int:
        print(f"\n{self.passed} passed / {self.failed} failed")
        return 1 if self.failed else 0


# ── The money gate ─────────────────────────────────────────────────────────

def check_money_gate(r: Results) -> None:
    print("\n-- the domain stage cannot run without --spend --")
    r.check("domain is named as a money stage",
            "domain" in e2e_run.MONEY_STAGES,
            "nothing would gate the purchase: " + repr(e2e_run.MONEY_STAGES))

    r.check("pending + no --spend is BLOCKED",
            e2e_run.plan_stage("domain", False, False) == e2e_run.BLOCKED,
            "a domain would be bought without authorisation")
    # The control: the gate must open when told to, or the check above passes
    # for a plan_stage that blocks everything for ever.
    r.check("pending + --spend RUNS",
            e2e_run.plan_stage("domain", False, True) == e2e_run.RUN,
            "--spend does nothing, so the money stage can never run")
    # And a domain already bought must not demand --spend to re-run the rest.
    r.check("already bought + no --spend is ALREADY, not BLOCKED",
            e2e_run.plan_stage("domain", True, False) == e2e_run.ALREADY,
            "a company that already has a domain could never be re-run "
            "without passing --spend, which would arm the gate for nothing")

    for s in api_v2.STAGE_ORDER:
        if s in e2e_run.MONEY_STAGES:
            continue
        r.check(f"{s} needs no --spend",
                e2e_run.plan_stage(s, False, False) == e2e_run.RUN,
                "a free stage is gated behind a money flag")
        r.check(f"{s} complete is ALREADY",
                e2e_run.plan_stage(s, True, False) == e2e_run.ALREADY, s)

    # --force re-runs a completed stage. Needed because new code inside an
    # already-complete stage can never otherwise run: the app stage finished
    # before the privacy publisher existed, so that company has no policy
    # URL and no way to acquire one from the console.
    r.check("force re-runs a completed free stage",
            e2e_run.plan_stage("email", True, False, force=True)
            == e2e_run.RUN,
            "a finished stage could never be re-run to exercise new code")
    # But forcing the MONEY stage still needs --spend. Forcing a re-run of
    # `domain` means buying a second domain, and --force is not consent to
    # spend; the two flags authorise different things.
    r.check("forcing the money stage still needs --spend",
            e2e_run.plan_stage("domain", True, False, force=True)
            == e2e_run.BLOCKED,
            "--force would have bought a second domain on its own")
    r.check("...and with both flags it runs",
            e2e_run.plan_stage("domain", True, True, force=True)
            == e2e_run.RUN, "the two together are explicit enough")
    r.check("force changes nothing for a stage that is not done",
            e2e_run.plan_stage("email", False, False, force=True)
            == e2e_run.RUN, "it was going to run anyway")


# ── The verdict ────────────────────────────────────────────────────────────

def check_verdict(r: Results) -> None:
    print("\n-- the verdict counts what RAN, not what is green --")
    order = list(api_v2.STAGE_ORDER)

    v = e2e_run.final_verdict(order, {s: e2e_run.DONE for s in order})
    r.check("every stage executed is proven", v["proven"] is True, repr(v))
    r.check("...and all of them count as executed",
            len(v["executed"]) == len(order), repr(v))

    v = e2e_run.final_verdict(order, {s: e2e_run.ALREADY for s in order})
    r.check("every stage ALREADY complete is still 'proven'",
            v["proven"] is True, repr(v))
    r.check("...but nothing is counted as executed",
            v["executed"] == [],
            "a run that did nothing would be reported as a proof: " + repr(v))

    for bad, label in ((e2e_run.FAILED, "a failure"),
                       (e2e_run.BLOCKED, "a blocked stage"),
                       (e2e_run.NOT_RUN, "a stage never reached")):
        out = {s: e2e_run.DONE for s in order}
        out[order[-1]] = bad
        v = e2e_run.final_verdict(order, out)
        r.check(f"{label} makes the run NOT proven", v["proven"] is False,
                repr(v))
        r.check(f"...and {label} is named in unproven",
                v["unproven"] == [order[-1]], repr(v))

    # A stage missing from the outcomes dict entirely must not read as proven.
    v = e2e_run.final_verdict(order, {})
    r.check("stages with no recorded outcome are not proven",
            v["proven"] is False and v["unproven"] == order, repr(v))


# ── The driver ─────────────────────────────────────────────────────────────

class Fake:
    """Stands in for api_v2's four decision points, recording the order."""

    def __init__(self, done=(), fail_on=None, succeed_but_incomplete=None,
                 reads_complete=()):
        self.done = set(done)
        self.fail_on = fail_on
        self.succeed_but_incomplete = succeed_but_incomplete
        # Stages that read COMPLETE once attempted, whatever they returned.
        # Not a contrivance: a stage that writes its own column and then fails
        # later in its body lands exactly here - _compute_stages reads the
        # column, sees it filled, and calls the stage done.
        self.reads_complete = set(reads_complete)
        self.ran: list[str] = []

    def find_row(self, cn):
        return {"Company Name": "ACME LTD", "Company Number": cn}

    def compute(self, cn, row):
        return {s: s in self.done or (s in self.reads_complete and s in self.ran)
                for s in api_v2.STAGE_ORDER}

    def exec_stage(self, cn, stage, row):
        self.ran.append(stage)
        if stage == self.fail_on:
            return {"status": "error", "error": "the provider refused"}
        if stage != self.succeed_but_incomplete:
            self.done.add(stage)   # the work sticks
        return {"status": "done"}


def drive(fake, **kw):
    saved = (api_v2._find_row, api_v2._compute_stages, api_v2._exec_stage)
    api_v2._find_row = fake.find_row
    api_v2._compute_stages = fake.compute
    api_v2._exec_stage = fake.exec_stage
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            code = e2e_run.drive("00000001", **kw)
    finally:
        (api_v2._find_row, api_v2._compute_stages,
         api_v2._exec_stage) = saved
    return code, buf.getvalue()


def check_driver_halts_at_the_money_stage(r: Results) -> None:
    print("\n-- without --spend the chain stops AT domain, not after it --")
    f = Fake()
    code, out = drive(f, allow_spend=False)
    i = api_v2.STAGE_ORDER.index("domain")
    r.check("the stages before domain all ran",
            f.ran == api_v2.STAGE_ORDER[:i], repr(f.ran))
    r.check("domain itself did NOT run", "domain" not in f.ran, repr(f.ran))
    r.check("and nothing after it ran",
            not any(s in f.ran for s in api_v2.STAGE_ORDER[i + 1:]),
            "delegate/email/app ran on a company with no domain, which "
            "proves nothing: " + repr(f.ran))
    r.check("the exit code is non-zero", code != 0, repr(code))
    r.check("the transcript says it is not proven", "NOT PROVEN" in out,
            out[-400:])
    # The control: with --spend the very same fake runs everything.
    f2 = Fake()
    code2, out2 = drive(f2, allow_spend=True)
    r.check("with --spend every stage runs, in order",
            f2.ran == list(api_v2.STAGE_ORDER), repr(f2.ran))
    r.check("...and that run IS proven", code2 == 0 and "PROVEN" in out2,
            f"{code2} {out2[-400:]}")


def check_driver_halts_on_failure(r: Results) -> None:
    print("\n-- a failing stage stops the chain --")
    f = Fake(fail_on="delegate")
    code, out = drive(f, allow_spend=True)
    i = api_v2.STAGE_ORDER.index("delegate")
    r.check("the failing stage is the last one that ran",
            f.ran == list(api_v2.STAGE_ORDER[:i + 1]), repr(f.ran))
    r.check("the exit code is non-zero", code != 0, repr(code))
    r.check("the reason is shown", "the provider refused" in out, out[-400:])
    r.check("the stages after it are reported 'not run'",
            "not reached" in out, out[-600:])


def check_driver_will_not_call_an_untouched_run_a_proof(r: Results) -> None:
    print("\n-- a company that was already finished is not a proof --")
    f = Fake(done=api_v2.STAGE_ORDER)
    code, out = drive(f, allow_spend=True)
    r.check("no stage ran", f.ran == [], repr(f.ran))
    r.check("the exit code is non-zero even though every stage is green",
            code != 0,
            "a run that did nothing reported success, which is the whole "
            "defect this harness exists to avoid")
    r.check("and it says so in words", "proves nothing" in out, out[-500:])


def check_driver_notices_work_that_did_not_stick(r: Results) -> None:
    print("\n-- a stage that returns success but leaves the tick off --")
    # Exactly the email stage with a broken dev@ route: status success, but
    # _compute_stages still reads the stage as incomplete.
    f = Fake(succeed_but_incomplete="email")
    code, out = drive(f, allow_spend=True)
    r.check("the stage is still reported", "email" in f.ran, repr(f.ran))
    r.check("the disagreement is printed",
            "INCOMPLETE" in out,
            "a stage whose work did not stick passed silently: " + out[-600:])
    r.check("and the run is NOT proven", code != 0, repr(code))
    # The control: a stage whose work sticks prints no such warning.
    f2 = Fake()
    code2, out2 = drive(f2, allow_spend=True)
    r.check("a run where everything sticks prints no warning",
            "INCOMPLETE" not in out2, out2[-400:])


def check_no_stick_alone_is_not_a_proof(r: Results) -> None:
    """Isolate the "did not stick" outcome from the halt it causes.

    The earlier check runs a whole chain, so the later stages are unreached
    and the run is unproven for two reasons at once. Mutation testing found
    that: filing the stage as DONE, and counting NO_STICK as proven, both
    survived, because "not proven" was over-determined. One stage, on its
    own, pins each.
    """
    print("\n-- one stage whose work did not stick, and nothing else --")
    f = Fake(succeed_but_incomplete="email")
    code, out = drive(f, allow_spend=True, only=["email"])
    r.check("the stage did run", f.ran == ["email"], repr(f.ran))
    r.check("the run is not proven", code != 0,
            "a single stage whose work did not stick was reported as a "
            "complete, proven pipeline")
    r.check("...and the transcript says NOT PROVEN", "NOT PROVEN" in out,
            "the verdict claimed a proof it does not have: " + out[-500:])
    r.check("...and names the reason", "did not stick" in out, out[-500:])
    r.check("the stage is NOT marked ok in the table",
            "ok       email" not in out,
            "the table shows a tick for a stage that did not finish: "
            + out[-500:])

    # The control: the same single stage, work sticking, IS a proof.
    f2 = Fake()
    code2, out2 = drive(f2, allow_spend=True, only=["email"])
    r.check("one stage that does stick is proven",
            code2 == 0 and "NOT PROVEN" not in out2,
            f"{code2} {out2[-400:]}")


def check_a_failure_that_reads_complete_still_halts(r: Results) -> None:
    """A stage can fail AND leave _compute_stages reading it complete.

    A stage that writes its own column before erroring does exactly this. The
    halt on failure must not be load-bearing on the "did not stick" check
    noticing - mutation testing showed it was: deleting the failure halt
    changed nothing, because every failing stage in the tests also read
    incomplete afterwards.
    """
    print("\n-- a failing stage halts even when it reads complete --")
    i = api_v2.STAGE_ORDER.index("delegate")
    f = Fake(fail_on="delegate", reads_complete=["delegate"])
    code, out = drive(f, allow_spend=True)
    r.check("the chain stopped at the failing stage",
            f.ran == list(api_v2.STAGE_ORDER[:i + 1]),
            "the chain walked past a stage that reported failure because the "
            "column it had already written read as complete: " + repr(f.ran))
    r.check("the run is not proven", code != 0, repr(code))
    r.check("the failure is named, not the stickiness",
            "reported failure" in out, out[-500:])


def check_only_subset(r: Results) -> None:
    print("\n-- --only restricts the stages considered --")
    f = Fake()
    code, out = drive(f, allow_spend=True, only=["details", "duns"])
    r.check("only the named stages ran", f.ran == ["details", "duns"],
            repr(f.ran))
    r.check("a subset that all passes is proven", code == 0, out[-300:])


def _called_names(path: str) -> set[str]:
    """Every name the module actually CALLS, dotted where it is an attribute.

    Parsed, not grepped. A grep over the source cannot tell a call from the
    prose in a docstring that explains why the call is not there - which is
    how the first version of this check failed on e2e_run's own comment about
    register_company_domain.
    """
    import ast

    def dotted(node):
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = dotted(node.func if isinstance(node, ast.Call) else node.value)
            return f"{base}.{node.attr}" if base else node.attr
        return ""

    tree = ast.parse(open(path, encoding="utf-8").read())
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = dotted(node.func)
            if name:
                out.add(name)
                out.add(name.rsplit(".", 1)[-1])
    return out


def check_running_as_root_is_refused(r: Results) -> None:
    """Running this harness as root poisons the service, silently.

    Observed 2026-11-21. A console run as root created
    app/src/c16811331 owned by root:root 755. The next dashboard run died in
    shutil.rmtree with "Permission denied: 'colors.xml'", because
    write_flavor_resources deletes and recreates that directory every time
    and the service user cannot empty a root-owned one. app/src itself was
    pipeline-owned - root's run had left exactly one directory behind, and
    that was enough.

    The root run reports nothing wrong. It gets further than the service
    ever will, so the damage is invisible from the console that caused it.
    """
    print("\n-- the harness refuses to run as root --")

    msg = e2e_run.root_guard(euid=0, service_user="pipeline",
                             allow_root=False)
    r.check("root with a service user configured is refused", bool(msg),
            "the run would leave root-owned files the service cannot delete")
    r.check("...and the message names the service user",
            "pipeline" in (msg or ""), repr(msg))
    r.check("...and tells the operator how to run it properly",
            "sudo -u pipeline" in (msg or ""), repr(msg))
    r.check("...and names the escape hatch",
            "--allow-root" in (msg or ""), repr(msg))

    # The controls. Each must be capable of returning None, or the guard is
    # simply "always refuse" and would be switched off within a day.
    r.check("the service user itself is allowed",
            e2e_run.root_guard(euid=999, service_user="pipeline",
                               allow_root=False) is None,
            "the correct way to run it was blocked")
    r.check("root with --allow-root is allowed",
            e2e_run.root_guard(euid=0, service_user="pipeline",
                               allow_root=True) is None,
            "the escape hatch does nothing")
    r.check("root on a box with no service user is allowed",
            e2e_run.root_guard(euid=0, service_user="", allow_root=False)
            is None,
            "a developer machine with no systemd unit cannot run the "
            "harness at all")


def check_force_reruns_a_finished_stage(r: Results) -> None:
    """Through drive(), not just the predicate.

    The app stage finished before the privacy publisher existed, so that
    company carries no policy URL. Without --force there is no way to make a
    completed stage run again from the console, and new code inside it can
    never execute.
    """
    print("\n-- --force re-runs a stage that is already complete --")
    f = Fake(done=api_v2.STAGE_ORDER)
    code, out = drive(f, allow_spend=True, only=["app"], force=["app"])
    r.check("the finished stage ran", f.ran == ["app"],
            f"nothing re-ran: {f.ran!r}")
    r.check("the plan says it was forced", "FORCED" in out,
            "an operator reading the log cannot tell it was overridden: "
            + out[:400])
    r.check("...and the run counts as a proof", code == 0, out[-300:])

    # The control: the same company WITHOUT --force must still skip.
    f2 = Fake(done=api_v2.STAGE_ORDER)
    code2, out2 = drive(f2, allow_spend=True, only=["app"])
    r.check("without --force it is still skipped", f2.ran == [], repr(f2.ran))
    r.check("...and still proves nothing", code2 != 0 and "proves nothing" in out2,
            f"{code2} {out2[-300:]}")

    # Forcing one stage must not force the others.
    f3 = Fake(done=api_v2.STAGE_ORDER)
    drive(f3, allow_spend=True, force=["email"])
    r.check("forcing one stage leaves the rest skipped", f3.ran == ["email"],
            f"--force leaked to other stages: {f3.ran!r}")


def check_main_actually_runs(r: Results) -> None:
    """Exercise main(), not just the predicate it calls.

    The predicate tests above all passed while main() carried a NameError:
    it called os.geteuid() and e2e_run did not import os. A pure function
    tested in isolation says nothing about the entry point that calls it,
    and this entry point is the only way anybody runs the harness.
    """
    print("\n-- main() reaches the guard without blowing up --")
    saved_argv = sys.argv
    buf = io.StringIO()
    try:
        sys.argv = ["e2e_run.py", "--company", "00000001"]
        with redirect_stdout(buf):
            code = e2e_run.main()
    except Exception as exc:                    # noqa: BLE001
        r.check("main() runs without raising", False,
                f"{type(exc).__name__}: {exc}")
        return
    finally:
        sys.argv = saved_argv
    out = buf.getvalue()

    # In this container euid is 0 and the unit names a service user, so the
    # guard must fire. That is also the server's situation.
    if os.geteuid() == 0:
        r.check("main() refuses as root and exits non-zero", code == 2,
                f"exit {code}: {out[-300:]!r}")
        r.check("...with the refusal text", "Refusing to run as root" in out,
                repr(out[:200]))
    else:
        r.check("main() got past the guard as a non-root user",
                "Refusing to run as root" not in out, repr(out[:200]))


def check_it_is_not_a_second_pipeline(r: Results) -> None:
    """The harness must delegate, not reimplement. If it ever grows its own
    idea of what a stage does, a green run here stops saying anything about
    the dashboard that operators actually use."""
    print("\n-- the harness delegates to the real stage executor --")
    called = _called_names(os.path.join(REPO, "e2e_run.py"))
    # The control: the parser must actually find calls, or every check below
    # passes against an empty set.
    r.check("the source parsed and calls were found", len(called) > 10,
            f"only found {sorted(called)!r}")

    for needed in ("api_v2._exec_stage", "api_v2._compute_stages",
                   "api_v2._result_failure"):
        r.check(f"it calls {needed}", needed in called,
                "the harness has its own copy of this decision")
    for forbidden in ("register_company_domain", "delegate_domain",
                      "ensure_dev_route", "download_certificate",
                      "enable_email_routing", "set_custom_dns"):
        r.check(f"it does NOT call {forbidden} itself",
                forbidden not in called,
                "the harness bypasses _exec_stage for this stage, so a green "
                "run proves nothing about the dashboard path")


def main() -> int:
    r = Results()
    print("test_e2e_run.py — the end-to-end harness must not lie")
    print(f"target: {os.path.join(REPO, 'e2e_run.py')}")
    check_money_gate(r)
    check_verdict(r)
    check_driver_halts_at_the_money_stage(r)
    check_driver_halts_on_failure(r)
    check_driver_will_not_call_an_untouched_run_a_proof(r)
    check_driver_notices_work_that_did_not_stick(r)
    check_no_stick_alone_is_not_a_proof(r)
    check_a_failure_that_reads_complete_still_halts(r)
    check_only_subset(r)
    check_running_as_root_is_refused(r)
    check_force_reruns_a_finished_stage(r)
    check_main_actually_runs(r)
    check_it_is_not_a_second_pipeline(r)
    return r.report()


if __name__ == "__main__":
    raise SystemExit(main())
