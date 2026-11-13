#!/usr/bin/env python3
"""Tests for stage feedback: what the dashboard is told when a stage runs.

    python3 test_stage_feedback.py

Hermetic: no network, no Namecheap account, no database. The stage function
itself is stubbed, because what is under test is the job runner and the log
plumbing around it, not any one stage.

Three failures this pins, all of them "the stage ran and said nothing":

  * The domain stage writes NOTHING to stdout. Every line it has to say goes
    through the `money` logger, and logging_setup sends log records to stderr
    on purpose, while the dashboard's per-stage log is a capture of stdout
    (api_v2._Tee). Measured on the unfixed code: a rejected domain order
    produced 0 bytes of job log and two stderr lines nobody reading the
    dashboard can see.

  * api_v2._spawn marked a job "done" whenever the stage function returned
    without raising. register_company_domain reports a rejected order as
    {"status": "error"} and an order whose outcome is UNKNOWN as
    {"status": "unresolved"} (run_pipeline.py:738, :773), and get_duns_number
    reports an exhausted retry the same way (run_pipeline.py:599). So a
    domain that was never bought - or was charged and left unresolved - got a
    green tick, no error, and no Error Log button, while the explanation sat
    unread in the job's `result` field.

  * /run-all did not stop on such a failure either, so it walked on to the
    next stage as though the previous one had succeeded.

The money-critical case is "unresolved": the purchase claim is deliberately
KEPT, a charge may have happened, and a human has to reconcile. That is the
one outcome that must never render as success.
"""
import io
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

from logging_setup import configure_logging

configure_logging()

import api_v2


class Results:
    def __init__(self) -> None:
        self.passed = 0
        self.failed: list[str] = []
        self.skipped: list[str] = []

    def check(self, name: str, cond: bool, detail: str = "") -> bool:
        if cond:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed.append(name)
            print(f"  FAIL  {name}" + (f"\n          {detail}" if detail else ""))
        return bool(cond)

    def skip(self, name: str, why: str) -> None:
        self.skipped.append(name)
        print(f"  SKIP  {name}\n          {why}")

    def report(self) -> int:
        print()
        print(f"{self.passed} passed / {len(self.failed)} failed / "
              f"{len(self.skipped)} skipped")
        for f in self.failed:
            print(f"  failed: {f}")
        for s in self.skipped:
            print(f"  skipped: {s}")
        return 1 if self.failed else 0


# ── helpers ─────────────────────────────────────────────────────────────────

def _run_spawn(cn: str, stage: str, fn, timeout: float = 5.0) -> dict:
    """Run fn through api_v2._spawn and return the finished job dict."""
    api_v2._spawn(cn, stage, fn)
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = api_v2._job_get(cn, stage)
        if j.get("status") in ("done", "failed"):
            return j
        time.sleep(0.02)
    return api_v2._job_get(cn, stage)


def _money_line_in_job_log(cn: str, stage: str, message: str) -> str:
    """Emit one money-logger record inside a bound worker thread.

    This is exactly the shape of the real thing: register_company_domain logs
    to the `money` logger from inside the thread api_v2._spawn bound.
    """
    key = api_v2._jk(cn, stage)
    captured: dict[str, str] = {}

    def worker():
        api_v2._TEE.bind(key)
        logging.getLogger("money").info("%s", message)
        captured["log"] = api_v2._TEE.unbind()

    t = threading.Thread(target=worker)
    t.start()
    t.join(5)
    return captured.get("log", "")


# ── the money log has to reach the job log ──────────────────────────────────

def check_money_log_reaches_job_log(r: Results) -> None:
    print("\nmoney log -> per-stage job log")
    log = _money_line_in_job_log("00000001", "domain", "MONEYPROBE order issued")
    r.check("a money-logger line reaches the bound job's log buffer",
            "MONEYPROBE order issued" in log,
            f"job log was {log!r}. The domain stage says everything it has to "
            f"say through this logger; if the line does not land here, the "
            f"dashboard's log drawer is empty for the one stage that spends "
            f"money.")


