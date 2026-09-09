"""The version has to be a fact, not a decoration.

It answers one question -- "am I running the latest?" -- and it can only
answer it if every place the number appears reads the same source. So the
VERSION file is the single origin, and these tests hold the rest of the
system to it.
"""

import importlib
import os
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "VERSION"

# x.y.z and nothing else. A suffix would still sort and compare, but the
# point of the scheme is that a person can read it at a glance.
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def test_the_version_file_exists_and_is_semver():
    assert VERSION_FILE.is_file(), "VERSION is the single source of the number"
    assert SEMVER.match(VERSION_FILE.read_text().strip())


def test_the_app_reports_the_file_when_ci_has_not_stamped_it(rig):
    from app import config
    assert config.APP_VERSION == VERSION_FILE.read_text().strip()


def test_a_stamped_build_wins_over_the_file(monkeypatch):
    """CI is the authority for what it built; the file is the fallback."""
    from app import config
    monkeypatch.setenv("APP_VERSION", "9.9.9")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.APP_VERSION == "9.9.9"
    finally:
        monkeypatch.delenv("APP_VERSION")
        importlib.reload(config)


def test_an_empty_stamp_falls_back_to_the_file(monkeypatch):
    """A CI step that fails to substitute must not stamp an empty version."""
    from app import config
    monkeypatch.setenv("APP_VERSION", "")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.APP_VERSION == VERSION_FILE.read_text().strip()
    finally:
        monkeypatch.delenv("APP_VERSION")
        importlib.reload(config)


async def test_healthz_serves_the_version_without_a_session(rig, monkeypatch):
    """So a deploy can be checked with curl before anyone logs in."""
    from app import immich, main

    async def no_ping():
        return False
    monkeypatch.setattr(immich, "ping", no_ping)

    d = await main.healthz()
    assert SEMVER.match(d["version"])


def test_the_image_carries_the_version_file():
    """config.py reads VERSION next to app/, so it has to be copied in."""
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "COPY VERSION" in dockerfile


def test_the_workflow_reads_the_file_rather_than_the_run_number():
    """A run number goes up on every push and says nothing about change."""
    wf = (ROOT / ".github/workflows/publish.yml").read_text()
    assert "< VERSION" in wf
    assert "github.run_number" not in wf, "the run number is not a version"


def test_the_changelog_documents_the_current_version():
    """A number with no record of what changed cannot be compared."""
    version = VERSION_FILE.read_text().strip()
    changelog = (ROOT / "CHANGELOG.md").read_text()
    assert f"## {version}" in changelog


# ---- the phone app reports the same number ------------------------------

COMPANION = ROOT / "companion" / "app" / "src" / "main" / "java" / "com" / \
            "immichoutbox" / "companion"


def test_the_app_takes_its_version_from_the_file_too():
    """build.gradle.kts must read VERSION rather than carry a copy."""
    gradle = (ROOT / "companion" / "app" / "build.gradle.kts").read_text()
    assert 'rootProject.file("../VERSION")' in gradle
    assert not re.search(r'versionName\s*=\s*"\d', gradle), \
        "versionName is hardcoded and will drift from VERSION"


def test_the_app_does_not_hardcode_a_version_anywhere():
    """The bug this guards: Relay.kt held `const val VERSION = "1.1.0"` while
    the build's versionName came from the file, so a freshly installed 1.3.0
    announced itself as 1.1.0 and was offered an update it already had,
    forever. A number kept in two places will eventually disagree."""
    for src in COMPANION.glob("*.kt"):
        for line in src.read_text().splitlines():
            if line.lstrip().startswith("//") or line.lstrip().startswith("*"):
                continue
            assert not re.search(r'(val|var)\s+\w*VERSION\w*\s*(:\s*String\s*)?=\s*"\d+\.\d+',
                                 line), \
                f"{src.name} hardcodes a version: {line.strip()}"


def test_the_app_asks_android_what_is_installed():
    relay = (COMPANION / "Relay.kt").read_text()
    assert "getPackageInfo" in relay, \
        "the reported version must come from the installed package"


# ---- the phone has to wake itself up ------------------------------------

def test_the_app_schedules_with_an_alarm_not_a_handler():
    """The bug this guards: polling was scheduled with Handler.postDelayed,
    which cannot wake a sleeping CPU. On a phone with its screen off -- the
    entire intended use -- the callback was deferred until something else
    woke the device, so the app never checked in. Measured on a Pixel 1: no
    check-in in over 100 seconds on a 60-second interval, plugged in, with
    the process alive throughout."""
    alarm = (COMPANION / "PollAlarm.kt")
    assert alarm.is_file(), "scheduling must go through AlarmManager"
    text = alarm.read_text()
    assert "AndAllowWhileIdle" in text, \
        "a plain alarm does not fire while the device is idle"
    assert "ELAPSED_REALTIME_WAKEUP" in text, \
        "the alarm has to wake the device, and survive a clock change"

    service = (COMPANION / "FreeSpaceService.kt").read_text()
    assert "postDelayed" not in service, \
        "Handler.postDelayed cannot wake a sleeping phone"


def test_the_poll_holds_the_cpu():
    """The alarm wakes the phone, but nothing keeps it awake once the
    broadcast returns -- and the check-in runs on another thread after it."""
    service = (COMPANION / "FreeSpaceService.kt").read_text()
    assert "PARTIAL_WAKE_LOCK" in service


def test_nothing_is_tapped_by_position():
    """A positional guess at the account picture tapped whatever else sat in
    the corner. On the Photos home screen that is the memories carousel, so
    a run opened a slideshow instead of freeing space."""
    service = (COMPANION / "FreeSpaceService.kt").read_text()
    assert "topRightTarget" not in service


def test_empty_environment_variables_are_treated_as_unset():
    """An unset GitHub secret arrives as an empty string, not as an absent
    variable, so Kotlin's `?:` never fires on it. The signing key password
    fell back to "" instead of the store password, and the build failed with
    "Given final block not properly padded" -- which reads like a corrupt
    keystore rather than a missing default."""
    gradle = (ROOT / "companion" / "app" / "build.gradle.kts").read_text()
    bare = re.findall(r'System\.getenv\("[A-Z_]+"\)\s*\?:', gradle)
    assert not bare, \
        f"bare `System.getenv(...) ?:` cannot survive an empty secret: {bare}"
