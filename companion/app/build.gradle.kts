plugins {
    id("com.android.application")
}

// One VERSION file for the whole project, the same one the server reports.
// The app and the server speak a protocol to each other, so letting their
// numbers drift apart would mean guessing which pairs are compatible. It
// was hardcoded here and had already gone stale by a release.
val declaredVersion: String = rootProject.file("../VERSION").readText().trim()

// Android compares updates by versionCode, an integer that must only ever
// go up; it never looks at the name. 1.2.0 -> 10200, which stays ordered as
// long as minor and patch stay below 100.
val versionInts = declaredVersion.split(".").map { it.toIntOrNull() ?: 0 }
val declaredCode = versionInts[0] * 10000 + versionInts[1] * 100 + versionInts[2]

android {
    namespace = "com.immichoutbox.companion"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.immichoutbox.companion"
        // Android 10 was the Pixel 1's last update, and the Pixel 1 is the
        // entire reason this app exists.
        minSdk = 29
        targetSdk = 36
        versionCode = declaredCode
        versionName = declaredVersion
    }

    // Android will only install an update signed with the same key as the
    // version already on the phone. The debug keystore is generated per
    // machine, so a CI runner makes a fresh one every build -- every
    // "update" would be rejected as a different app. A stable key has to
    // come from outside the build.
    //
    // Absent the secrets this stays null and the release build is unsigned,
    // which the workflow detects and falls back from. Failing here instead
    // would block the server's release over a phone app.
    val keystorePath: String? = System.getenv("ANDROID_KEYSTORE_PATH")
    signingConfigs {
        if (!keystorePath.isNullOrBlank() && file(keystorePath).exists()) {
            create("release") {
                storeFile = file(keystorePath)
                storePassword = System.getenv("ANDROID_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("ANDROID_KEY_ALIAS") ?: "companion"
                keyPassword = System.getenv("ANDROID_KEY_PASSWORD")
                    ?: System.getenv("ANDROID_KEYSTORE_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = signingConfigs.findByName("release")
        }
    }

    lint {
        textReport = true
        // Compatibility with the Pixel 1 is the whole point, so an API used
        // above the floor is a build failure, not a note in a report.
        error += "NewApi"
        // fullBackupContent would be dead configuration: allowBackup="false"
        // already switches backup off entirely below Android 12, which is
        // every version the Pixel 1 will ever run. See the manifest.
        disable += "DataExtractionRules"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

// Deliberately no dependencies. Not androidx, not okhttp, not a JSON
// library -- the framework has all of it. An app asking for an
// accessibility service should be small enough to read in full before you
// grant it.
dependencies {
}