def check_money_log_still_on_stderr(r: Results) -> None:
    """The audit trail must not MOVE to stdout - it must also stay on stderr.

    logging_setup documents `money` as the one channel that should never be
    filtered out, and gunicorn captures stderr (errorlog = "-").
    """
    print("\nmoney log -> stderr (audit trail preserved)")
    root = logging.getLogger()
    stderr_handlers = [h for h in root.handlers
                       if isinstance(h, logging.StreamHandler)
                       and getattr(h, "stream", None) is sys.stderr]
    r.check("the root logger still has a stderr handler",
            bool(stderr_handlers),
            f"root handlers: {root.handlers!r}")
    money = logging.getLogger("money")
    r.check("the money logger still propagates to it",
            money.propagate is True,
            "propagate=False would take the money lines off stderr, which is "
            "where the audit trail is read from")


def check_money_log_does_not_cross_jobs(r: Results) -> None:
    """Two stages running at once must not see each other's money lines.

    _Tee routes by the WRITING thread, so one shared handler is correct here;
    a per-stage handler would double-write. This pins the property either way.
    """
    print("\nmoney log -> isolated per job")
    out: dict[str, str] = {}
    barrier = threading.Barrier(2)

    def worker(tag: str):
        key = api_v2._jk("0000000" + tag, "domain")
        api_v2._TEE.bind(key)
        barrier.wait(5)
        logging.getLogger("money").info("MONEYPROBE tag=%s", tag)
        time.sleep(0.05)
        out[tag] = api_v2._TEE.unbind()

    ts = [threading.Thread(target=worker, args=(t,)) for t in ("2", "3")]
    for t in ts:
        t.start()
    for t in ts:
        t.join(5)

    r.check("job 2 saw its own money line", "tag=2" in out.get("2", ""),
            f"job 2 log: {out.get('2')!r}")
    r.check("job 2 did NOT see job 3's money line", "tag=3" not in out.get("2", ""),
            f"job 2 log: {out.get('2')!r}")
    r.check("job 3 did NOT see job 2's money line", "tag=2" not in out.get("3", ""),
            f"job 3 log: {out.get('3')!r}")


# ── a stage that fails by RETURNING must not read as success ────────────────

def check_returned_error_marks_failed(r: Results) -> None:
    print("\nreturned status=error -> job failed")
    j = _run_spawn("00000010", "domain",
                   lambda: {"status": "error",
                            "error": "Namecheap rejected the order "
                                     "(attempted domain: example.online)"})
    r.check("job status is failed", j.get("status") == "failed",
            f"status was {j.get('status')!r}; 'done' is a green tick for a "
            f"domain that was never bought")
    r.check("the returned error is carried on the job",
            "Namecheap rejected the order" in str(j.get("error") or ""),
            f"error was {j.get('error')!r}")
    r.check("the returned dict is still kept in result",
            "example.online" in str(j.get("result") or ""),
            f"result was {j.get('result')!r}")


def check_returned_unresolved_marks_failed(r: Results) -> None:
    print("\nreturned status=unresolved -> job failed (money-critical)")
    j = _run_spawn("00000011", "domain",
                   lambda: {"status": "unresolved",
                            "error": "Namecheap accepted the order but "
                                     "reported Registered=false",
                            "charged": "9.86"})
    r.check("job status is failed", j.get("status") == "failed",
            f"status was {j.get('status')!r}. This is the outcome where a "
            f"charge MAY have happened and the purchase claim is kept on "
            f"purpose; rendering it as success is how a human never finds out")
    r.check("the operator is told why",
            "Registered=false" in str(j.get("error") or ""),
            f"error was {j.get('error')!r}")


