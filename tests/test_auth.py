"""The dashboard hands out API keys if it is left open, so all of it is
behind a password."""

import pytest

pytestmark = pytest.mark.asyncio


def client():
    from fastapi.testclient import TestClient
    from app.main import app
    # No context manager: that would start the scanner and feeder tasks.
    return TestClient(app)


async def test_unauthenticated_api_calls_are_401(rig):
    from app import auth
    auth.ensure_initialised()
    c = client()
    assert c.get("/api/status").status_code == 401
    assert c.post("/api/refresh").status_code == 401
    assert c.get("/api/stats").status_code == 401


async def test_browser_routes_redirect_to_the_login_page(rig):
    from app import auth
    auth.ensure_initialised()
    c = client()
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/login"
    assert c.get("/login").status_code == 200
    assert c.get("/healthz").status_code in (200, 500)   # open, may fail to ping


async def test_sign_in_and_out(rig):
    from app import auth
    auth.set_password("a-good-password")
    c = client()

    assert c.post("/api/login", json={"password": "wrong"}).status_code == 401
    assert c.post("/api/login", json={"password": "a-good-password"}).status_code == 200
    assert c.get("/api/status").status_code == 200

    assert c.post("/api/logout").status_code == 200
    assert c.get("/api/status").status_code == 401


async def test_the_default_password_must_be_changed_before_anything_else(rig):
    from app import auth
    auth.ensure_initialised()
    assert auth.must_change() and auth.is_default_password()

    c = client()
    c.post("/api/login", json={"password": auth.DEFAULT_PASSWORD})
    # Status still works, so the dashboard can render the demand.
    assert c.get("/api/status").json()["auth"]["must_change"] is True
    # Everything else is closed until the password is replaced.
    assert c.post("/api/refresh").status_code == 403
    assert c.get("/api/settings").status_code == 403

    assert c.post("/api/password", json={"new": "a-good-password"}).status_code == 200
    assert not auth.must_change() and not auth.is_default_password()
    assert c.post("/api/refresh").status_code == 200


async def test_a_new_password_must_be_reasonable(rig):
    from app import auth
    auth.set_password("a-good-password")
    c = client()
    c.post("/api/login", json={"password": "a-good-password"})

    bad = c.post("/api/password", json={"current": "a-good-password", "new": "short"})
    assert bad.status_code == 400
    same = c.post("/api/password",
                  json={"current": "a-good-password", "new": auth.DEFAULT_PASSWORD})
    assert same.status_code == 400
    wrong = c.post("/api/password", json={"current": "nope", "new": "another-good-one"})
    assert wrong.status_code == 400


async def test_changing_the_password_signs_other_sessions_out(rig):
    from app import auth
    auth.set_password("a-good-password")
    other, mine = client(), client()
    other.post("/api/login", json={"password": "a-good-password"})
    mine.post("/api/login", json={"password": "a-good-password"})
    assert other.get("/api/status").status_code == 200

    assert mine.post("/api/password",
                     json={"current": "a-good-password", "new": "a-better-password"}
                     ).status_code == 200
    assert other.get("/api/status").status_code == 401
    assert mine.get("/api/status").status_code == 200, "the browser that changed it"


async def test_is_default_password_costs_no_hash_after_the_first_answer(rig):
    """The dashboard asks on every status refresh; the answer is recorded at
    set_password() time rather than re-derived from 240k PBKDF2 iterations."""
    from app import auth, db

    auth.set_password("a-good-password")
    assert db.get_meta("auth_is_default") == "0"

    calls = []
    original = auth.verify
    auth.verify = lambda pw: calls.append(pw) or original(pw)
    try:
        assert auth.is_default_password() is False
        assert auth.is_default_password() is False
    finally:
        auth.verify = original
    assert calls == [], "the hash was recomputed"


async def test_a_legacy_database_answers_once_then_records_it(rig):
    from app import auth, db

    auth.set_password(auth.DEFAULT_PASSWORD)
    db.connect().execute("DELETE FROM meta WHERE k='auth_is_default'")
    db.connect().commit()

    assert auth.is_default_password() is True
    assert db.get_meta("auth_is_default") == "1"


async def test_repeated_failures_are_throttled(rig):
    from app import auth
    auth.set_password("a-good-password")
    c = client()

    for _ in range(auth.MAX_FREE_ATTEMPTS):
        assert c.post("/api/login", json={"password": "no"}).status_code == 401

    r = c.post("/api/login", json={"password": "no"})
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    # Even the right password waits: guessing cannot be hidden behind a hit.
    assert c.post("/api/login", json={"password": "a-good-password"}).status_code == 429

    auth.note_success("testclient")
    assert c.post("/api/login", json={"password": "a-good-password"}).status_code == 200


async def test_the_throttle_table_stays_bounded(rig):
    from app import auth
    for i in range(700):
        auth.note_failure(f"10.0.0.{i}")
    assert len(auth._failures) <= 512


@pytest.mark.parametrize("host,ok", [
    ("192.168.1.50:8099", True),
    ("127.0.0.1:8099", True),
    ("[::1]:8099", True),
    ("tower.local", True),
    ("nas.lan", True),
    ("tower", True),
    ("photos.evil.com", False),
    ("", False),
    (None, False),
])
async def test_host_allowlist(rig, host, ok):
    from app import auth
    assert auth.host_allowed(host) is ok


async def test_extra_hosts_can_be_allowed(rig, monkeypatch):
    from app import auth
    assert not auth.host_allowed("relay.mydomain.tld")
    monkeypatch.setenv("ALLOWED_HOSTS", "relay.mydomain.tld, other.example")
    assert auth.host_allowed("relay.mydomain.tld")
    assert auth.host_allowed("other.example:8099")
    assert not auth.host_allowed("photos.evil.com")


async def test_an_unrecognised_host_is_refused_before_the_password(rig):
    from app import auth
    auth.set_password("a-good-password")
    c = client()
    r = c.post("/api/login", json={"password": "a-good-password"},
               headers={"host": "photos.evil.com"})
    assert r.status_code == 421
