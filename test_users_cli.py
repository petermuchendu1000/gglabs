#!/usr/bin/env python3
"""Resetting a password from the console, named by email.

This exists because of where it gets run. The console is a serial terminal
reached through noVNC, which has already mangled one long command this
session (`unzip` arrived as `un^zi^p`). A password reset typed there as a
`python3 -c` one-liner is about 180 characters of quoting, and a mangled
one does not fail cleanly - it fails somewhere in the middle, after the
import and possibly after the write.

So the operation gets a named CLI branch, and the branch has to hold one
property above all others:

A RESET THAT CHANGED NOTHING MUST NOT LOOK LIKE ONE THAT WORKED. The data
layer takes a user id. A console operator has an email address. Resolving
one to the other can fail - a typo, an account that was deleted, an address
that was never registered - and the failure mode that matters is the quiet
one: printing "password set" when no row was touched. Whoever ran it then
believes they can sign in, cannot, and has no reason to suspect the reset
rather than the password.

The second property is narrower but just as easy to get wrong: it must
change the account that was NAMED, not merely some account.
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

import users


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


ACCOUNTS = {
    "cosiam22@gmail.com": {"id": 7, "email": "cosiam22@gmail.com",
                           "role": "superadmin", "status": "approved"},
    "other@example.com": {"id": 9, "email": "other@example.com",
                          "role": "user", "status": "approved"},
}


def stub(fn):
    """Run fn with get_by_email and set_password replaced.

    Returns (result, error, calls) - calls records every set_password.
    """
    saved = (users.get_by_email, users.set_password)
    calls: list = []
    users.get_by_email = lambda e, with_hash=False: ACCOUNTS.get(e)
    users.set_password = (
        lambda user_id, new_password, actor, must_change=False:
        calls.append({"id": user_id, "password": new_password,
                      "actor": actor, "must_change": must_change}))
    try:
        return fn(), None, calls
    except Exception as e:                       # noqa: BLE001 - reported
        return None, e, calls
    finally:
        users.get_by_email, users.set_password = saved


def check_a_missing_account_is_a_refusal(r: Results) -> None:
    print("\n-- an address with no account refuses, and writes nothing --")
    out, err, calls = stub(
        lambda: users.set_password_by_email("nobody@example.com", "x" * 14,
                                            actor="console"))
    r.check("it raises rather than returning",
            isinstance(err, users.UserError),
            f"got {err!r} / returned {out!r} - returning None invites a "
            f"caller to print 'done' for a reset that never happened")
    r.check("...and the message names the address",
            "nobody@example.com" in str(err), str(err))
    r.check("...and no password was written",
            calls == [],
            f"{calls!r} - a write against a resolved-to-nothing id is worse "
            f"than no write at all")


def check_the_named_account_is_the_one_changed(r: Results) -> None:
    print("\n-- the account named is the account changed --")
    out, err, calls = stub(
        lambda: users.set_password_by_email("cosiam22@gmail.com",
                                            "Create123456#", actor="console"))
    r.check("it succeeds", err is None, repr(err))
    r.check("exactly one write", len(calls) == 1, repr(calls))
    r.check("...against the id that address resolves to",
            calls and calls[0]["id"] == 7,
            f"{calls!r} - expected id 7, not 9; resetting the wrong account "
            f"locks out a stranger and leaves the real one unchanged")
    r.check("...carrying the password given",
            calls and calls[0]["password"] == "Create123456#", repr(calls))
    r.check("it returns the row, so the caller can say what it changed",
            isinstance(out, dict) and out.get("email") == "cosiam22@gmail.com",
            f"{out!r} - a bare True cannot be printed back as evidence")


def check_must_change_is_off_unless_asked(r: Results) -> None:
    """An operator resetting their OWN password wants to use it."""
    print("\n-- the forced change is opt-in --")
    _, _, calls = stub(
        lambda: users.set_password_by_email("cosiam22@gmail.com", "x" * 14,
                                            actor="console"))
    r.check("default leaves the password usable",
            calls and calls[0]["must_change"] is False,
            f"{calls!r} - defaulting to True means the password just set "
            f"cannot be used, which is not what 'set it to X' asked for")

    _, _, calls = stub(
        lambda: users.set_password_by_email("cosiam22@gmail.com", "x" * 14,
                                            actor="console", must_change=True))
    r.check("...and it can still be demanded",
            calls and calls[0]["must_change"] is True, repr(calls))


def check_a_short_password_is_not_swallowed(r: Results) -> None:
    print("\n-- the length rule still applies through this path --")
    # The real set_password validates. Here the stub does not, so assert
    # instead that this function does not pre-empt or bypass the check:
    # it must hand the password straight to set_password.
    src = open(os.path.join(REPO, "users.py"), encoding="utf-8").read()
    if "def set_password_by_email" not in src:
        r.check("the function exists", False, "nothing to inspect")
        return
    fn = src[src.index("def set_password_by_email"):]
    fn = fn[:fn.index("\ndef ", 1)]
    r.check("it delegates to set_password",
            "set_password(" in fn,
            "re-implementing the write here would mean the epoch bump and "
            "the audit record could drift apart from the id path")
    r.check("...and does not validate the password itself",
            "validate_password" not in fn,
            "two copies of the length rule is one copy too many")


def check_the_cli_branch_cannot_lie(r: Results) -> None:
    print("\n-- the --passwd branch reports failure as failure --")
    src = open(os.path.join(REPO, "users.py"), encoding="utf-8").read()
    if not r.check("the branch exists", '_cmd == "--passwd"' in src,
                   "without it the reset is a 180-character one-liner typed "
                   "into a console that has already corrupted one"):
        return

    tail = src[src.index('_cmd == "--passwd"'):]
    tail = tail[:tail.index("    else:")]
    live = "\n".join(l for l in tail.splitlines()
                     if not l.strip().startswith("#"))

    r.check("a refusal exits non-zero",
            "sys.exit(1)" in live,
            "exit 0 after a refusal is the console equivalent of a silent "
            "success: the next command in a && chain runs")
    r.check("...and is caught explicitly",
            "UserError" in live,
            "an unhandled traceback is survivable; a caught-and-ignored "
            "error is not")
    r.check("usage error exits non-zero too",
            "sys.exit(2)" in live, live[:400])
    r.check("it prints the account it changed",
            "['id']" in live or '["id"]' in live,
            "naming the account is the only way the operator can tell the "
            "reset hit the one they meant")
    r.check("it says the sessions were revoked",
            "sessio" in live.lower(),
            "set_password bumps session_epoch; an operator who does not "
            "know that reads the sudden sign-out as a second fault")

    # The password is in argv. It must not also be put anywhere durable.
    r.check("it never echoes the password back",
            "_pw}" not in live and "{_pw" not in live,
            "printing it puts a live credential in the scrollback of a "
            "console that is shared over noVNC")


def main() -> int:
    r = Results()
    print("test_users_cli.py - resetting a password from the console")
    check_a_missing_account_is_a_refusal(r)
    check_the_named_account_is_the_one_changed(r)
    check_must_change_is_off_unless_asked(r)
    check_a_short_password_is_not_swallowed(r)
    check_the_cli_branch_cannot_lie(r)
    return r.report()


if __name__ == "__main__":
    raise SystemExit(main())
