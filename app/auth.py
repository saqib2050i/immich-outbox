"""Authentication.

The dashboard hands out API keys if it is left open: a backup download
contains the ledger, and the ledger holds the Immich and Syncthing keys in
plaintext. Syncthing in particular has no read-only key, so its key is full
config control. So the whole surface is behind a password.

Passwords are stored as PBKDF2-HMAC-SHA256 with a per-password random salt,
using only the standard library. The password itself is never written down.

Sessions live in memory. A restart logs everyone out, which for a
single-user LAN tool is a fair trade for having no session store.
"""

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

from . import db

DEFAULT_PASSWORD = "admin"
ITERATIONS = 240_000
SESSION_DAYS = 30
COOKIE = "relay_session"

# token -> expiry
_sessions: dict[str, datetime] = {}


def _hash(password: str, salt: bytes, iterations: int = ITERATIONS) -> str:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def set_password(password: str, must_change: bool = False) -> None:
    db.set_meta("auth_hash", _hash(password, secrets.token_bytes(16)))
    db.set_meta("auth_must_change", "1" if must_change else "0")
    db.set_meta("auth_changed_at", db.now())
    # Recorded rather than re-derived. The dashboard asks "is this still the
    # default password?" on every status refresh, and the only way to answer
    # it from the hash is to run the full PBKDF2 — 240k iterations, tens to
    # hundreds of milliseconds, on the same event loop that is downloading
    # photos. The answer is known for free at the moment it is set.
    db.set_meta("auth_is_default", "1" if password == DEFAULT_PASSWORD else "0")


def ensure_initialised() -> None:
    """First run: seed the default password and demand it be changed."""
    if not db.get_meta("auth_hash"):
        set_password(os.getenv("INITIAL_PASSWORD", DEFAULT_PASSWORD), must_change=True)
        db.log("auth", "password set to the default — change it at first login")


def verify(password: str) -> bool:
    stored = db.get_meta("auth_hash") or ""
    try:
        algo, iters, salt_hex, _ = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    candidate = _hash(password, bytes.fromhex(salt_hex), int(iters))
    # Constant time, so a wrong password cannot be found by timing.
    return hmac.compare_digest(candidate, stored)


def must_change() -> bool:
    return db.get_meta("auth_must_change") == "1"


def is_default_password() -> bool:
    flag = db.get_meta("auth_is_default")
    if flag is None:
        # A database from before the flag existed: pay for the hash once,
        # then record it.
        flag = "1" if verify(DEFAULT_PASSWORD) else "0"
        db.set_meta("auth_is_default", flag)
    return flag == "1"


# ------------------------------------------------------------- sessions

def _sweep() -> None:
    now = datetime.now(timezone.utc)
    for token, exp in list(_sessions.items()):
        if exp < now:
            _sessions.pop(token, None)


def new_session() -> str:
    _sweep()
    token = secrets.token_urlsafe(32)
    _sessions[token] = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    return token


def valid_session(token: str | None) -> bool:
    if not token:
        return False
    exp = _sessions.get(token)
    if not exp:
        return False
    if exp < datetime.now(timezone.utc):
        _sessions.pop(token, None)
        return False
    return True


def end_session(token: str | None) -> None:
    if token:
        _sessions.pop(token, None)


def end_all_sessions() -> None:
    """After a password change, every existing session dies."""
    _sessions.clear()


# ------------------------------------------------------- login throttle

# Verifying a password costs a deliberate 240k PBKDF2 iterations, which is
# also what makes repeated attempts expensive for this service: unthrottled,
# anything on the LAN can keep the CPU busy hashing and starve the feeder.
# A short escalating delay per client makes guessing pointless without ever
# locking the real user out for long.
MAX_FREE_ATTEMPTS = 5
MAX_BACKOFF_SECONDS = 30.0

# client -> (consecutive failures, when the last one was)
_failures: dict[str, tuple[int, datetime]] = {}


def throttle_for(client: str) -> float:
    """Seconds this client must wait before another attempt is considered."""
    record = _failures.get(client)
    if not record:
        return 0.0
    count, last = record
    if count < MAX_FREE_ATTEMPTS:
        return 0.0
    wait = min(2.0 ** (count - MAX_FREE_ATTEMPTS), MAX_BACKOFF_SECONDS)
    elapsed = (datetime.now(timezone.utc) - last).total_seconds()
    return max(0.0, wait - elapsed)


def note_failure(client: str) -> None:
    count, _ = _failures.get(client, (0, None))
    _failures[client] = (count + 1, datetime.now(timezone.utc))
    # Bounded, so a spray of forged client addresses cannot grow this
    # without limit. The oldest attempts are the least interesting.
    if len(_failures) > 512:
        for stale, _ in sorted(_failures.items(), key=lambda kv: kv[1][1])[:256]:
            _failures.pop(stale, None)


def note_success(client: str) -> None:
    _failures.pop(client, None)


# --------------------------------------------------- DNS rebinding guard

def host_allowed(host_header: str | None) -> bool:
    """Reject requests arriving under an unexpected hostname.

    A page on the internet can point its own domain at a private address and
    then talk to this service from the visitor's browser. Blocking hostnames
    we do not recognise stops that before any password is involved.
    """
    if not host_header:
        return False
    host = host_header.rsplit(":", 1)[0].strip("[]").lower()

    extra = {h.strip().lower() for h in os.getenv("ALLOWED_HOSTS", "").split(",") if h.strip()}
    if host in extra or "*" in extra:
        return True
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True

    # Bare IP addresses are fine: rebinding needs a name.
    try:
        import ipaddress
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass

    # Names that only resolve on a LAN are fine too.
    return host.endswith(".local") or host.endswith(".lan") or "." not in host
