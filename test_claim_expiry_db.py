#!/usr/bin/env python3
"""The seven-day claim release, against a REAL PostgreSQL.

    ALLOW_DB_WRITE_TEST=1 DATABASE_URL=postgresql://... python3 test_claim_expiry_db.py

Not part of `make test`, because it writes to a database. `make e2e` runs it
against the throwaway cluster it creates. It refuses to run without
ALLOW_DB_WRITE_TEST=1 so it cannot be pointed at production by accident, and
it only ever touches one reserved company number, which it removes afterwards.

What can only be checked here: the interval arithmetic, and that the release
is ONE conditional statement rather than a read followed by a write. Both are
the difference between a claim that expires when it should and a claim that
expires while an order is still pending.
"""
import os
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO)

TEST_CN = "00000777"          # reserved for this test; removed at the end


def main() -> int:
    if os.getenv("ALLOW_DB_WRITE_TEST") != "1":
        print("SKIP  test_claim_expiry_db.py — this test WRITES to the database.")
        print("      Set ALLOW_DB_WRITE_TEST=1 and point DATABASE_URL at a")
        print("      throwaway database to run it. `make e2e` does both.")
        return 0
    if not os.getenv("DATABASE_URL"):
        print("SKIP  test_claim_expiry_db.py — DATABASE_URL is not set.")
        return 0

    import db
    from logging_setup import configure_logging
    configure_logging()

    dsn = os.environ["DATABASE_URL"]
    print(f"writing to {dsn.split('@')[-1]} as company {TEST_CN}")
    db.ensure_schema()

    passed, failed = 0, []

    def check(name, cond, detail=""):
        nonlocal passed
        if cond:
            passed += 1
            print(f"  PASS  {name}")
        else:
            failed.append(name)
            print(f"  FAIL  {name}" + (f"\n          {detail}" if detail else ""))

    def set_age(hours):
        """Backdate the row so the claim looks `hours` old.

        Hours, not days: "exactly seven days old" is not observable. The
        backdating statement and the release statement run at different
        instants, so a row set to now() - 7 days is already older than seven
        days by the time the release is evaluated. The boundary that means
        anything is "an hour short of seven days" against "an hour past".
        """
        with db._cursor() as cur:
            cur.execute(
                f"UPDATE {db.TABLE} SET updated_at = now() - make_interval(hours => %s) "
                f"WHERE company_number = %s", (hours, TEST_CN))

    def status():
        with db._cursor() as cur:
            cur.execute(f"SELECT domain_status FROM {db.TABLE} "
                        f"WHERE company_number = %s", (TEST_CN,))
            row = cur.fetchone()
        return (row or {}).get("domain_status")

    try:
        with db._cursor() as cur:
            cur.execute(f"DELETE FROM {db.TABLE} WHERE company_number = %s", (TEST_CN,))

        print("\na claim younger than the limit")
        check("the claim is taken", db.claim_domain_purchase(TEST_CN) is True)
        set_age(6 * 24)
        check("six days old is not released",
              db.release_expired_claim(TEST_CN, 7) is False,
              f"domain_status is now {status()!r}")
        check("...and the claim still stands", status() == db.DOMAIN_CLAIM,
              f"domain_status is {status()!r}")

        print("\na claim past the limit")
        set_age(8 * 24)
        check("eight days old IS released",
              db.release_expired_claim(TEST_CN, 7) is True)
        check("the row says so", status() == db.DOMAIN_CLAIM_EXPIRED,
              f"domain_status is {status()!r}")
        check("a second call releases nothing",
              db.release_expired_claim(TEST_CN, 7) is False)
        check("the company can claim again",
              db.claim_domain_purchase(TEST_CN) is True,
              f"domain_status is {status()!r}")

        print("\nthe boundary, and the guards")
        set_age(7 * 24 - 1)
        check("an hour short of seven days does not release",
              db.release_expired_claim(TEST_CN, 7) is False,
              f"domain_status is {status()!r}")
        set_age(7 * 24 + 1)
        check("an hour past seven days does release",
              db.release_expired_claim(TEST_CN, 7) is True,
              f"domain_status is {status()!r}")
        with db._cursor() as cur:
            cur.execute(f"UPDATE {db.TABLE} SET domain_status = %s "
                        f"WHERE company_number = %s", (db.DOMAIN_CLAIM, TEST_CN))
        db.update_row(TEST_CN, {"Domain Status": "registered"})
        set_age(400 * 24)
        check("a resolved claim is never touched, however old",
              db.release_expired_claim(TEST_CN, 7) is False,
              f"domain_status is {status()!r}")
        check("...and keeps its value", status() == "registered",
              f"domain_status is {status()!r}")
        check("a company with no row releases nothing",
              db.release_expired_claim("00000778", 7) is False)
        check("a nonsense age releases nothing",
              db.release_expired_claim(TEST_CN, 0) is False)

        print("\nany write refreshes the clock, so the age is a LOWER bound")
        with db._cursor() as cur:
            cur.execute(f"UPDATE {db.TABLE} SET domain_status = %s "
                        f"WHERE company_number = %s", (db.DOMAIN_CLAIM, TEST_CN))
        set_age(8 * 24)
        db.update_row(TEST_CN, {"Company Notes": "an unrelated edit"})
        check("an unrelated edit delays the release rather than hastening it",
              db.release_expired_claim(TEST_CN, 7) is False,
              f"domain_status is {status()!r}; updated_at is only ever >= the "
              f"claim time, so this can release late but never early")
    finally:
        try:
            with db._cursor() as cur:
                cur.execute(f"DELETE FROM {db.TABLE} WHERE company_number IN (%s, %s)",
                            (TEST_CN, "00000778"))
            print(f"\ncleaned up {TEST_CN}")
        except Exception as e:
            print(f"\ncould not clean up {TEST_CN}: {e}")

    print()
    print(f"{passed} passed / {len(failed)} failed")
    for f in failed:
        print(f"  failed: {f}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
