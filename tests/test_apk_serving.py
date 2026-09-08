"""Serving the app from the relay itself.

The point is that a change to the phone app ships the same way a change to
the server does -- push, pull the image, and the Pixel offers the update on
its next check-in. No cable, no laptop.

The thing worth guarding is that a server with no app bundled says so
plainly instead of serving a broken download, because "the file is missing"
and "the file is empty" look identical to a phone mid-install.
"""

import json
import os

import pytest


def put_apk(tmp_path, monkeypatch, body=b"PK\x03\x04 not really an apk",
            meta=None):
    """Stand a build in the dist directory the server reads."""
    from app import companion
    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    (dist / "companion.apk").write_bytes(body)
    if meta is not None:
        (dist / "companion.json").write_text(json.dumps(meta))
    monkeypatch.setattr(companion, "DIST_DIR", str(dist))
    companion._apk_cache.clear()
    return dist


# ---- nothing bundled ----------------------------------------------------

@pytest.mark.asyncio
async def test_a_server_with_no_app_says_so(rig, tmp_path, monkeypatch):
    from app import companion
    monkeypatch.setattr(companion, "DIST_DIR", str(tmp_path / "empty"))
    companion._apk_cache.clear()

    info = companion.apk_info()
    assert info["available"] is False
    assert info["version"] == ""


@pytest.mark.asyncio
async def test_downloading_a_missing_app_is_a_404_not_an_empty_file(
        rig, tmp_path, monkeypatch):
    """A zero-byte download would fail on the phone with nothing to explain it."""
    from fastapi import HTTPException
    from app import companion, main

    monkeypatch.setattr(companion, "DIST_DIR", str(tmp_path / "empty"))
    companion._apk_cache.clear()

    with pytest.raises(HTTPException) as raised:
        await main.companion_apk()
    assert raised.value.status_code == 404


@pytest.mark.asyncio
async def test_the_phone_is_told_of_no_build_rather_than_a_blank_version(
        rig, tmp_path, monkeypatch):
    from app import companion, settings
    settings.save({"companion_enabled": True})
    monkeypatch.setattr(companion, "DIST_DIR", str(tmp_path / "empty"))
    companion._apk_cache.clear()

    assert companion.poll({"device": "pixel"})["latest_version"] == ""


# ---- a build is bundled -------------------------------------------------

@pytest.mark.asyncio
async def test_the_build_is_described(rig, tmp_path, monkeypatch):
    from app import companion
    put_apk(tmp_path, monkeypatch, body=b"x" * 4096,
            meta={"version": "1.4.0", "signed": "release",
                  "built_at": "2026-09-08T10:00:00Z", "sha256": "abc123"})

    info = companion.apk_info()
    assert info == {"available": True, "version": "1.4.0", "size": 4096,
                    "sha256": "abc123", "signed": "release",
                    "built_at": "2026-09-08T10:00:00Z"}


@pytest.mark.asyncio
async def test_a_missing_checksum_is_computed(rig, tmp_path, monkeypatch):
    """So a hand-built image still serves something verifiable."""
    import hashlib
    from app import companion
    body = b"hand built"
    put_apk(tmp_path, monkeypatch, body=body, meta={"version": "9.9.9"})
    assert companion.apk_info()["sha256"] == hashlib.sha256(body).hexdigest()


@pytest.mark.asyncio
async def test_the_version_falls_back_to_the_servers_own(rig, tmp_path, monkeypatch):
    """One VERSION file builds both, so they agree unless told otherwise."""
    from app import config, companion
    put_apk(tmp_path, monkeypatch, meta={})
    assert companion.apk_info()["version"] == config.APP_VERSION


@pytest.mark.asyncio
async def test_the_download_is_served_as_an_apk(rig, tmp_path, monkeypatch):
    from app import companion, main
    put_apk(tmp_path, monkeypatch, meta={"version": "1.4.0"})

    resp = await main.companion_apk()
    assert resp.media_type == "application/vnd.android.package-archive"
    # Versioned, so a phone that downloaded before does not reinstall a
    # cached copy of the old build under the same name.
    assert "1.4.0" in resp.headers["content-disposition"]


@pytest.mark.asyncio
async def test_the_phone_is_told_what_is_on_offer(rig, tmp_path, monkeypatch):
    from app import companion, settings
    settings.save({"companion_enabled": True})
    put_apk(tmp_path, monkeypatch, meta={"version": "1.4.0"})

    assert companion.poll({"device": "pixel"})["latest_version"] == "1.4.0"


# ---- the update notice --------------------------------------------------

@pytest.mark.asyncio
async def test_an_out_of_date_phone_is_noticed(rig, tmp_path, monkeypatch):
    from app import companion, settings
    settings.save({"companion_enabled": True})
    put_apk(tmp_path, monkeypatch, meta={"version": "1.4.0"})

    companion.poll({"device": "pixel", "app_version": "1.1.0"})
    snap = companion.snapshot()
    assert snap["update_available"] is True
    assert snap["running_version"] == "1.1.0"
    assert snap["apk"]["version"] == "1.4.0"


@pytest.mark.asyncio
async def test_a_current_phone_is_not_nagged(rig, tmp_path, monkeypatch):
    from app import companion, settings
    settings.save({"companion_enabled": True})
    put_apk(tmp_path, monkeypatch, meta={"version": "1.4.0"})

    companion.poll({"device": "pixel", "app_version": "1.4.0"})
    assert companion.snapshot()["update_available"] is False


@pytest.mark.asyncio
async def test_a_phone_that_has_never_checked_in_is_not_called_out_of_date(
        rig, tmp_path, monkeypatch):
    """Unknown is not the same as old."""
    from app import companion, settings
    settings.save({"companion_enabled": True})
    put_apk(tmp_path, monkeypatch, meta={"version": "1.4.0"})
    assert companion.snapshot()["update_available"] is False


# ---- the description cache ---------------------------------------------

@pytest.mark.asyncio
async def test_a_replaced_build_is_noticed(rig, tmp_path, monkeypatch):
    """The checksum is cached against size and mtime; a new image must not
    keep serving the old description."""
    from app import companion
    dist = put_apk(tmp_path, monkeypatch, body=b"one", meta={"version": "1.0.0"})
    assert companion.apk_info()["version"] == "1.0.0"

    (dist / "companion.apk").write_bytes(b"two but longer")
    (dist / "companion.json").write_text(json.dumps({"version": "2.0.0"}))
    os.utime(dist / "companion.apk", (1, 1))

    assert companion.apk_info()["version"] == "2.0.0"


# ---- the install page ---------------------------------------------------

def test_the_install_page_is_behind_the_session_gate():
    """It is not in OPEN_PATHS, so a stranger on the LAN cannot pull the
    build or read the setup instructions."""
    from app.main import COMPANION_PATHS, OPEN_PATHS
    assert "/app" not in OPEN_PATHS
    assert "/app/companion.apk" not in OPEN_PATHS
    assert "/app" not in COMPANION_PATHS


def test_the_install_page_exists():
    import pathlib
    page = (pathlib.Path(__file__).resolve().parent.parent
            / "app" / "static" / "install.html")
    assert page.is_file()
    text = page.read_text()
    assert "/app/companion.apk" in text, "the page has to link to the download"
    assert "/api/companion/apk" in text, "and ask what is on offer"
