plugins {
    id("com.android.application")
}

android {
    namespace = "com.immichoutbox.companion"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.immichoutbox.companion"
        // Android 10 was the Pixel 1's last update, and the Pixel 1 is the
        // entire reason this app exists.
        minSdk = 29
        targetSdk = 36
        versionCode = 1
        versionName = "1.1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
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
