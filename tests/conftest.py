"""Test rig.

Every test gets its own database and its own outbox directory. `config`
reads the environment at import time, so it is reloaded after the
environment is set; everything else dereferences `config.X` lazily and so
picks the new values up in place. `settings` and `backup` also capture
values at import time and are reloaded for the same reason.
"""

import importlib
import os

import pytest


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    outbox = tmp_path / "outbox"
    outbox.mkdir()

    monkeypatch.setenv("DB_PATH", str(tmp_path / "bridge.db"))
    monkeypatch.setenv("OUTBOX_DIR", str(outbox))
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("OUTBOX_MAX_GB", "1")
    monkeypatch.setenv("MAX_BATCH_FILES", "40")
    monkeypatch.setenv("ALLOWED_HOSTS", "")

    from app import auth, backup, config, db, settings
    db.close()
    importlib.reload(config)
    importlib.reload(settings)
    importlib.reload(backup)

    auth._sessions.clear()
    auth._failures.clear()
    db.connect()

    yield Rig(tmp_path, outbox, monkeypatch)

    db.close()


class Rig:
    def __init__(self, root, outbox, monkeypatch):
        self.root = root
        self.outbox = outbox
        self._monkeypatch = monkeypatch

    def cap(self, nbytes: int) -> None:
        """Set the outbox cap in bytes.

        The setting is whole gigabytes, which a test cannot use to fence in
        a handful of hundred-byte files. The cap is the flow control, so a
        test that wants to constrain a fill has to be able to set it.
        """
        from app import settings
        self._monkeypatch.setattr(
            settings.Settings, "outbox_max_bytes",
            property(lambda self: nbytes))

    def files(self) -> set[str]:
        """Delivered files in the outbox, ignoring markers and partials."""
        from app import feeder
        return set(feeder._real_names())

    def used(self) -> int:
        return sum((self.outbox / n).stat().st_size for n in self.files())

    def deliver(self, n: int = 1) -> list[str]:
        """Simulate Smart Storage clearing n files off the phone."""
        gone = sorted(self.files())[:n]
        for name in gone:
            (self.outbox / name).unlink()
        return gone


def asset(i: int, *, size: int = 1000, kind: str = "IMAGE",
          taken: str = "2026-01-01", name: str | None = None) -> dict:
    return {
        "id": f"asset-{i}",
        "filename": name or f"IMG_{i:04d}.jpg",
        "size": size,
        "checksum": f"sum{i}",
        "taken_at": taken,
        "kind": kind,
        "state": "pending",
        "queued_at": None,
        "width": 6000,
        "height": 4000,
        "duration": None,
    }


def fake_download(payload_for=lambda asset_id, size: b"x" * size):
    """Stand in for immich.stream_original.

    Returns bytes of exactly the size the ledger recorded, because top_up
    checks the two against each other before accepting a file.
    """
    class Resp:
        def __init__(self, blob):
            self.blob = blob

        async def aiter_bytes(self, chunk):
            for i in range(0, len(self.blob), chunk):
                yield self.blob[i:i + chunk]

        async def aclose(self):
            pass

    class Client:
        async def aclose(self):
            pass

    async def stream_original(asset_id):
        from app import db
        row = db.connect().execute(
            "SELECT size FROM assets WHERE id=?", (asset_id,)).fetchone()
        return Resp(payload_for(asset_id, row["size"])), Client()

    return stream_original