def check_success_statuses_still_done(r: Results) -> None:
    """Scope guard, not a defect test.

    This passes on the unfixed code too. It is here so the status check cannot
    quietly start failing stages that did their job: `certificate` returns an
    "error" KEY that is None on success, `duns` reports a legitimate
    "not_found", and two stages return a bare {"status": "done"}.
    """
    print("\nnon-failure returns stay done (scope guard)")
    cases = [
        ("registered domain",   {"status": "registered", "domain": "x.online"}),
        ("assigned email",      {"status": "assigned", "email": "a@b.c"}),
        ("already assigned",    {"status": "already_assigned"}),
        ("duns found",          {"status": "found", "duns_number": "123"}),
        ("duns not_found",      {"status": "not_found", "duns_number": ""}),
        ("plain done",          {"status": "done"}),
        ("certificate ok",      {"path": "/tmp/c.pdf", "error": None}),
        ("details dict",        {"company_number": "1", "row_updates": {}}),
        ("non-dict return",     "a string"),
    ]
    for i, (label, ret) in enumerate(cases):
        j = _run_spawn(f"000002{i:02d}", "domain", lambda ret=ret: ret)
        r.check(f"{label} -> done", j.get("status") == "done",
                f"status was {j.get('status')!r} for return {ret!r}")


def check_raise_still_marks_failed(r: Results) -> None:
    print("\nraising still marks the job failed (unchanged behaviour)")

    def boom():
        raise RuntimeError("stage blew up")

    j = _run_spawn("00000030", "domain", boom)
    r.check("job status is failed", j.get("status") == "failed",
            f"status was {j.get('status')!r}")
    r.check("the exception message is on the job",
            "stage blew up" in str(j.get("error") or ""),
            f"error was {j.get('error')!r}")
    r.check("the traceback is in the log",
            "Traceback" in str(j.get("log") or ""),
            f"log was {str(j.get('log'))[:200]!r}")


# ── run-all must stop when a stage reports failure ─────────────────────────

def check_run_all_stops_on_reported_failure(r: Results) -> None:
    """Drive the real /run-all route with the stage function stubbed.

    The chain must stop where a stage REPORTS failure, exactly as it already
    stops where a stage raises. Walking on means the next stage - possibly the
    billed domain purchase - runs on the strength of a stage that did not
    succeed.
    """
    print("\n/run-all stops on a reported failure")
    try:
        from flask import Flask
    except ModuleNotFoundError as e:
        r.skip("/run-all stops on a reported failure",
               f"flask is not installed in this environment ({e})")
        return

    cn = "00000040"
    order = api_v2.STAGE_ORDER
    fail_at = order[1]
    ran: list[str] = []

    saved = (api_v2._find_row, api_v2._compute_stages, api_v2._exec_stage,
             api_v2._co_lock)
    try:
        api_v2._find_row = lambda _cn: {"Company Number": cn,
                                        "Company Name": "TEST CO LTD"}
        api_v2._compute_stages = lambda _cn, _row: {s: False for s in order}
        api_v2._co_lock = lambda _cn: threading.Lock()

        def fake_exec(_cn, stage, _row):
            ran.append(stage)
            if stage == fail_at:
                return {"status": "error", "error": "reported failure"}
            return {"status": "done"}

        api_v2._exec_stage = fake_exec

        app = Flask(__name__)
        app.register_blueprint(api_v2.api_v2)
        client = app.test_client()
        resp = client.post(f"/api/v2/company/{cn}/run-all")
        r.check("the route accepted the request", resp.status_code == 200,
                f"HTTP {resp.status_code}: {resp.data!r}")

        deadline = time.time() + 5
        while time.time() < deadline:
            if api_v2._job_get(cn, fail_at).get("status") in ("done", "failed"):
                break
            time.sleep(0.02)
        time.sleep(0.2)   # give a wrongly-continuing chain time to show itself

        r.check(f"the failing stage ({fail_at}) is marked failed",
                api_v2._job_get(cn, fail_at).get("status") == "failed",
                f"job was {api_v2._job_get(cn, fail_at)!r}")
        r.check(f"the stage before it ({order[0]}) is marked done",
                api_v2._job_get(cn, order[0]).get("status") == "done",
                f"job was {api_v2._job_get(cn, order[0])!r}")
        r.check("no stage after the failure ran",
                ran == [order[0], fail_at],
                f"stages that ran: {ran!r}; expected to stop after {fail_at!r}")
        for later in order[2:]:
            r.check(f"{later} never started",
                    api_v2._job_get(cn, later).get("status") in (None, "", "idle"),
                    f"job was {api_v2._job_get(cn, later)!r}")
    finally:
        (api_v2._find_row, api_v2._compute_stages, api_v2._exec_stage,
         api_v2._co_lock) = saved


