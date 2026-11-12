#!/usr/bin/env python3
"""Check that the dashboard page actually serves its UI elements.

    APP_AUTH_TOKEN=... python3 test_ui_elements.py
    BASE_URL=http://127.0.0.1:5000 APP_AUTH_TOKEN=... python3 test_ui_elements.py

Needs a running server; it is the one test here that does. It used to report
every element as MISSING and still exit 0, which made it incapable of failing
and so worth nothing as a check:

  * it sent no credential, and app.py fails CLOSED (nothing but /health
    without APP_AUTH_TOKEN), so the auth gate answered with the login page and
    all six elements were "missing" from it. A login page is now reported as a
    login page rather than as six broken UI elements.
  * it never set an exit code, so a run where everything was missing looked
    exactly like a run where everything was fine.
"""
import base64
import os
import sys

import requests

BASE = os.getenv("BASE_URL", "http://localhost:5000").rstrip("/")
TOKEN = os.getenv("APP_AUTH_TOKEN", "")

headers = {}
if TOKEN:
    # app.py: HTTP Basic, any username, password = APP_AUTH_TOKEN. Sent
    # preemptively because the gate redirects a browser to /login instead of
    # answering 401, so a challenge-driven client never gets asked.
    headers["Authorization"] = "Basic " + base64.b64encode(
        f"ui-test:{TOKEN}".encode()).decode()

try:
    response = requests.get(BASE + "/", headers=headers, timeout=30)
except requests.RequestException as e:
    print(f"Could not reach {BASE}: {e.__class__.__name__}: {e}")
    print("Start the app first; this test needs a running server.")
    raise SystemExit(2)

html = response.text

if "Sign in to continue" in html or "/login" in response.url:
    print(f"{BASE}/ answered with the sign-in page, so no UI element could be "
          f"checked.")
    print("Set APP_AUTH_TOKEN to the server's token and run this again.")
    raise SystemExit(2)

checks = {
    'Company Status select': 'id="company-status-select"' in html,
    'Notes textarea': 'id="company-notes-textarea"' in html,
    'Archive button': 'id="archive-toggle-btn"' in html,
    'Status filter select': 'id="status-filter"' in html,
    'Archive tab': 'data-f="archive"' in html,
    'Status Filter label': 'Status Filter' in html,
    # The log drawer must be able to say something when a stage printed
    # nothing: a refused domain order carries its reason in the job's `error`,
    # not in its captured stdout.
    'Log drawer text function': 'function drawerText(j)' in html,
    'Log drawer uses it': 'drawerText(j)' in html.split(
        'function drawerText(j)')[-1],
}

print(f"UI Element Check Results ({BASE}, HTTP {response.status_code}):")
print("-" * 50)
for check, result in checks.items():
    print(f"{check:.<40} {'OK' if result else 'MISSING'}")

missing = [name for name, ok in checks.items() if not ok]
print("-" * 50)
print(f"{len(checks) - len(missing)} present / {len(missing)} missing")
if missing:
    print("MISSING: " + ", ".join(missing))
sys.exit(1 if missing else 0)
