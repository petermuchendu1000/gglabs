"""Mint per-company Android signing keystores, deterministically.

Password = hex(HMAC-SHA256(master_secret, company_number))[:32].

The master secret lives in `apps/generator/.master_secret` (auto-created on
first run if missing). LOSING IT = every rebuild produces a different
signing identity = Android refuses the "update" install. Back it up.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import shutil
import subprocess
from dataclasses import dataclass

_MASTER_SECRET_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".master_secret")


def _load_or_create_master() -> bytes:
    if os.path.exists(_MASTER_SECRET_PATH):
        with open(_MASTER_SECRET_PATH, "rb") as f:
            return f.read().strip()
    secret = secrets.token_hex(32).encode("ascii")  # 64 hex chars
    with open(_MASTER_SECRET_PATH, "wb") as f:
        f.write(secret)
    print(f"  Minted master secret at {_MASTER_SECRET_PATH} — BACK THIS UP.")
    return secret


def _find_keytool() -> str:
    # Prefer the one on PATH (JAVA_HOME/bin) over bundled JBR; both work.
    exe = shutil.which("keytool") or shutil.which("keytool.exe")
    if exe:
        return exe
    java_home = os.environ.get("JAVA_HOME", "")
    if java_home:
        for exe in ("keytool", "keytool.exe"):
            cand = os.path.join(java_home, "bin", exe)
            if os.path.exists(cand):
                return cand
    # There was a third branch here hardcoding one machine's Adoptium
    # install. It was guarded by os.path.exists so it never fired on the
    # server, but it is the same defect as the two Windows defaults removed
    # from pipeline.stage_build and it belongs with them. PATH and JAVA_HOME
    # are the two places a JDK is findable; a third is somebody's laptop.
    raise FileNotFoundError(
        "keytool not found. Install a JDK (the app template needs 17) or "
        "set JAVA_HOME to one that has bin/keytool.")


@dataclass(frozen=True)
class Keystore:
    path: str
    store_password: str
    key_alias: str
    key_password: str


def mint(
    company_number: str,
    company_name: str,
    out_dir: str,
) -> Keystore:
    """Return a keystore at {out_dir}/{company_number}.jks. Idempotent."""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{company_number}.jks")

    master = _load_or_create_master()
    digest = hmac.new(master, company_number.encode("ascii"), hashlib.sha256).hexdigest()
    password = digest[:32]   # 128 bits, >> keytool minimum of 6
    alias = "upload"

    if os.path.exists(path):
        return Keystore(path=path, store_password=password, key_alias=alias, key_password=password)

    keytool = _find_keytool()
    # Sanitise company name for X.500 DN (no commas, no quotes; <= 64 chars per RDN)
    cn = company_name.replace(",", " ").replace('"', "").replace("\\", "").strip()[:64]
    if not cn:
        cn = company_number
    dname = f"CN={cn}, O={cn}, L=London, C=GB"

    cmd = [
        keytool, "-genkeypair",
        "-keystore", path,
        "-storetype", "PKCS12",
        "-storepass", password,
        "-keypass", password,
        "-alias", alias,
        "-keyalg", "RSA",
        "-keysize", "2048",
        "-validity", "10950",   # 30 years
        "-dname", dname,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(
            f"keytool failed for {company_number}:\n"
            f"  stdout: {res.stdout}\n"
            f"  stderr: {res.stderr}"
        )
    return Keystore(path=path, store_password=password, key_alias=alias, key_password=password)


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "template-shift-journal", "app", "keystores"
    )
    ks = mint("13510663", "SWIFT PLUS PERSONNEL LTD", out)
    print(f"OK  {ks.path}  alias={ks.key_alias}  pass={ks.store_password[:8]}…")
