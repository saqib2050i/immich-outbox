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


def test_the_app_takes_its_version_from_its_own_file():
    """build.gradle.kts must read companion/VERSION rather than carry a copy.

    Its own, not the server's. They shared one file so a pair could never be
    untested together, but the server moves for reasons the app has no part
    in and every such move told a phone it was out of date over an APK
    identical to the one on it. What a build can actually do is carried by
    the `features` list in the protocol; a matching number never said that
    and only looked as though it did.
    """
    gradle = (ROOT / "companion" / "app" / "build.gradle.kts").read_text()
    assert 'rootProject.file("VERSION")' in gradle
    assert 'rootProject.file("../VERSION")' not in gradle, \
        "that is the server's version, and the two are separate now"
    assert not re.search(r'versionName\s*=\s*"\d', gradle), \
        "versionName is hardcoded and will drift from the file"


def test_the_app_has_a_version_of_its_own():
    app = (ROOT / "companion" / "VERSION").read_text().strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", app), app


def test_the_apps_version_only_ever_goes_up():
    """Android compares by versionCode and refuses anything not above what
    is installed. The split nearly shipped a companion at 1.0.0 -- code
    10000, against 21000 already on the phone -- which Android would have
    turned down as a downgrade, silently, forever.
    """
    def code(v):
        a, b, c = (int(x) for x in v.split("."))
        return a * 10000 + b * 100 + c

    # The last release built from the shared VERSION file, and so the
    # highest number any phone can already be carrying from that era. Fixed
    # rather than read from CHANGELOG.md: that is the *server's* changelog
    # now, and the server moves on its own clock -- deriving the floor from
    # it would drag the app's version up behind every server release, which
    # is the coupling this split removed.
    SHARED_ERA_LAST = "2.10.0"

    app = (ROOT / "companion" / "VERSION").read_text().strip()
    assert code(app) >= code(SHARED_ERA_LAST), (
        f"companion/VERSION is {app} (code {code(app)}), at or below the "
        f"{SHARED_ERA_LAST} (code {code(SHARED_ERA_LAST)}) a phone may "
        "already be carrying from the shared-file era; Android would refuse "
        "it as a downgrade, silently")


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


# ---- the app is rebuilt only when the app has changed ---------------------

def test_the_apk_is_reused_when_its_sources_have_not_changed():
    """Since the version split the APK does not move when the server does,
    and most runs are server-only -- so most runs rebuilt, from scratch, an
    APK identical to the one before it, on the critical path because the
    image bundles the result."""
    wf = (ROOT / ".github" / "workflows" / "publish.yml").read_text()
    assert "actions/cache/restore@" in wf
    assert "hashFiles('companion/**')" in wf, \
        "the key must be the app's own sources and nothing else"


def test_the_cache_is_written_only_after_a_successful_build():
    """The combined cache action writes from a post step that runs even when
    the job failed, which here would store a dist/ holding nothing under
    these sources' key -- and every later run would hit it, ship no app, and
    say nothing."""
    wf = (ROOT / ".github" / "workflows" / "publish.yml").read_text()
    assert "actions/cache@" not in wf, "use restore/save, not the combined one"
    save = wf[wf.index("actions/cache/save@") - 400:]
    save = save[:save.index("actions/cache/save@") + 200]
    assert "success()" in save
    assert "test -s dist/companion.apk" in wf, \
        "an empty dist/ must never be cached as though it were a build"


def test_debug_and_release_do_not_share_a_cache_key():
    """The same sources signed two different ways are two different files,
    and which one a run produces depends on whether the secret is there."""
    wf = (ROOT / ".github" / "workflows" / "publish.yml").read_text()
    assert "apk-${{ steps.want.outputs.kind }}-" in wf
