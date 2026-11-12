#!/usr/bin/env python3
"""Run the end-to-end browser test: `make e2e`.

Starts the real application against a real PostgreSQL, drives the dashboard in
a real Chromium with Playwright, screenshots every stage outcome, then stops
everything it started.

What is real: Flask, auth.py's gate, static/app.html as shipped, every api_v2
route, the job runner and its stdout tee, logging_setup, run_pipeline's
register_company_domain, namecheap_automation's own XML parsing, and the
database schema that db.py creates.

What is doubled, and nothing else: the HTTP boundary to Namecheap, Companies
House and D&B, and the email pool. NO ORDER IS EVER PLACED and no request
leaves the machine. See e2e/app_under_test.py.

Database: uses $DATABASE_URL if set - point it at a THROWAWAY database, the
run truncates the companies table. With no $DATABASE_URL it starts a
temporary PostgreSQL cluster if the server binaries are installed, and
removes it afterwards.

Requires: playwright + chromium for node (`require('playwright')`), and the
Python dependencies in requirements.txt.
"""
from __future__ import annotations

import glob
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PORT = int(os.environ.get("E2E_PORT", "5001"))
BASE = f"http://127.0.0.1:{PORT}"
SHOTS = os.environ.get("E2E_SHOTS") or os.path.join(HERE, "shots")
MODE_FILE = os.path.join(HERE, ".e2e_mode")


def log(msg: str) -> None:
    print(f"[e2e] {msg}", flush=True)


# ── a throwaway PostgreSQL, only when one is not supplied ──────────────────

class TempCluster:
    def __init__(self) -> None:
        self.dir: str | None = None
        self.bin: str | None = None

    def start(self) -> str:
        bins = sorted(glob.glob("/usr/lib/postgresql/*/bin"))
        if not bins:
            raise SystemExit(
                "No DATABASE_URL and no PostgreSQL server binaries found.\n"
                "Set DATABASE_URL to a throwaway database and run again.")
        self.bin = bins[-1]
        # Outside any private temp dir, because the postgres user has to be
        # able to traverse the whole path.
        self.dir = tempfile.mkdtemp(prefix="gglabs-e2e-pg-", dir="/tmp")
        data, run = os.path.join(self.dir, "data"), os.path.join(self.dir, "run")
        os.makedirs(data), os.makedirs(run)
        user = "postgres"
        if os.geteuid() == 0:
            subprocess.run(["chown", "-R", f"{user}:{user}", self.dir], check=True)
            pre = ["su", user, "-s", "/bin/bash", "-c"]
        else:
            pre = ["bash", "-c"]
        port = PORT + 1000
        subprocess.run(pre + [f"{self.bin}/initdb -D {data} -U postgres "
                              f"--auth=trust -E UTF8"],
                       check=True, capture_output=True)
        subprocess.run(pre + [f"{self.bin}/pg_ctl -D {data} -l {self.dir}/pg.log "
                              f"-o '-p {port} -k {run} -c listen_addresses=127.0.0.1' "
                              f"start"], check=True, capture_output=True)
        log(f"started a throwaway PostgreSQL on port {port}")
        return f"postgresql://postgres@127.0.0.1:{port}/postgres"

    def stop(self) -> None:
        if not self.dir:
            return
        data = os.path.join(self.dir, "data")
        pre = (["su", "postgres", "-s", "/bin/bash", "-c"]
               if os.geteuid() == 0 else ["bash", "-c"])
        subprocess.run(pre + [f"{self.bin}/pg_ctl -D {data} stop"],
                       capture_output=True)
        shutil.rmtree(self.dir, ignore_errors=True)
        log("removed the throwaway PostgreSQL")


def wait_for(url: str, token: str, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url)
            urllib.request.urlopen(req, timeout=5).read()
            return True
        except urllib.error.HTTPError:
            return True          # answering at all is enough
        except Exception:
            time.sleep(0.4)
    return False


def main() -> int:
    os.makedirs(SHOTS, exist_ok=True)
    # Generated per run: nothing resembling a credential is ever committed,
    # and a stale one cannot be reused.
    token = os.environ.get("APP_AUTH_TOKEN") or secrets.token_urlsafe(24)

    cluster = TempCluster()
    dsn = os.environ.get("DATABASE_URL")
    started_cluster = False
    if not dsn:
        dsn = cluster.start()
        started_cluster = True

    env = dict(os.environ)
    env.update({
        "DATABASE_URL": dsn, "APP_AUTH_TOKEN": token, "E2E_PORT": str(PORT),
        "E2E_MODE_FILE": MODE_FILE, "E2E_REPO": REPO, "ALLOW_INSECURE_COOKIE": "1",
        "PYTHONPATH": REPO + os.pathsep + env.get("PYTHONPATH", ""),
    })

    server = None
    try:
        log("creating the pipeline schema with db.py")
        subprocess.run([sys.executable, os.path.join(REPO, "db.py")],
                       env=env, check=True, capture_output=True, timeout=120)
        log("clearing the companies table (this database must be throwaway)")
        subprocess.run(
            [sys.executable, "-c",
             "import sys, db; db._invalidate() if hasattr(db,'_invalidate') else None;\n"
             "from db import _cursor\n"
             "with _cursor() as c: c.execute('truncate companies')"],
            env=env, check=True, capture_output=True, timeout=120, cwd=REPO)

        with open(MODE_FILE, "w", encoding="utf-8") as f:
            f.write("registered")

        log(f"starting the application on {BASE}")
        server = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "app_under_test.py")],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            cwd=REPO, start_new_session=True)
        if not wait_for(BASE + "/health", token):
            raise SystemExit("the application did not come up")

        log("driving the dashboard in chromium")
        drive = subprocess.run(
            ["node", os.path.join(HERE, "drive.cjs")],
            env={**env, "E2E_BASE": BASE, "E2E_SHOTS": SHOTS}, cwd=HERE)
        log(f"screenshots and report in {SHOTS}")
        return drive.returncode
    finally:
        if server and server.poll() is None:
            os.killpg(os.getpgid(server.pid), signal.SIGTERM)
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(server.pid), signal.SIGKILL)
            log("stopped the application")
        if started_cluster:
            cluster.stop()
        if os.path.exists(MODE_FILE):
            os.unlink(MODE_FILE)


if __name__ == "__main__":
    raise SystemExit(main())
