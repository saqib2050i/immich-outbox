# Photo relay companion

An Android app for the Pixel that presses Google Photos' **Free up space**
when the relay asks it to.

## Why it exists

Smart Storage clears a backed-up photo from the phone roughly thirty days
after Google Photos confirms it. That delay is the relay's throughput
limit: the outbox cap is the only flow control, and the outbox mirrors the
phone's queue folder, so nothing new goes out until the phone lets go of
what it already has. Pressing the button turns a thirty-day wait into about
a minute.

## What it will not do

**It never deletes a photo.** It taps the button, and Google Photos removes
only what it has already verified as backed up.

This is not a promise, it is a permission list. Check the built APK
yourself:

```bash
~/Library/Android/sdk/build-tools/*/aapt2 dump permissions app/build/outputs/apk/debug/app-debug.apk
```

    uses-permission: name='android.permission.INTERNET'
    uses-permission: name='android.permission.ACCESS_NETWORK_STATE'

That is the whole list. There is no storage or media permission, so the app
is incapable of reading, moving or deleting a single file. Android enforces
it, not the code.

It matters because of how the relay decides something is backed up: a file
disappearing from the outbox is the *only* evidence there is. Anything but
Google Photos deleting a file forges that proof, and the ledger would mark
photos as safe that never left the house.

The accessibility service is also restricted to
`com.google.android.apps.photos` in `accessibility_service_config.xml`, so
it cannot see any other app's screen.

## What it talks to

Only the relay, over the LAN, and only outbound. The phone polls; the
server never connects to the phone, and the app opens no listening socket.

Two endpoints:

| | |
|---|---|
| `POST /api/companion/poll` | check in, and be told whether to free space |
| `POST /api/companion/report` | say how it went |

Both carry the pairing token in an `X-Companion-Token` header.

The server decides *everything*: whether to run, how long to wait before
checking in again, and which button labels to look for. The app has almost
no policy in it, which is why a Google Photos rename is a text field in the
dashboard rather than a new APK.

## Building

Requires Android Studio (for the SDK and its bundled JDK). No other
dependencies — the app uses framework APIs only, so there is nothing to
audit but four Kotlin files.

```bash
cd companion
ANDROID_HOME=~/Library/Android/sdk ./gradlew assembleDebug
```

The APK lands at `app/build/outputs/apk/debug/app-debug.apk`. Opening the
`companion/` folder in Android Studio and pressing Run does the same thing.

Install it on the Pixel over USB:

```bash
~/Library/Android/sdk/platform-tools/adb install -r companion/app/build/outputs/apk/debug/app-debug.apk
```

**Or skip the cable.** CI builds the app and bundles it into the server image,
so the Pixel can fetch it from the relay itself: open
`http://<relay>:8099/app` in the phone's browser, sign in, and tap download.
The app checks on every check-in and offers the link itself when the server is
serving a newer build than the one installed — it never installs anything, so
it needs no permission to do so.

## Signing, and why it matters for updates

Android will only install an update over a copy signed with **the same key**.
The debug keystore is generated per machine, so a CI runner makes a fresh one
on every build — meaning every "update" would look like a different app and be
refused.

Without a key configured, CI still builds and the relay still serves the app.
It is debug-signed, it installs fine the first time, and the install page says
so. Updating it just means uninstalling the old copy first, which loses the
pairing code and the accessibility grant.

To make updates one tap, create a key once and give it to CI:

```bash
keytool -genkeypair -v -keystore companion.jks -alias companion \
  -keyalg RSA -keysize 4096 -validity 10000 \
  -dname "CN=Photo relay companion, O=Home, C=GB"
base64 -i companion.jks | pbcopy      # macOS; on Linux use base64 -w0
```

Then add four repository secrets (Settings → Secrets and variables → Actions):

| Secret | Value |
|---|---|
| `ANDROID_KEYSTORE_BASE64` | the base64 you just copied |
| `ANDROID_KEYSTORE_PASSWORD` | the store password you chose |
| `ANDROID_KEY_ALIAS` | `companion` |
| `ANDROID_KEY_PASSWORD` | the key password (usually the same) |

**Keep `companion.jks` somewhere safe and do not commit it.** Lose it and the
only way to update the app again is to uninstall it from the phone first. It is
not in this repository, and `.gitignore` covers `*.keystore`.

## Setting it up on the phone

1. In the relay dashboard: **Settings → Phone companion**, tick *Use the
   companion app*, save, then **Show pairing code**.
2. Open the app on the Pixel. Enter the relay address
   (`http://192.168.1.2:8099`) and paste the pairing code. Tap **Save and
   check in**.
3. Tap **Open accessibility settings**, find *Photo relay companion*, and
   switch it on. Android will warn you that the service can observe your
   screen; it can only observe Google Photos.
4. Back in the app, the status line should read *Checked in.* Within a
   minute the dashboard's **Phone** card should show the battery and free
   space.

Leave the Pixel plugged in. A charging phone never enters Doze, which is
what keeps the check-ins punctual.

## When it stops working

Google renames these buttons from time to time. When it does, the run fails
and the app reports **what it actually saw on screen**, which appears in the
dashboard and in the alert. Copy the right label out of that message into
**Settings → Phone companion → Button labels** and save. No new APK.

The relay also raises an alert if the phone stops checking in at all —
without one the symptom is indistinguishable from Google Photos simply
being slow, because the outbox just sits full.

## Known limits

- The UI walk is automation against an app that is free to change. It is
  written to fail loudly rather than silently, and to tell you what it saw,
  but it will need the labels updating occasionally.
- Google Photos must be signed in, with backup on, and have finished
  uploading before there is anything for it to free.
- The Pixel 1's last update was Android 10, which is what `minSdk 29`
  targets. On Android 11+ a system delete dialog may appear instead of
  Google Photos' own; if so, add that package to
  `accessibility_service_config.xml` or the service will not see it.