# ── the drawer has to show something ───────────────────────────────────────

_DRAWER_CASES = [
    # (label, job, must appear in the rendered text)
    ("a real log wins",            {"status": "done", "log": "line one"}, "line one"),
    ("failed with empty log",      {"status": "failed", "log": "",
                                    "error": "Namecheap rejected the order"},
                                   "Namecheap rejected the order"),
    ("done with empty log",        {"status": "done", "log": "",
                                    "result": "{'status': 'registered'}"},
                                   "registered"),
    ("running with nothing yet",   {"status": "running", "log": ""},
                                   "Waiting for output"),
    ("idle",                       {"status": "idle"}, "No output yet."),
    ("no job at all",              {}, "No output yet."),
]


def _extract_js_function(src: str, name: str) -> str | None:
    """Pull one top-level `function name(...) { ... }` out of the page."""
    m = re.search(r"^function\s+" + re.escape(name) + r"\s*\(", src, re.M)
    if not m:
        return None
    i = src.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
    return None


def check_drawer_text(r: Results) -> None:
    """Run the SHIPPED drawer function in node against real job shapes."""
    print("\nlog drawer text (static/app.html, run in node)")
    page = os.path.join(REPO, "static", "app.html")
    with open(page, encoding="utf-8") as f:
        src = f.read()

    fn = _extract_js_function(src, "drawerText")
    if not r.check("static/app.html defines drawerText(j)", fn is not None,
                   "the drawer's text choice has to be a named function for "
                   "this test to reach it; without it the fallback is inline "
                   "in refreshLogContent and only a browser can see it"):
        return

    r.check("refreshLogContent uses drawerText",
            re.search(r"drawerText\s*\(\s*j\s*\)", src) is not None,
            "the function exists but nothing calls it with the job")

    if not shutil_which("node"):
        r.skip("drawer text cases", "node is not on PATH")
        return

    with tempfile.TemporaryDirectory() as td:
        driver = os.path.join(td, "drawer_test.mjs")
        with open(driver, "w", encoding="utf-8") as f:
            f.write(fn + "\n")
            f.write("const cases = " + json.dumps(
                [[lbl, job, want] for lbl, job, want in _DRAWER_CASES]) + ";\n")
            f.write("""
const out = [];
for (const [label, job, want] of cases) {
  let text, err = null;
  try { text = String(drawerText(job)); }
  catch (e) { text = ""; err = String(e); }
  out.push({label, text, ok: text.includes(want), err});
}
console.log(JSON.stringify(out));
""")
        proc = subprocess.run([shutil_which("node"), driver],
                              capture_output=True, text=True, timeout=60)
        if not r.check("node ran the extracted function", proc.returncode == 0,
                       f"exit {proc.returncode}: {proc.stderr.strip()[:400]}"):
            return
        results = json.loads(proc.stdout.strip().splitlines()[-1])

    for (lbl, job, want), got in zip(_DRAWER_CASES, results):
        r.check(f"drawer: {lbl}", got["ok"],
                f"job {job!r} rendered {got['text']!r}, expected it to contain "
                f"{want!r}" + (f" (error: {got['err']})" if got["err"] else ""))


def shutil_which(cmd: str):
    import shutil
    return shutil.which(cmd)


# ── "nothing was downloaded" is not a success ──────────────────────────────

def _exec(cn: str, stage: str, row: dict | None = None) -> dict:
    """Run the REAL _exec_stage through the REAL job runner."""
    row = row or {"Company Number": cn, "Company Name": "TEST CO LTD"}
    return _run_spawn(cn, stage, lambda: api_v2._exec_stage(cn, stage, row))


