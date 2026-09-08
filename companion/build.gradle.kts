// AGP 9 compiles Kotlin itself; the org.jetbrains.kotlin.android plugin is
// no longer required and refuses to apply alongside it.
plugins {
    id("com.android.application") version "9.0.0" apply false
}
