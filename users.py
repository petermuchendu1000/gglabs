"""User accounts, roles and the approval workflow.

The console used to be guarded by one shared secret. That is the right
shape for one operator and the wrong shape the moment a second person needs
access: a shared password cannot be revoked for one person, cannot say who
did what, and cannot be anything other than all-or-nothing.

This module owns the data. auth.py owns the request gate, admin_routes.py
owns the screens. Keeping them apart means "who may do what" is answerable
by reading one file.

Passwords are stored as werkzeug scrypt hashes (32768:8:1, 16-byte salt,
per-hash). Nothing here ever stores, logs or returns a plaintext password,
and row dictionaries handed to callers have password_hash stripped unless
asked for explicitly.

Two invariants are enforced here rather than in the UI, because a UI check
is a suggestion:

  * The last active superadmin cannot be suspended, rejected, demoted or
    deleted. Locking yourself out of your own admin panel is a one-way
    door on a box you may only reach through a console.
  * Every password change and every suspension bumps session_epoch, and
    auth.py stores that epoch in the session. Changing a password or
    suspending an account therefore invalidates that user's existing
    sessions immediately, instead of leaving a logged-in browser working
    until the cookie happens to expire.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time

from werkzeug.security import check_password_hash, generate_password_hash

import db

log = logging.getLogger(__name__)
# Access changes belong in the audited channel, like money does: granting
# someone an account on this system is granting them a system that spends.
_audit = logging.getLogger("money")

TABLE = "app_users"
AUDIT_TABLE = "user_audit"

ROLE_USER = "user"
ROLE_ADMIN = "admin"
ROLE_SUPERADMIN = "superadmin"
ROLES = (ROLE_USER, ROLE_ADMIN, ROLE_SUPERADMIN)
_RANK = {ROLE_USER: 0, ROLE_ADMIN: 1, ROLE_SUPERADMIN: 2}

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_SUSPENDED = "suspended"
STATUS_REJECTED = "rejected"
STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_SUSPENDED, STATUS_REJECTED)

MIN_PASSWORD_LEN = 12

_PUBLIC_COLS = ("id, email, full_name, role, status, must_change_password, "
                "created_at, approved_at, approved_by, last_login_at, "
                "session_epoch")

# Deliberately permissive. Strict RFC 5322 validation rejects addresses that
# work, and the approval step is the real gate on who gets in - a typo'd
# address simply never gets approved.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class UserError(Exception):
    """A rejection the caller should show to a person, verbatim."""


def normalise_email(email: str) -> str:
    return (email or "").strip().lower()


def rank(role: str) -> int:
    return _RANK.get(role, -1)


def at_least(role: str, required: str) -> bool:
    return rank(role) >= rank(required)


# ── Schema ──────────────────────────────────────────────────────────────────
def ensure_schema() -> None:
    with db._cursor() as cur:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                id                   BIGSERIAL PRIMARY KEY,
                email                TEXT NOT NULL,
                password_hash        TEXT NOT NULL,
                full_name            TEXT,
                role                 TEXT NOT NULL DEFAULT '{ROLE_USER}',
                status               TEXT NOT NULL DEFAULT '{STATUS_PENDING}',
                must_change_password BOOLEAN NOT NULL DEFAULT FALSE,
                created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
                approved_at          TIMESTAMPTZ,
                approved_by          TEXT,
                last_login_at        TIMESTAMPTZ,
                session_epoch        INTEGER NOT NULL DEFAULT 0
            )""")
        # Uniqueness on the NORMALISED address, as an index rather than a
        # column constraint: "Admin@x.com" and "admin@x.com" are one person,
        # and enforcing that in Python alone would lose the race between two
        # simultaneous registrations.
        cur.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {TABLE}_email_lower_idx "
                    f"ON {TABLE} (lower(email))")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {AUDIT_TABLE} (
                id         BIGSERIAL PRIMARY KEY,
                at         TIMESTAMPTZ NOT NULL DEFAULT now(),
                actor      TEXT,
                action     TEXT NOT NULL,
                subject    TEXT,
                detail     TEXT
            )""")
        cur.execute(f"CREATE INDEX IF NOT EXISTS {AUDIT_TABLE}_at_idx "
                    f"ON {AUDIT_TABLE} (at DESC)")
        _harden(cur)


def _harden(cur) -> None:
    """Close these tables to the Data API explicitly, twice over.

    Supabase has an `ensure_rls` event trigger that enables RLS on new
    public tables, and it is enabled on the production project. Its body
    was read rather than trusted to its name, and it ends:

        EXCEPTION WHEN OTHERS THEN
            RAISE LOG 'rls_auto_enable: failed to enable RLS on %', ...

    So if that ALTER fails for any reason the table is still created, the
    CREATE TABLE still succeeds, and the only trace is a log line on a
    server log we cannot read. A table holding password hashes should not
    depend on a mechanism that fails silently and belongs to someone else.

    Two independent closures, because one of them failing should not open
    the table:

      RLS      - with no policy, a non-exempt role sees zero rows
      no grant - anon and authenticated hold no privilege at all, so
                 PostgREST has nothing to expose in the first place

    Both matter here: pg_default_acl on this project grants anon
    arwdDxtm on every new table automatically, so a table created with
    RLS off would be fully readable AND writable with the project's
    public anon key.

    ENABLE, deliberately NOT FORCE. FORCE subjects the table owner to RLS
    as well, which would leave rolbypassrls as the single thing keeping
    this application able to read its own accounts table. With plain
    ENABLE the owner is exempt on its own merits, so the application
    survives even if that role attribute is ever changed.
    """
    for table in (TABLE, AUDIT_TABLE):
        cur.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # The PostgREST roles do not exist outside Supabase, so a bare
        # REVOKE would fail on a plain PostgreSQL used for tests. Guarded
        # per role rather than skipped wholesale, so a project with only
        # one of them is still hardened.
        cur.execute(
            """DO $$
               DECLARE r text;
               BEGIN
                 FOREACH r IN ARRAY ARRAY['anon','authenticated'] LOOP
                   IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
                     EXECUTE format('REVOKE ALL ON TABLE %I FROM %I', %s, r);
                   END IF;
                 END LOOP;
               END $$""".replace("%s", f"'{table}'"))


def record(actor: str, action: str, subject: str = "", detail: str = "") -> None:
    """Append to the access audit trail.

    Never raises: an audit write failing must not take down the action it
    was describing, but it must be loud in the log.
    """
    try:
        with db._cursor() as cur:
            cur.execute(
                f"INSERT INTO {AUDIT_TABLE} (actor, action, subject, detail) "
                f"VALUES (%s, %s, %s, %s)",
                (actor, action, subject, detail))
    except Exception as e:                       # pragma: no cover
        log.error("audit write failed: %s action=%s subject=%s", e, action, subject)
    _audit.info("access: actor=%s action=%s subject=%s %s",
                actor, action, subject, detail)


def audit_tail(limit: int = 200) -> list[dict]:
    with db._cursor() as cur:
        cur.execute(f"SELECT at, actor, action, subject, detail FROM {AUDIT_TABLE} "
                    f"ORDER BY at DESC LIMIT %s", (limit,))
        return [dict(r) for r in cur.fetchall()]


# ── Lookup ──────────────────────────────────────────────────────────────────
def get_by_email(email: str, with_hash: bool = False) -> dict | None:
    cols = _PUBLIC_COLS + (", password_hash" if with_hash else "")
    with db._cursor() as cur:
        cur.execute(f"SELECT {cols} FROM {TABLE} WHERE lower(email) = %s",
                    (normalise_email(email),))
        row = cur.fetchone()
    return dict(row) if row else None


def get_by_id(user_id: int, with_hash: bool = False) -> dict | None:
    cols = _PUBLIC_COLS + (", password_hash" if with_hash else "")
    with db._cursor() as cur:
        cur.execute(f"SELECT {cols} FROM {TABLE} WHERE id = %s", (user_id,))
        row = cur.fetchone()
    return dict(row) if row else None


# ── Request-path cache ──────────────────────────────────────────────────────
# auth.py checks the signed-in user on EVERY request, and the database is
# across a network. Without a cache that is a Supabase round trip per
# request, which would dominate page load.
#
# Mutations in this module invalidate the entry directly rather than waiting
# for the TTL, so suspending an account or changing a password takes effect
# on the next request - not up to CACHE_TTL seconds later. The TTL is only a
# backstop for changes made outside this process (psql, another host).
CACHE_TTL = 10.0
_cache: dict[int, tuple[float, dict | None]] = {}
_cache_lock = threading.Lock()


def invalidate_cache(user_id: int | None = None) -> None:
    with _cache_lock:
        if user_id is None:
            _cache.clear()
        else:
            _cache.pop(user_id, None)


def get_cached(user_id: int) -> dict | None:
    now = time.time()
    with _cache_lock:
        hit = _cache.get(user_id)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
    row = get_by_id(user_id)
    with _cache_lock:
        _cache[user_id] = (now, row)
    return row


def list_users() -> list[dict]:
    with db._cursor() as cur:
        cur.execute(f"SELECT {_PUBLIC_COLS} FROM {TABLE} "
                    f"ORDER BY (status = '{STATUS_PENDING}') DESC, created_at DESC")
        return [dict(r) for r in cur.fetchall()]


def count_active_superadmins(excluding_id: int | None = None) -> int:
    with db._cursor() as cur:
        cur.execute(
            f"SELECT count(*) AS n FROM {TABLE} "
            f"WHERE role = %s AND status = %s AND (%s::bigint IS NULL OR id <> %s)",
            (ROLE_SUPERADMIN, STATUS_APPROVED, excluding_id, excluding_id))
        return cur.fetchone()["n"]


# ── Mutation ────────────────────────────────────────────────────────────────
def validate_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD_LEN:
        raise UserError(f"Password must be at least {MIN_PASSWORD_LEN} characters.")


def create_user(email: str, password: str, full_name: str = "",
                role: str = ROLE_USER, status: str = STATUS_PENDING,
                must_change_password: bool = False,
                actor: str = "self-registration") -> dict:
    email = normalise_email(email)
    if not _EMAIL_RE.match(email):
        raise UserError("That does not look like an email address.")
    validate_password(password)
    if role not in ROLES:
        raise UserError(f"Unknown role: {role}")
    if status not in STATUSES:
        raise UserError(f"Unknown status: {status}")

    with db._cursor() as cur:
        cur.execute(
            f"INSERT INTO {TABLE} (email, password_hash, full_name, role, status, "
            f"                     must_change_password) "
            f"VALUES (%s, %s, %s, %s, %s, %s) "
            f"ON CONFLICT (lower(email)) DO NOTHING "
            f"RETURNING {_PUBLIC_COLS}",
            (email, generate_password_hash(password), full_name.strip(),
             role, status, must_change_password))
        row = cur.fetchone()
    if row is None:
        # ON CONFLICT DO NOTHING rather than a pre-check, so two simultaneous
        # registrations for one address cannot both succeed.
        raise UserError("An account with that email already exists.")
    record(actor, "user.create", email, f"role={role} status={status}")
    return dict(row)


def verify_password(email: str, password: str) -> dict | None:
    """Return the user when the password matches, else None.

    Hashes against a dummy when the account is absent so that a missing
    account and a wrong password take comparable time; otherwise the
    response time tells an attacker which addresses are registered.
    """
    user = get_by_email(email, with_hash=True)
    if user is None:
        generate_password_hash("timing-equalisation-only")
        return None
    if not check_password_hash(user["password_hash"], password or ""):
        return None
    user.pop("password_hash", None)
    return user


def set_password(user_id: int, new_password: str, actor: str,
                 must_change: bool = False) -> None:
    validate_password(new_password)
    with db._cursor() as cur:
        cur.execute(
            f"UPDATE {TABLE} SET password_hash = %s, must_change_password = %s, "
            f"       session_epoch = session_epoch + 1 "
            f" WHERE id = %s RETURNING email",
            (generate_password_hash(new_password), must_change, user_id))
        row = cur.fetchone()
    if row is None:
        raise UserError("No such user.")
    # The epoch bump above is what makes this a revocation rather than a
    # preference: every session minted before this moment stops working.
    invalidate_cache(user_id)
    record(actor, "user.password_change", row["email"], "sessions invalidated")


def set_password_by_email(email: str, new_password: str, actor: str,
                          must_change: bool = False) -> dict:
    """Set one account's password, naming it by address rather than by id.

    The data layer wants an id. A person at a console has an email, and an
    id is the wrong thing to type there: a number carries nothing on its
    face that says it is the account you meant, so a mistyped one resets a
    stranger silently and leaves the intended account untouched.

    Raises rather than returning None when the address resolves to nothing.
    A falsy return would let the caller print "password set" for a reset
    that wrote no row - and the person would then read their failed
    sign-in as a wrong password rather than as a reset that never
    happened. Returns the row so the caller can print back WHICH account
    it changed, which is the only evidence available at a console that the
    right one was hit.

    Deliberately does not validate the password here: set_password does,
    and a second copy of the rule is a second thing to keep in step.
    """
    user = get_by_email(email)
    if user is None:
        raise UserError(f"No account with the address {email}.")
    set_password(user["id"], new_password, actor=actor,
                 must_change=must_change)
    return user


def set_status(user_id: int, status: str, actor: str) -> dict:
    if status not in STATUSES:
        raise UserError(f"Unknown status: {status}")
    target = get_by_id(user_id)
    if target is None:
        raise UserError("No such user.")

    if (target["role"] == ROLE_SUPERADMIN and status != STATUS_APPROVED
            and count_active_superadmins(excluding_id=user_id) == 0):
        raise UserError(
            "This is the last active superadmin. Promote another one first, "
            "or you will lock yourself out of the admin panel.")

    approving = status == STATUS_APPROVED and target["status"] != STATUS_APPROVED
    with db._cursor() as cur:
        cur.execute(
            f"UPDATE {TABLE} SET status = %s, "
            f"       approved_at = CASE WHEN %s THEN now() ELSE approved_at END, "
            f"       approved_by = CASE WHEN %s THEN %s ELSE approved_by END, "
            # Suspending or rejecting must cut off a browser that is already
            # logged in, not wait for its cookie to lapse.
            f"       session_epoch = CASE WHEN %s THEN session_epoch + 1 "
            f"                            ELSE session_epoch END "
            f" WHERE id = %s RETURNING {_PUBLIC_COLS}",
            (status, approving, approving, actor,
             status in (STATUS_SUSPENDED, STATUS_REJECTED), user_id))
        row = cur.fetchone()
    invalidate_cache(user_id)
    record(actor, f"user.{status}", target["email"],
           f"was {target['status']}")
    return dict(row)


def set_role(user_id: int, role: str, actor: str) -> dict:
    if role not in ROLES:
        raise UserError(f"Unknown role: {role}")
    target = get_by_id(user_id)
    if target is None:
        raise UserError("No such user.")
    if (target["role"] == ROLE_SUPERADMIN and role != ROLE_SUPERADMIN
            and count_active_superadmins(excluding_id=user_id) == 0):
        raise UserError(
            "This is the last active superadmin. Promote another one first.")
    with db._cursor() as cur:
        cur.execute(f"UPDATE {TABLE} SET role = %s WHERE id = %s "
                    f"RETURNING {_PUBLIC_COLS}", (role, user_id))
        row = cur.fetchone()
    invalidate_cache(user_id)
    record(actor, "user.role_change", target["email"],
           f"{target['role']} -> {role}")
    return dict(row)


def delete_user(user_id: int, actor: str) -> None:
    target = get_by_id(user_id)
    if target is None:
        raise UserError("No such user.")
    if (target["role"] == ROLE_SUPERADMIN
            and count_active_superadmins(excluding_id=user_id) == 0):
        raise UserError("This is the last active superadmin and cannot be deleted.")
    with db._cursor() as cur:
        cur.execute(f"DELETE FROM {TABLE} WHERE id = %s", (user_id,))
    invalidate_cache(user_id)
    record(actor, "user.delete", target["email"], f"role={target['role']}")


def touch_login(user_id: int) -> None:
    with db._cursor() as cur:
        cur.execute(f"UPDATE {TABLE} SET last_login_at = now() WHERE id = %s",
                    (user_id,))


# ── Bootstrap ───────────────────────────────────────────────────────────────
def ensure_superadmin(email: str, password: str) -> dict | None:
    """Create the first superadmin if it is missing. Idempotent.

    Deliberately does NOT reset the password of an existing account. A
    bootstrap that overwrote the password on every boot would mean the
    value in .env silently outranked whatever the operator later chose,
    and rotating it would be impossible without editing the server.

    The account is created with must_change_password set, because a
    bootstrap password has by definition been written down somewhere -
    in .env, in a runbook, or in the conversation that set it up.
    """
    email = normalise_email(email)
    if not email or not password:
        return None
    existing = get_by_email(email)
    if existing is not None:
        return existing
    user = create_user(email, password, full_name="Superadmin",
                       role=ROLE_SUPERADMIN, status=STATUS_APPROVED,
                       must_change_password=True, actor="bootstrap")
    log.warning("bootstrapped superadmin %s - it must change its password "
                "at first sign-in", email)
    return user


if __name__ == "__main__":
    import sys

    _cmd = sys.argv[1] if len(sys.argv) > 1 else ""

    if _cmd == "--init":
        # Explicit, operator-run. app.py deliberately does not do this at
        # import: starting a process should not issue DDL against a
        # production database as a side effect.
        print("Creating the accounts schema if it is absent.")
        ensure_schema()
        print(f"  '{TABLE}' and '{AUDIT_TABLE}' ready.")

        # Report the security posture rather than leaving it implied. The
        # whole point of hardening explicitly is not to trust a mechanism
        # that fails silently, so this must not fail silently either.
        with db._cursor() as _c:
            _c.execute(
                """SELECT relname, relrowsecurity AS rls
                     FROM pg_class
                    WHERE relname = ANY(%s)
                      AND relnamespace = 'public'::regnamespace
                    ORDER BY relname""", ([TABLE, AUDIT_TABLE],))
            for _r in _c.fetchall():
                mark = "closed" if _r["rls"] else "*** RLS NOT ENABLED ***"
                print(f"  {_r['relname']:12s} row level security: {mark}")
            _c.execute(
                """SELECT grantee, table_name
                     FROM information_schema.role_table_grants
                    WHERE table_schema = 'public'
                      AND table_name = ANY(%s)
                      AND grantee IN ('anon','authenticated')""",
                ([TABLE, AUDIT_TABLE],))
            _left = _c.fetchall()
            if _left:
                print("  *** anon/authenticated STILL hold privileges: "
                      f"{[(r['grantee'], r['table_name']) for r in _left]}")
            else:
                print("  anon/authenticated hold no privileges on either table")

        email = os.getenv("SUPERADMIN_EMAIL", "")
        password = os.getenv("SUPERADMIN_PASSWORD", "")
        if not email or not password:
            print("  SUPERADMIN_EMAIL / SUPERADMIN_PASSWORD not set in .env;"
                  " no superadmin created.")
        else:
            existing = get_by_email(email)
            if existing:
                print(f"  {email} already exists (role={existing['role']}, "
                      f"status={existing['status']}); left untouched.")
            else:
                u = ensure_superadmin(email, password)
                print(f"  created superadmin {u['email']} - it MUST change "
                      f"its password at first sign-in.")
        print(f"\n  superadmins now active: {count_active_superadmins()}")

    elif _cmd == "--passwd":
        # Exists so that a console reset is a short, typeable command. The
        # equivalent `python3 -c` is ~180 characters of nested quoting on a
        # serial terminal that has already corrupted one long line this
        # month, and a line mangled halfway through is worse than one that
        # does not run: the import succeeds and the write may or may not.
        if len(sys.argv) < 4:
            print("  usage: users.py --passwd <email> <password> "
                  "[--force-change]")
            sys.exit(2)
        _email, _pw = sys.argv[2], sys.argv[3]
        _force = "--force-change" in sys.argv[4:]
        try:
            _u = set_password_by_email(_email, _pw, actor="console",
                                       must_change=_force)
        except UserError as _e:
            # Non-zero, because this is typed into a shell where the next
            # thing after && must not run on a reset that did not happen.
            print(f"  REFUSED: {_e}")
            sys.exit(1)
        print(f"  password set for id={_u['id']}  {_u['email']}")
        print(f"    role={_u['role']}  status={_u['status']}")
        print(f"    must change it at next sign-in: {_force}")
        print("    every session that account had is now invalid.")
        print("  NOTE: the password was passed on the command line, so it "
              "is in this shell's history and was visible in `ps`.")

    elif _cmd == "--list":
        for u in list_users():
            print(f"  {u['id']:>4}  {u['email']:32s} {u['role']:11s} "
                  f"{u['status']:10s} must_change={u['must_change_password']}")
        print(f"  ({len(list_users())} accounts)")

    else:
        print(__doc__)
        print("Commands:")
        print("  --init   create the accounts schema and the bootstrap"
              " superadmin from .env")
        print("  --list   list accounts (read-only)")
        print("  --passwd <email> <password> [--force-change]")
        print("           set one account's password, naming it by address")
        sys.exit(2)