def check_certificate_failure_marks_failed(r: Results) -> None:
    """download_certificate reports a missing document by RETURNING
    (None, reason) - run_pipeline.py:894 and :904 - never by raising. With no
    status in the returned dict the job runner read that as success, so a
    company with no certificate showed a green tick.
    """
    print("\ncertificate: nothing downloaded -> job failed")
    import run_pipeline
    saved = (run_pipeline.download_certificate, api_v2._update_excel_row)
    writes: list = []
    try:
        api_v2._update_excel_row = lambda cn, updates: writes.append(updates)
        run_pipeline.download_certificate = lambda cn, out: (None, "No documents available")
        j = _exec("00000050", "certificate")
        r.check("job status is failed", j.get("status") == "failed",
                f"status was {j.get('status')!r}")
        r.check("the reason reaches the operator",
                "No documents available" in str(j.get("error") or ""),
                f"error was {j.get('error')!r}")
        r.check("nothing was written to the company row", writes == [],
                f"writes were {writes!r}: a failed download must not mark the "
                f"certificate as downloaded")

        writes.clear()
        run_pipeline.download_certificate = lambda cn, out: ("/tmp/Certificate_X.pdf", None)
        j = _exec("00000051", "certificate")
        r.check("a real download still reads done", j.get("status") == "done",
                f"status was {j.get('status')!r}")
        r.check("...and is still recorded on the row",
                any("Certificate Path" in w for w in writes),
                f"writes were {writes!r}")
    finally:
        run_pipeline.download_certificate, api_v2._update_excel_row = saved


# ── every stage says one line ──────────────────────────────────────────────

def check_every_stage_narrates(r: Results) -> None:
    """A finished stage must leave something human in the job log.

    Before this, five of the seven stages printed nothing at all, so the best
    the drawer could do was dump the raw Python dict the stage returned.
    """
    print("\nevery stage narrates one line into the job log")
    import run_pipeline
    import email_pool

    class FakePool:
        def get_assigned_email(self, cn):
            return ("", "", "")

        def assign_next(self, company_number="", company_name=""):
            return ("acct41@example.invalid", "Ada", "Byron")

    saved = {
        "details": run_pipeline.get_company_details,
        "duns": run_pipeline.get_duns_number,
        "cert": run_pipeline.download_certificate,
        "domain": run_pipeline.register_company_domain,
        "pool": email_pool.EmailPool,
        "upd": api_v2._update_excel_row,
    }
    try:
        api_v2._update_excel_row = lambda cn, updates: None
        run_pipeline.get_company_details = lambda cn: {
            "company_number": cn,
            "row_updates": {"Company Name": "TEST CO LTD", "Status": "active",
                            "Directors": "SMITH, Johnathan Archibald"}}
        run_pipeline.get_duns_number = lambda cn, headless=True: {
            "duns_number": "987654321", "status": "found"}
        run_pipeline.download_certificate = lambda cn, out: ("/tmp/Certificate_Y.pdf", None)
        run_pipeline.register_company_domain = lambda name, details: {
            "status": "error", "error": "No available domains found"}
        email_pool.EmailPool = FakePool

        cases = [
            ("details",     "00000060", ["Company Name", "Directors"]),
            ("duns",        "00000061", ["987654321"]),
            ("certificate", "00000062", ["Certificate_Y.pdf"]),
            ("domain",      "00000063", ["No available domains found"]),
            ("email",       "00000064", ["acct41@example.invalid"]),
        ]
        for stage, cn, wants in cases:
            j = _exec(cn, stage)
            log = str(j.get("log") or "")
            r.check(f"{stage} wrote something to the job log", bool(log.strip()),
                    f"log was {log!r}; the drawer would fall back to dumping "
                    f"the returned dict")
            for want in wants:
                r.check(f"{stage} log mentions {want!r}", want in log,
                        f"log was {log!r}")

        # The details stage's row_updates carry director names and dates of
        # birth. The job log names the COLUMNS it wrote, never the values, so
        # the drawer does not become a second place personal data is printed.
        j = _exec("00000065", "details")
        r.check("details log does not print the director's name",
                "SMITH, Johnathan Archibald" not in str(j.get("log") or ""),
                f"log was {str(j.get('log'))!r}")
    finally:
        run_pipeline.get_company_details = saved["details"]
        run_pipeline.get_duns_number = saved["duns"]
        run_pipeline.download_certificate = saved["cert"]
        run_pipeline.register_company_domain = saved["domain"]
        email_pool.EmailPool = saved["pool"]
        api_v2._update_excel_row = saved["upd"]


# ── a write that failed must never report success ──────────────────────────
#
# _update_excel_row caught every exception, printed "[db] update_row failed"
# and returned None. Five routes then answered 200 and the dashboard toasted
# "Saved N field(s)" for a write that never happened, and six stage call
# sites carried on and let the job finish "done" without its result recorded.
#
# This is the same silent-success family as the rest of this file: the
# certificate stage recording a failure as done, _spawn calling a refused
# domain order done. A save that did not happen must not look like one that
# did - that is the whole point.

class _Boom(Exception):
    pass


def _client():
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(api_v2.api_v2)
    return app.test_client()


def _with_broken_db(fn):
    """Run fn() with every row write failing, as a database outage would."""
    import db
    saved = (db.update_row, api_v2._find_row, api_v2._compute_stages)
    try:
        def boom(cn, updates):
            raise _Boom("could not connect to server: Connection refused")
        db.update_row = boom
        api_v2._find_row = lambda cn: {"Company Number": cn,
                                       "Company Name": "TEST CO LTD"}
        api_v2._compute_stages = lambda cn, row: {s: False for s in api_v2.STAGE_ORDER}
        return fn()
    finally:
        db.update_row, api_v2._find_row, api_v2._compute_stages = saved


ROUTES = [
    ("fields",    "/api/v2/company/00000800/fields",    {"fields": {"domain_status": ""}}),
    ("status",    "/api/v2/company/00000800/status",    {"status": "On Hold"}),
    ("notes",     "/api/v2/company/00000800/notes",     {"notes": "a note"}),
    ("archive",   "/api/v2/company/00000800/archive",   None),
    ("unarchive", "/api/v2/company/00000800/unarchive", None),
]


def check_failed_write_is_not_reported_as_saved(r: Results) -> None:
    print("\na write that failed is not reported as saved")
    try:
        from flask import Flask  # noqa: F401
    except ModuleNotFoundError as e:
        r.skip("a write that failed is not reported as saved",
               f"flask is not installed ({e})")
        return

    for name, path, body in ROUTES:
        resp = _with_broken_db(
            lambda: _client().post(path, json=body) if body is not None
            else _client().post(path))
        payload = resp.get_json(silent=True) or {}
        r.check(f"/{name}: does not answer 2xx", not (200 <= resp.status_code < 300),
                f"HTTP {resp.status_code} with {payload!r} - the dashboard reads "
                f"any 2xx as a successful save")
        r.check(f"/{name}: says what went wrong",
                "Connection refused" in str(payload.get("error") or ""),
                f"payload was {payload!r}; the UI shows e.error in its toast")


def check_successful_write_still_reports_ok(r: Results) -> None:
    print("\n...and a write that worked still reports success")
    try:
        from flask import Flask  # noqa: F401
    except ModuleNotFoundError:
        r.skip("a successful write still reports success", "flask is not installed")
        return
    import db
    saved = (db.update_row, api_v2._find_row, api_v2._compute_stages)
    written = []
    try:
        db.update_row = lambda cn, updates: written.append((cn, updates))
        api_v2._find_row = lambda cn: {"Company Number": cn, "Company Name": "TEST CO LTD"}
        api_v2._compute_stages = lambda cn, row: {s: False for s in api_v2.STAGE_ORDER}
        resp = _client().post("/api/v2/company/00000801/fields",
                              json={"fields": {"domain_status": ""}})
        payload = resp.get_json(silent=True) or {}
        r.check("200 is still 200", resp.status_code == 200,
                f"HTTP {resp.status_code}: {payload!r}")
        r.check("and it names what it wrote",
                "Domain Status" in (payload.get("updated") or []), f"{payload!r}")
        r.check("and the write actually happened", len(written) == 1, f"{written!r}")
    finally:
        db.update_row, api_v2._find_row, api_v2._compute_stages = saved


def check_stage_whose_write_fails_is_not_done(r: Results) -> None:
    """A stage that could not record its result has not finished its job."""
    print("\na stage whose row write fails does not read as done")
    import db
    import email_pool

    class FakePool:
        def get_assigned_email(self, cn):
            return ("", "", "")

        def assign_next(self, company_number="", company_name=""):
            return ("acct9@example.invalid", "Ada", "Byron")

    saved = (db.update_row, email_pool.EmailPool)
    try:
        def boom(cn, updates):
            raise _Boom("could not connect to server: Connection refused")
        db.update_row = boom
        email_pool.EmailPool = FakePool
        j = _run_spawn("00000802", "email",
                       lambda: api_v2._exec_stage(
                           "00000802", "email",
                           {"Company Number": "00000802", "Company Name": "TEST CO LTD"}))
        r.check("the job is failed", j.get("status") == "failed",
                f"status was {j.get('status')!r} - the email was assigned from the "
                f"pool but never recorded, so the stage did not finish")
        r.check("the reason is carried", "Connection refused" in str(j.get("error") or ""),
                f"error was {j.get('error')!r}")
    finally:
        db.update_row, email_pool.EmailPool = saved


# ── an outage is an outage, not "no such company" ──────────────────────────
#
# _all_rows returned [] and _find_row returned None when the database could
# not be reached, so a dashboard with a dead database showed "No companies
# found" and every company answered 404 "Not found". Both are statements
# about the DATA. Neither was true: nothing was known either way.
#
# pipeline.py:180 carries the same warning for the Namecheap listing - an
# error response has no Domain elements, so an unchecked failure reads as an
# account with nothing in it.

def check_outage_is_not_an_empty_list(r: Results) -> None:
    print("\nreads: a database outage is not an empty company list")
    try:
        from flask import Flask  # noqa: F401
    except ModuleNotFoundError:
        r.skip("a database outage is not an empty company list", "flask missing")
        return
    import db
    saved = db.all_rows
    try:
        def boom(use_cache=True):
            raise _Boom("could not connect to server: Connection refused")
        db.all_rows = boom
        resp = _client().get("/api/v2/companies")
        payload = resp.get_json(silent=True)
        r.check("it does not answer 200 with an empty list",
                resp.status_code != 200,
                f"HTTP {resp.status_code} with {payload!r} - an empty list reads "
                f"as 'this pipeline has no companies'")
        r.check("it says the database could not be reached",
                "could not be reached" in str((payload or {}).get("error") or "").lower()
                or "Connection refused" in str((payload or {}).get("error") or ""),
                f"payload was {payload!r}")
    finally:
        db.all_rows = saved


def check_outage_is_not_a_missing_company(r: Results) -> None:
    print("\nreads: a database outage is not a missing company")
    try:
        from flask import Flask  # noqa: F401
    except ModuleNotFoundError:
        r.skip("a database outage is not a missing company", "flask missing")
        return
    import db
    saved = db.find_row
    try:
        def boom(cn):
            raise _Boom("could not connect to server: Connection refused")
        db.find_row = boom
        resp = _client().get("/api/v2/company/00000900")
        payload = resp.get_json(silent=True)
        r.check("it is not a 404", resp.status_code != 404,
                f"HTTP {resp.status_code} - 404 says this company does not exist, "
                f"which is a claim about the data nobody can make right now")
        r.check("it says the database could not be reached",
                "Connection refused" in str((payload or {}).get("error") or ""),
                f"payload was {payload!r}")
    finally:
        db.find_row = saved


def check_a_genuinely_missing_company_is_still_404(r: Results) -> None:
    print("\nreads: a company that really is absent is still 404")
    try:
        from flask import Flask  # noqa: F401
    except ModuleNotFoundError:
        r.skip("a genuinely missing company is still 404", "flask missing")
        return
    import db
    saved = db.find_row
    try:
        db.find_row = lambda cn: None
        resp = _client().get("/api/v2/company/00000901")
        r.check("404", resp.status_code == 404, f"HTTP {resp.status_code}")
    finally:
        db.find_row = saved


def check_the_list_still_works(r: Results) -> None:
    print("\nreads: a working database still lists companies")
    try:
        from flask import Flask  # noqa: F401
    except ModuleNotFoundError:
        r.skip("a working database still lists companies", "flask missing")
        return
    import db
    saved = (db.all_rows, api_v2._compute_stages)
    try:
        db.all_rows = lambda use_cache=True: [
            {"Company Number": "00000902", "Company Name": "TEST CO LTD"}]
        api_v2._compute_stages = lambda cn, row: {s: False for s in api_v2.STAGE_ORDER}
        resp = _client().get("/api/v2/companies")
        payload = resp.get_json(silent=True) or []
        r.check("200", resp.status_code == 200, f"HTTP {resp.status_code}")
        r.check("with the company", any(c.get("cn") == "00000902" for c in payload),
                f"{payload!r}")
    finally:
        db.all_rows, api_v2._compute_stages = saved


_LIST_MSG_CASES = [
    ("an outage says so", {"loadError": "Connection refused"}, "Could not reach"),
    ("an outage names the reason", {"loadError": "Connection refused"}, "Connection refused"),
    ("no companies still says that", {"loadError": ""}, "No companies found"),
    ("no error key at all", {}, "No companies found"),
]


def check_sidebar_message(r: Results) -> None:
    """The server now refuses to pretend, so the page must not pretend either."""
    print("\nreads: the sidebar does not say 'No companies' during an outage")
    page = os.path.join(REPO, "static", "app.html")
    with open(page, encoding="utf-8") as f:
        src = f.read()
    fn = _extract_js_function(src, "companyListMessage")
    if not r.check("static/app.html defines companyListMessage(state)", fn is not None,
                   "an empty sidebar during an outage is the same lie the server "
                   "just stopped telling"):
        return
    r.check("renderSidebar uses it",
            re.search(r"companyListMessage\s*\(", src[src.index("function renderSidebar"):]
                      if "function renderSidebar" in src else "") is not None,
            "the function exists but the sidebar does not call it")
    if not shutil_which("node"):
        r.skip("sidebar message cases", "node is not on PATH")
        return
    with tempfile.TemporaryDirectory() as td:
        driver = os.path.join(td, "m.mjs")
        with open(driver, "w", encoding="utf-8") as f:
            f.write(fn + "\n")
            f.write("const cases = " + json.dumps(
                [[l, st, w] for l, st, w in _LIST_MSG_CASES]) + ";\n")
            f.write("""
const out = [];
for (const [label, state, want] of cases) {
  let text; try { text = String(companyListMessage(state)); } catch (e) { text = "ERR " + e; }
  out.push({label, text, ok: text.includes(want)});
}
console.log(JSON.stringify(out));
""")
        proc = subprocess.run([shutil_which("node"), driver], capture_output=True,
                              text=True, timeout=60)
        if not r.check("node ran it", proc.returncode == 0, proc.stderr[:300]):
            return
        for (lbl, st, want), got in zip(_LIST_MSG_CASES,
                                        json.loads(proc.stdout.strip().splitlines()[-1])):
            r.check(f"sidebar: {lbl}", got["ok"],
                    f"state {st!r} rendered {got['text']!r}, wanted {want!r}")


# ── main ────────────────────────────────────────────────────────────────────

def main() -> int:
    print("test_stage_feedback.py — what the dashboard is told when a stage runs")
    print(f"  + {os.path.join(REPO, 'logging_setup.py')}  (money -> stdout)")
    print(f"  + {os.path.join(REPO, 'api_v2.py')}         (_spawn / _seq status)")
    print(f"  + {os.path.join(REPO, 'static', 'app.html')} (drawerText)")
    r = Results()
    check_money_log_reaches_job_log(r)
    check_money_log_still_on_stderr(r)
    check_money_log_does_not_cross_jobs(r)
    check_returned_error_marks_failed(r)
    check_returned_unresolved_marks_failed(r)
    check_success_statuses_still_done(r)
    check_raise_still_marks_failed(r)
    check_run_all_stops_on_reported_failure(r)
    check_certificate_failure_marks_failed(r)
    check_every_stage_narrates(r)
    check_failed_write_is_not_reported_as_saved(r)
    check_successful_write_still_reports_ok(r)
    check_stage_whose_write_fails_is_not_done(r)
    check_outage_is_not_an_empty_list(r)
    check_outage_is_not_a_missing_company(r)
    check_a_genuinely_missing_company_is_still_404(r)
    check_the_list_still_works(r)
    check_sidebar_message(r)
    check_drawer_text(r)
    return r.report()


if __name__ == "__main__":
    raise SystemExit(main())
